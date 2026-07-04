from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, override

from discord import Interaction, Member, Message, app_commands

from bot import config
from cogs.base.feature_channel_base import (
    FeatureChannelBase,
    _get_configured_feature_context,
    _get_interaction_channel_context,
)
from components.ui_google_sheets_errors import send_google_sheets_error
from components.ui_team_register import (
    TEAM_REGISTER_DISPLAY_NAME,
    TeamRegisterView,
    build_summary_embed,
    build_team_register_settings_panel,
)
from models.feature_channel import FeatureChannel
from utils.google_sheets_errors import GoogleSheetsError
from utils.key_async_lock import KeyAsyncLock
from utils.reactions import add_reaction_if_possible, remove_reaction_if_present
from utils.team_register_manager import TeamRegisterManager
from utils.team_register_structs import ClassifiedTeams, TeamParser

if TYPE_CHECKING:
    from discord.ui import View

    from bot import Rhoboto
    from components.ui_settings_flow import SettingsPanel


class TeamRegister(
    FeatureChannelBase[TeamRegisterManager, ClassifiedTeams], group_name="team_register"
):
    feature_name = "team_register"
    feature_display_name = TEAM_REGISTER_DISPLAY_NAME
    help_template_key = "team.help"
    lock = KeyAsyncLock()

    ManagerType = TeamRegisterManager

    @override
    def _build_initial_setup_view(self, manager: TeamRegisterManager) -> View:
        return TeamRegisterView(team_register_manager=manager)

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
        )

    @override
    async def process_upsert_from_message(
        self, message: Message
    ) -> ClassifiedTeams | None:
        if not await self._should_process_message(message):
            return None

        self._log_received_message(message)

        user_info = self._message_user_info(message)
        lines = message.content.splitlines()
        parse_result = TeamParser.parse_submission(user_info, lines=lines)
        if parse_result.invalid_attempts:
            await add_reaction_if_possible(
                message,
                config.CONFUSED_EMOJI,
                log=self.logger,
            )
            return None

        teams = parse_result.teams
        if not teams:
            return None

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

        feature_channel = await FeatureChannel.get_or_none(
            guild_id=message.guild.id,
            channel_id=message.channel.id,
            feature_name=self.feature_name,
        )
        if not feature_channel:
            return None

        manager = TeamRegisterManager(
            feature_channel, config.GOOGLE_SERVICE_ACCOUNT_PATH
        )

        team_register_config = await manager.get_sheet_config_or_none()
        if team_register_config is None:
            return None

        if self.bot.user is not None:
            await add_reaction_if_possible(
                message,
                config.PROCESSING_EMOJI,
                log=self.logger,
            )

        classified_teams = TeamParser.classify_teams(teams)
        team_tuple = classified_teams.as_tuple()

        async with self.lock(message.channel.id):
            metadata = await manager.fetch_google_sheets_metadata()
            manager.log_missing_worksheet_warnings(metadata)

            metadata = await manager.ensure_worksheets_and_upsert_sheet_config(
                metadata, count=len(team_tuple)
            )

            await asyncio.gather(
                manager.upsert_user_teams(user_info, *team_tuple, metadata=metadata),
                manager.upsert_user_summary(
                    user_info,
                    message.author.roles if isinstance(message.author, Member) else [],
                    *team_tuple,
                    metadata=metadata,
                ),
            )

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
        FeatureChannelBase.feature_enabled_app_command_predicate(feature_name)
    )
    async def summary(self, interaction: Interaction) -> None:
        interaction_context = _get_interaction_channel_context(interaction)

        await interaction.response.defer(ephemeral=True)

        context = await _get_configured_feature_context(
            interaction,
            feature_name=self.feature_name,
            manager_type=self.ManagerType,
            interaction_context=interaction_context,
        )
        if context is None:
            return

        async with self.lock(context.channel_id):
            try:
                metadata = await context.manager.fetch_google_sheets_metadata()
                context.manager.log_missing_worksheet_warnings(metadata)

                metadata = (
                    await context.manager.ensure_worksheets_and_upsert_sheet_config(
                        metadata,
                        count=0,  # No teams to process, just refresh summary
                    )
                )

                summary_df = await context.manager.refresh_summary_worksheet(
                    metadata,
                    member_by_names={m.name: m for m in context.guild.members},
                )
            except GoogleSheetsError as exc:
                await send_google_sheets_error(interaction, exc)
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
        FeatureChannelBase.feature_enabled_app_command_predicate(feature_name)
    )
    async def settings(self, interaction: Interaction) -> None:
        """Slash command to show and edit current feature settings."""
        await interaction.response.defer(ephemeral=True)
        await self.setup_after_enable(interaction)

    @app_commands.command(
        name="help",
        description="Show the all language how to register your data for this feature.",
    )
    @app_commands.check(
        FeatureChannelBase.feature_enabled_app_command_predicate(feature_name)
    )
    async def help(self, interaction: Interaction) -> None:
        await self._help_callback(interaction)


async def setup(bot: Rhoboto) -> None:
    await bot.add_cog(TeamRegister(bot))
