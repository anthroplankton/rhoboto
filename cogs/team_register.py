from __future__ import annotations

from typing import TYPE_CHECKING, override

from discord import Interaction, Member, Message, app_commands

from bot import config
from cogs.base.discord_context import require_guild_channel_source
from cogs.base.feature_channel_base import FeatureChannelBase
from cogs.base.feature_channel_context import (
    ConfiguredFeatureChannelContext,
    MessageParseResult,
)
from components.ui_storage_errors import send_storage_error
from components.ui_team_register import (
    TEAM_REGISTER_DISPLAY_NAME,
    TeamRegisterView,
    build_summary_embed,
    build_team_register_settings_panel,
    get_fresh_team_register_config_or_respond,
)
from utils.key_async_lock import KeyAsyncLock
from utils.reactions import add_reaction_if_possible, remove_reaction_if_present
from utils.storage_errors import (
    StorageOperationContext,
    classify_storage_exception,
    partial_success_storage_error,
)
from utils.team_register_manager import TeamRegisterManager
from utils.team_register_structs import ClassifiedTeams, Team, TeamParser

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from discord.ui import View

    from bot import Rhoboto
    from components.ui_settings_flow import SettingsPanel
    from models.team_register import TeamRegisterConfig
    from utils.structs_base import UserInfo


class TeamRegister(
    FeatureChannelBase[TeamRegisterManager, list[Team], ClassifiedTeams],
    group_name="team_register",
):
    feature_name = "team_register"
    feature_display_name = TEAM_REGISTER_DISPLAY_NAME
    guide_template_key = "team.guide"
    auto_guide_template_key = "team.auto_guide"
    sheet_write_lock = KeyAsyncLock()
    auto_guide_lock = KeyAsyncLock()

    ManagerType = TeamRegisterManager

    @override
    def _guide_worksheet_id(
        self,
        feature_config: TeamRegisterConfig,
    ) -> int:
        return feature_config.summary_worksheet_id

    @override
    def _build_initial_setup_view(self, manager: TeamRegisterManager) -> View:
        return TeamRegisterView(
            team_register_manager=manager,
            latest_guide_enabled=False,
            latest_guide_toggle_callback=self._toggle_team_latest_guide,
            latest_guide_state_resolver=self._latest_guide_state_resolver(manager),
            latest_guide_refresh_callback=self._latest_guide_refresh_callback(manager),
        )

    def _latest_guide_state_resolver(
        self,
        manager: TeamRegisterManager,
    ) -> Callable[[], Awaitable[bool]]:
        async def latest_guide_state_resolver() -> bool:
            return await self._auto_guide_is_enabled(manager.feature_channel)

        return latest_guide_state_resolver

    @override
    async def _build_settings_panel(
        self,
        interaction: Interaction,
        manager: TeamRegisterManager,
        sheet_config: object,
    ) -> SettingsPanel:
        return await build_team_register_settings_panel(
            manager,
            interaction,
            sheet_config,
            latest_guide_enabled=False,
            latest_guide_toggle_callback=self._toggle_team_latest_guide,
            latest_guide_state_resolver=self._latest_guide_state_resolver(manager),
            latest_guide_refresh_callback=self._latest_guide_refresh_callback(manager),
        )

    async def _toggle_team_latest_guide(
        self,
        interaction: Interaction,
        *,
        enabled: bool,
        current_view: View,
    ) -> None:
        team_register = await get_fresh_team_register_config_or_respond(
            current_view.team_register_manager,
            interaction,
        )
        if team_register is None:
            return

        await self.toggle_auto_guide_from_settings(
            interaction,
            enabled=enabled,
            current_view=current_view,
            feature_config=team_register,
        )

    @override
    async def _parse_message_submission(
        self, message: Message
    ) -> MessageParseResult[list[Team]]:
        user_info = self._message_user_info(message)
        lines = message.content.splitlines()
        parse_result = TeamParser.parse_submission(user_info, lines=lines)
        if parse_result.invalid_attempts:
            return MessageParseResult.invalid(user_info=user_info)

        teams = parse_result.teams
        if not teams:
            return MessageParseResult.ignored()

        return MessageParseResult.parsed(teams, user_info=user_info)

    @override
    async def _process_configured_message_submission(
        self,
        message: Message,
        context: ConfiguredFeatureChannelContext[TeamRegisterManager],
        submission: list[Team],
        user_info: UserInfo,
    ) -> ClassifiedTeams | None:
        teams = submission
        self.logger.info(
            "Parsed teams in Guild: `%s` Channel: `%s` (Feature: `%s`): `%s` (%s)",
            message.guild.id,
            message.channel.id,
            self.feature_name,
            message.author.display_name,
            ", ".join(
                f"{t.leader_skill_value}/{t.internal_skill_value}/{t.team_power}"
                for t in teams
            ),
        )

        if self.bot.user is not None:
            await add_reaction_if_possible(
                message,
                config.PROCESSING_EMOJI,
                log=self.logger,
            )

        classified_teams = TeamParser.classify_teams(teams)
        team_tuple = classified_teams.as_tuple()
        manager = context.manager

        async with self.sheet_write_lock(context.channel_id):
            metadata = await manager.fetch_google_sheets_metadata()
            manager.log_missing_worksheet_warnings(metadata)

            try:
                metadata = await manager.ensure_worksheets_and_upsert_sheet_config(
                    metadata, count=len(team_tuple)
                )
                await manager.upsert_user_teams(
                    user_info,
                    *team_tuple,
                    metadata=metadata,
                )
                await manager.upsert_user_summary(
                    user_info,
                    message.author.roles if isinstance(message.author, Member) else [],
                    *team_tuple,
                    metadata=metadata,
                )
            except Exception as exc:
                error = partial_success_storage_error(exc)
                if error is None:
                    raise
                raise error from error.__cause__

        if self.bot.user is not None:
            await remove_reaction_if_present(
                message,
                config.PROCESSING_EMOJI,
                self.bot.user,
                log=self.logger,
            )
            await add_reaction_if_possible(message, "✅", log=self.logger)

        return classified_teams

    @app_commands.command(
        name="summary",
        description=(
            "Show and refresh team summary with effective value, user info, and "
            "roles of encore type."
        ),
    )
    @app_commands.check(
        FeatureChannelBase.feature_enabled_app_command_predicate(
            feature_name,
            feature_display_name,
        )
    )
    async def summary(self, interaction: Interaction) -> None:
        source = require_guild_channel_source(
            interaction,
            action="refresh team summary",
        )
        member_by_names = {member.name: member for member in source.guild.members}

        await interaction.response.defer(ephemeral=True)
        operation_context = StorageOperationContext(
            operation="team_register_summary",
            feature_name=self.feature_name,
            guild_id=source.guild.id,
            channel_id=source.channel.id,
        )

        try:
            feature_channel_context = await self._get_feature_channel_context(source)
            context = await self._get_configured_feature_channel_context(
                feature_channel_context
            )
        except Exception as exc:
            storage_error = classify_storage_exception(exc)
            if storage_error is None:
                raise
            await send_storage_error(
                interaction,
                storage_error,
                context=operation_context,
                log=self.logger,
            )
            return

        if context is None:
            await self._send_missing_config_followup(interaction)
            return

        try:
            async with self.sheet_write_lock(context.channel_id):
                metadata = await context.manager.fetch_google_sheets_metadata()
                context.manager.log_missing_worksheet_warnings(metadata)

                try:
                    metadata = (
                        await context.manager.ensure_worksheets_and_upsert_sheet_config(
                            metadata,
                            count=0,  # No teams to process, just refresh summary
                        )
                    )

                    summary_df = await context.manager.refresh_summary_worksheet(
                        metadata,
                        member_by_names=member_by_names,
                    )
                except Exception as exc:
                    storage_error = partial_success_storage_error(exc)
                    if storage_error is None:
                        raise
                    await send_storage_error(
                        interaction,
                        storage_error,
                        context=operation_context,
                        log=self.logger,
                    )
                    return
        except Exception as exc:
            storage_error = classify_storage_exception(exc)
            if storage_error is None:
                raise
            await send_storage_error(
                interaction,
                storage_error,
                context=operation_context,
                log=self.logger,
            )
            return

        if summary_df is None:
            await interaction.followup.send(
                content="No summary worksheet found or no data to display.",
                ephemeral=True,
            )
            return

        await interaction.followup.send(embed=build_summary_embed(summary_df))

    @app_commands.command(
        name="settings",
        description="Show and edit current feature settings for this channel.",
    )
    @app_commands.check(
        FeatureChannelBase.feature_enabled_app_command_predicate(
            feature_name,
            feature_display_name,
        )
    )
    async def settings(self, interaction: Interaction) -> None:
        """Slash command to show and edit current feature settings."""
        await interaction.response.defer(ephemeral=True)
        await self.setup_after_enable(interaction)

    @app_commands.command(
        name="announce_guide",
        description=(
            "Post the team registration guide using configured announcement languages."
        ),
    )
    @app_commands.check(
        FeatureChannelBase.feature_enabled_app_command_predicate(
            feature_name,
            feature_display_name,
        )
    )
    async def announce_guide(self, interaction: Interaction) -> None:
        await self.send_guide_message(interaction)


async def setup(bot: Rhoboto) -> None:
    await bot.add_cog(TeamRegister(bot))
