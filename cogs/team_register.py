from __future__ import annotations

from typing import TYPE_CHECKING, override

from discord import Interaction, Member, Message, app_commands

from bot import config
from cogs.base.discord_context import require_guild_channel_source
from cogs.base.feature_channel_base import FeatureChannelBase
from components.ui_storage_errors import send_storage_error
from components.ui_team_register import (
    TEAM_REGISTER_DISPLAY_NAME,
    TeamRegisterView,
    build_summary_embed,
    build_team_register_settings_panel,
    get_fresh_team_register_config_or_respond,
)
from utils.key_async_lock import KeyAsyncLock
from utils.reactions import add_reaction_if_possible, transition_processing_reaction
from utils.storage_errors import (
    StorageOperationContext,
    classify_storage_exception,
)
from utils.team_register_manager import (
    TEAM_REGISTER_SHEET_WRITE_LOCK,
    TeamRegisterManager,
    fresh_team_channel_transaction,
)
from utils.team_register_structs import ClassifiedTeams, Team, TeamParser

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from discord.ui import View

    from bot import Rhoboto
    from cogs.base.feature_channel_context import ConfiguredFeatureChannelContext
    from components.ui_settings_flow import SettingsPanel
    from utils.structs_base import UserInfo


class TeamRegister(
    FeatureChannelBase[TeamRegisterManager, list[Team], ClassifiedTeams],
    group_name="team_register",
):
    feature_name = "team_register"
    feature_display_name = TEAM_REGISTER_DISPLAY_NAME
    guide_template_key = "team.guide"
    auto_guide_template_key = "team.auto_guide"
    sheet_write_lock = TEAM_REGISTER_SHEET_WRITE_LOCK
    auto_guide_lock = KeyAsyncLock()

    ManagerType = TeamRegisterManager
    ParserType = TeamParser

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
    async def _process_configured_message_submission(
        self,
        message: Message,
        context: ConfiguredFeatureChannelContext[TeamRegisterManager],
        submission: list[Team],
        user_info: UserInfo,
    ) -> ClassifiedTeams | None:
        teams = submission
        self.logger.info(
            (
                "Parsed Team Register submission. operation=team_register_parse "
                "feature=%s guild=%s channel=%s message=%s teams=%s"
            ),
            self.feature_name,
            message.guild.id,
            message.channel.id,
            message.id,
            len(teams),
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

        async with fresh_team_channel_transaction(
            manager,
            self.sheet_write_lock,
            channel_id=context.channel_id,
        ):
            await manager.upsert_user_registration(
                user_info,
                message.author.roles if isinstance(message.author, Member) else [],
                *team_tuple,
            )

        await transition_processing_reaction(
            message,
            ("✅",),
            processing_emoji=config.PROCESSING_EMOJI,
            user=self.bot.user,
            log=self.logger,
        )

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
            async with fresh_team_channel_transaction(
                context.manager,
                self.sheet_write_lock,
                channel_id=context.channel_id,
            ):
                summary_df = await context.manager.refresh_summary_registration(
                    member_by_names=member_by_names,
                )
        except Exception as exc:  # noqa: BLE001
            await self._send_interaction_storage_error_or_raise(
                interaction,
                exc,
                source=source,
                operation="team_register_summary",
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
