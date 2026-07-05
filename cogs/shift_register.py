from __future__ import annotations

from typing import TYPE_CHECKING, override

from discord import app_commands

from bot import config
from cogs.base.discord_context import require_guild_channel_source
from cogs.base.feature_channel_base import (
    FeatureChannelBase,
    _send_public_announcement_followups,
)
from cogs.base.feature_channel_context import (
    ConfiguredFeatureChannelContext,
    MessageParseResult,
)
from components.ui_shift_register import (
    SHIFT_REGISTER_DISPLAY_NAME,
    ShiftRegisterView,
    build_shift_register_settings_panel,
)
from utils.key_async_lock import KeyAsyncLock
from utils.reactions import add_reaction_if_possible, remove_reaction_if_present
from utils.shift_register_manager import ShiftRegisterManager
from utils.shift_register_structs import RecruitmentTimeRanges, Shift, ShiftParser
from utils.shift_register_timeline import render_shift_info_announcement_messages

if TYPE_CHECKING:
    from discord import Interaction, Message
    from discord.ui import View

    from bot import Rhoboto
    from components.ui_settings_flow import SettingsPanel
    from utils.structs_base import UserInfo


class ShiftRegister(
    FeatureChannelBase[ShiftRegisterManager, Shift, Shift],
    group_name="shift_register",
):
    feature_name = "shift_register"
    feature_display_name = SHIFT_REGISTER_DISPLAY_NAME
    help_template_key = "shift.help"
    info_template_key = "shift.info"
    lock = KeyAsyncLock()

    ManagerType = ShiftRegisterManager

    @override
    def _build_initial_setup_view(self, manager: ShiftRegisterManager) -> View:
        return ShiftRegisterView(shift_register_manager=manager)

    @override
    async def _build_settings_panel(
        self,
        _interaction: Interaction,
        manager: ShiftRegisterManager,
        sheet_config: object,
    ) -> SettingsPanel:
        return await build_shift_register_settings_panel(
            manager,
            sheet_config,
        )

    @override
    async def _parse_message_submission(
        self,
        message: Message,
    ) -> MessageParseResult[Shift]:
        user_info = self._message_user_info(message)
        lines = message.content.splitlines()
        parse_result = ShiftParser.parse_lines(user_info, lines)
        if parse_result.invalid_attempts:
            return MessageParseResult.invalid(user_info=user_info)

        shift = parse_result.shift
        if shift is None:
            return MessageParseResult.ignored()

        return MessageParseResult.parsed(shift, user_info=user_info)

    @override
    async def _process_configured_message_submission(
        self,
        message: Message,
        context: ConfiguredFeatureChannelContext[ShiftRegisterManager],
        submission: Shift,
        user_info: UserInfo,
    ) -> Shift | None:
        shift = submission
        recruitment_ranges = RecruitmentTimeRanges.from_json(
            getattr(context.feature_config, "recruitment_time_ranges", None)
        )
        if not recruitment_ranges.contains_slots(set(shift)):
            await self._add_invalid_registration_reactions(message)
            return None

        return await self._write_shift_registration(
            message,
            user_info,
            shift,
            context.manager,
        )

    async def _write_shift_registration(
        self,
        message: Message,
        user_info: UserInfo,
        shift: Shift,
        manager: ShiftRegisterManager,
    ) -> Shift:
        self.logger.info(
            "Parsed shift in Guild: `%s` Channel: `%s` (Feature: `%s`): `%s` (%r)",
            message.guild.id,
            message.channel.id,
            self.feature_name,
            message.author.display_name,
            shift,
        )

        if self.bot.user is not None:
            await add_reaction_if_possible(
                message,
                config.PROCESSING_EMOJI,
                log=self.logger,
            )

        async with self.lock(message.channel.id):
            metadata = await manager.fetch_google_sheets_metadata()
            manager.log_missing_worksheet_warnings(metadata)

            metadata = await manager.ensure_worksheets_and_upsert_sheet_config(metadata)

            await manager.upsert_or_delete_user_shift(
                user_info, shift, metadata=metadata
            )

        if self.bot.user is not None:
            await remove_reaction_if_present(
                message,
                config.PROCESSING_EMOJI,
                self.bot.user,
                log=self.logger,
            )
            await add_reaction_if_possible(message, "✅", log=self.logger)

        return shift

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
        name="info",
    )
    @app_commands.check(
        FeatureChannelBase.feature_enabled_app_command_predicate(
            feature_name,
            feature_display_name,
        )
    )
    async def info(self, interaction: Interaction) -> None:
        await interaction.response.defer(ephemeral=False)

        source = require_guild_channel_source(
            interaction,
            action="show shift info",
        )
        feature_channel_context = await self._get_feature_channel_context(source)
        context = await self._get_configured_feature_channel_context(
            feature_channel_context
        )
        if context is None:
            await self._send_missing_config_followup(interaction)
            return

        recruitment_ranges = RecruitmentTimeRanges.from_json(
            getattr(context.feature_config, "recruitment_time_ranges", None)
        )
        announcements = await render_shift_info_announcement_messages(
            self.info_template_key,
            context.guild_id,
            self.logger,
            day_number=getattr(context.feature_config, "day_number", None),
            event_date=getattr(context.feature_config, "event_date", None),
            recruitment_time_range=recruitment_ranges.announcement_display(),
            submission_deadline_at=getattr(
                context.feature_config,
                "submission_deadline_at",
                None,
            ),
            draft_shift_proposal_at=getattr(
                context.feature_config,
                "draft_shift_proposal_at",
                None,
            ),
            final_shift_notice_at=getattr(
                context.feature_config,
                "final_shift_notice_at",
                None,
            ),
        )
        await _send_public_announcement_followups(interaction, announcements)

    @app_commands.command(
        name="help",
        description="Show the all language how to register your data for this feature.",
    )
    @app_commands.check(
        FeatureChannelBase.feature_enabled_app_command_predicate(
            feature_name,
            feature_display_name,
        )
    )
    async def help(self, interaction: Interaction) -> None:
        await self._help_callback(interaction)


async def setup(bot: Rhoboto) -> None:
    await bot.add_cog(ShiftRegister(bot))
