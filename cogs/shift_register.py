from __future__ import annotations

from typing import TYPE_CHECKING, override

from discord import app_commands

from bot import config
from cogs.base.feature_channel_base import (
    FeatureChannelBase,
    _get_configured_feature_context,
    _send_public_announcement_followups,
)
from components.ui_google_sheets_errors import send_google_sheets_error
from components.ui_settings_flow import (
    send_current_panel_followup,
    send_settings_view_followup,
)
from components.ui_shift_register import (
    ShiftRegisterView,
    build_shift_register_settings_panel,
)
from models.feature_channel import FeatureChannel
from utils.google_sheets_errors import GoogleSheetsError
from utils.key_async_lock import KeyAsyncLock
from utils.reactions import add_reaction_if_possible, remove_reaction_if_present
from utils.shift_register_manager import ShiftRegisterManager
from utils.shift_register_structs import RecruitmentTimeRanges, Shift, ShiftParser
from utils.shift_register_timeline import render_shift_info_announcement_messages

if TYPE_CHECKING:
    from discord import Interaction, Message

    from bot import Rhoboto
    from utils.structs_base import UserInfo


class ShiftRegister(
    FeatureChannelBase[ShiftRegisterManager, Shift],
    group_name="shift_register",
):
    feature_name = "shift_register"
    help_template_key = "shift.help"
    info_template_key = "shift.info"
    lock = KeyAsyncLock()

    ManagerType = ShiftRegisterManager

    async def setup_after_enable(self, interaction: Interaction) -> None:
        if interaction.channel is None or interaction.guild is None:
            msg = (
                "Interaction channel or guild is None. "
                "Cannot proceed with setup message."
            )
            raise ValueError(msg)
        guild_id = interaction.guild.id
        channel_id = interaction.channel.id
        feature_channel = await FeatureChannel.get(
            guild_id=guild_id,
            channel_id=channel_id,
            feature_name=self.feature_name,
        )

        manager = ShiftRegisterManager(
            feature_channel, config.GOOGLE_SERVICE_ACCOUNT_PATH
        )

        shift_register_config = await manager.get_sheet_config_or_none()
        if shift_register_config is None:
            content = (
                "Shift Register is not yet configured for this channel. "
                "Click below to set up."
            )
            view = ShiftRegisterView(shift_register_manager=manager)
            await send_settings_view_followup(
                interaction,
                content=content,
                view=view,
            )
            return

        try:
            panel = await build_shift_register_settings_panel(
                manager,
                shift_register_config,
            )
        except GoogleSheetsError as exc:
            await send_google_sheets_error(interaction, exc)
            return

        await send_current_panel_followup(interaction, panel)

    @override
    async def process_upsert_from_message(self, message: Message) -> Shift | None:
        """
        Listen for messages to provide a button for shift register setup/edit.
        This is used in channels where the feature is enabled.
        """
        if not await self._should_process_message(message):
            return None

        self._log_received_message(message)

        user_info = self._message_user_info(message)
        lines = message.content.splitlines()
        parse_result = ShiftParser.parse_lines(user_info, lines)
        if parse_result.invalid_attempts:
            await add_reaction_if_possible(
                message,
                config.CONFUSED_EMOJI,
                log=self.logger,
            )
            return None

        shift = parse_result.shift
        if shift is None:
            return None

        feature_channel = await FeatureChannel.get_or_none(
            guild_id=message.guild.id,
            channel_id=message.channel.id,
            feature_name=self.feature_name,
        )
        manager = None
        shift_register_config = None
        if feature_channel:
            manager = ShiftRegisterManager(
                feature_channel, config.GOOGLE_SERVICE_ACCOUNT_PATH
            )
            shift_register_config = await manager.get_sheet_config_or_none()
        if manager is None or shift_register_config is None:
            return None

        recruitment_ranges = RecruitmentTimeRanges.from_json(
            getattr(shift_register_config, "recruitment_time_ranges", None)
        )
        if not recruitment_ranges.contains_slots(set(shift)):
            await add_reaction_if_possible(
                message,
                config.CONFUSED_EMOJI,
                log=self.logger,
            )
            return None

        return await self._write_shift_registration(
            message,
            user_info,
            shift,
            manager,
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
        FeatureChannelBase.feature_enabled_app_command_predicate(feature_name)
    )
    async def settings(self, interaction: Interaction) -> None:
        """Slash command to show and edit current feature settings."""
        await interaction.response.defer(ephemeral=True)
        await self.setup_after_enable(interaction)

    @app_commands.command(
        name="info",
    )
    @app_commands.check(
        FeatureChannelBase.feature_enabled_app_command_predicate(feature_name)
    )
    async def info(self, interaction: Interaction) -> None:
        await interaction.response.defer(ephemeral=False)

        context = await _get_configured_feature_context(
            interaction,
            feature_name=self.feature_name,
            manager_type=self.ManagerType,
        )
        if context is None:
            return

        recruitment_ranges = RecruitmentTimeRanges.from_json(
            getattr(context.sheet_config, "recruitment_time_ranges", None)
        )
        announcements = await render_shift_info_announcement_messages(
            self.info_template_key,
            context.guild_id,
            self.logger,
            day_number=getattr(context.sheet_config, "day_number", None),
            event_date=getattr(context.sheet_config, "event_date", None),
            recruitment_time_range=recruitment_ranges.announcement_display(),
            submission_deadline_at=getattr(
                context.sheet_config,
                "submission_deadline_at",
                None,
            ),
            draft_shift_proposal_at=getattr(
                context.sheet_config,
                "draft_shift_proposal_at",
                None,
            ),
            final_shift_notice_at=getattr(
                context.sheet_config,
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
        FeatureChannelBase.feature_enabled_app_command_predicate(feature_name)
    )
    async def help(self, interaction: Interaction) -> None:
        await self._help_callback(interaction)


async def setup(bot: Rhoboto) -> None:
    await bot.add_cog(ShiftRegister(bot))
