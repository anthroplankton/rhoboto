from __future__ import annotations

import calendar
from typing import TYPE_CHECKING, override

from discord import app_commands

from bot import config
from cogs.base.feature_channel_base import FeatureChannelBase
from components.ui_google_sheets_errors import send_google_sheets_error
from components.ui_settings_flow import send_current_panel_followup
from components.ui_shift_register import (
    ShiftRegisterView,
    build_shift_register_settings_panel,
)
from models.feature_channel import FeatureChannel
from utils.google_sheets_errors import GoogleSheetsError
from utils.key_async_lock import KeyAsyncLock
from utils.message_templates import render_message_template
from utils.reactions import add_reaction_if_possible, remove_reaction_if_present
from utils.shift_register_manager import ShiftRegisterManager
from utils.shift_register_structs import Period, Shift, ShiftParser

if TYPE_CHECKING:
    from discord import Interaction, Message

    from bot import Rhoboto


class ShiftRegister(
    FeatureChannelBase[ShiftRegisterManager, Shift | list[Period]],
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
            await interaction.followup.send(content=content, view=view, ephemeral=True)
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
    async def process_upsert_from_message(
        self, message: Message
    ) -> Shift | list[Period] | None:
        """
        Listen for messages to provide a button for shift register setup/edit.
        This is used in channels where the feature is enabled.
        """
        if not await self._should_process_message(message):
            return None

        self._log_received_message(message)

        user_info = self._message_user_info(message)
        lines = message.content.splitlines()
        shift, periods = ShiftParser.parse_lines(user_info, lines)
        if not periods:
            if ShiftParser.looks_like_invalid_attempt(lines):
                await add_reaction_if_possible(
                    message,
                    config.CONFUSED_EMOJI,
                    log=self.logger,
                )
            return None

        self.logger.info(
            "Parsed shift in Guild: `%s` Channel: `%s` (Feature: `%s`): `%s` (%r)",
            message.guild.id,
            message.channel.id,
            self.feature_name,
            message.author.display_name,
            shift,
        )

        if not shift:
            await add_reaction_if_possible(
                message,
                config.CONFUSED_EMOJI,
                log=self.logger,
            )
            return periods

        feature_channel = await FeatureChannel.get_or_none(
            guild_id=message.guild.id,
            channel_id=message.channel.id,
            feature_name=self.feature_name,
        )
        if not feature_channel:
            return None

        manager = ShiftRegisterManager(
            feature_channel, config.GOOGLE_SERVICE_ACCOUNT_PATH
        )

        shift_register_config = await manager.get_sheet_config_or_none()
        if shift_register_config is None:
            return None

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
    async def info(
        self,
        interaction: Interaction,
        day_number: int,
        month: int,
        day: int,
        deadline_day: int,
        deadline_hour: int,
        draft_day: int,
        draft_hour: int,
        final_day: int,
        final_hour: int,
    ) -> None:
        if interaction.channel is None or interaction.guild is None:
            msg = (
                "Interaction channel or guild is None. "
                "Cannot proceed with help command."
            )
            raise ValueError(msg)

        await interaction.response.defer(ephemeral=False)

        feature_channel = await FeatureChannel.get(
            guild_id=interaction.guild.id,
            channel_id=interaction.channel.id,
            feature_name=self.feature_name,
        )

        manager = self.ManagerType(feature_channel, config.GOOGLE_SERVICE_ACCOUNT_PATH)

        sheet_config = await manager.get_sheet_config_or_none()
        if sheet_config is None:
            await interaction.followup.send(
                content=f"`{self.feature_name}` is not configured for this channel.",
                ephemeral=True,
            )
            return

        month_name = calendar.month_name[month]
        await interaction.followup.send(
            render_message_template(
                self.info_template_key,
                "ja",
                bot=self.bot.user.mention if self.bot.user is not None else "@bot",
                day_number=day_number,
                month_name=month_name,
                month=month,
                day=day,
                deadline_day=deadline_day,
                deadline_hour=deadline_hour,
                draft_day=draft_day,
                draft_hour=draft_hour,
                final_day=final_day,
                final_hour=final_hour,
                sheet_url=sheet_config.sheet_url,
            ),
            ephemeral=False,
        )

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
