from __future__ import annotations

from typing import TYPE_CHECKING, override

from discord import app_commands

from bot import config
from cogs.base.discord_context import require_guild_channel_source
from cogs.base.feature_channel_base import (
    FeatureChannelBase,
    _send_public_announcement_followups,
)
from components.ui_shift_register import (
    SHIFT_REGISTER_DISPLAY_NAME,
    ShiftRegisterView,
    build_shift_register_settings_panel,
    get_fresh_shift_register_config_or_respond,
)
from utils.key_async_lock import KeyAsyncLock
from utils.reactions import add_reaction_if_possible, remove_reaction_if_present
from utils.shift_register_manager import ShiftRegisterManager
from utils.shift_register_structs import RecruitmentTimeRanges, Shift, ShiftParser
from utils.shift_register_timeline import (
    build_shift_timeline_template_values,
    render_shift_timeline_announcement_messages,
)
from utils.storage_errors import partial_success_storage_error

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from discord import Interaction, Message
    from discord.ui import View

    from bot import Rhoboto
    from cogs.base.feature_channel_context import ConfiguredFeatureChannelContext
    from components.ui_settings_flow import SettingsPanel
    from utils.structs_base import UserInfo


class ShiftRegister(
    FeatureChannelBase[ShiftRegisterManager, Shift, Shift],
    group_name="shift_register",
):
    feature_name = "shift_register"
    feature_display_name = SHIFT_REGISTER_DISPLAY_NAME
    guide_template_key = "shift.guide"
    auto_guide_template_key = "shift.auto_guide"
    timeline_template_key = "shift.timeline"
    sheet_write_lock = KeyAsyncLock()
    auto_guide_lock = KeyAsyncLock()

    ManagerType = ShiftRegisterManager
    ParserType = ShiftParser

    @override
    def _auto_guide_template_values(
        self,
        context: ConfiguredFeatureChannelContext[ShiftRegisterManager],
        language: str,
    ) -> dict[str, object]:
        values = super()._auto_guide_template_values(context, language)
        feature_config = context.feature_config
        recruitment_ranges = RecruitmentTimeRanges.from_json(
            getattr(feature_config, "recruitment_time_ranges", None)
        )
        values.update(
            build_shift_timeline_template_values(
                language,
                day_number=getattr(feature_config, "day_number", None),
                event_date=getattr(feature_config, "event_date", None),
                recruitment_time_range=recruitment_ranges.announcement_display(),
                submission_deadline_at=getattr(
                    feature_config,
                    "submission_deadline_at",
                    None,
                ),
                draft_shift_proposal_at=getattr(
                    feature_config,
                    "draft_shift_proposal_at",
                    None,
                ),
                final_shift_notice_at=getattr(
                    feature_config,
                    "final_shift_notice_at",
                    None,
                ),
            )
        )
        return values

    @override
    def _build_initial_setup_view(self, manager: ShiftRegisterManager) -> View:
        return ShiftRegisterView(
            shift_register_manager=manager,
            latest_guide_enabled=False,
            latest_guide_toggle_callback=self._toggle_shift_latest_guide,
            latest_guide_state_resolver=self._latest_guide_state_resolver(manager),
            latest_guide_refresh_callback=self._latest_guide_refresh_callback(manager),
        )

    def _latest_guide_state_resolver(
        self,
        manager: ShiftRegisterManager,
    ) -> Callable[[], Awaitable[bool]]:
        async def latest_guide_state_resolver() -> bool:
            return await self._auto_guide_is_enabled(manager.feature_channel)

        return latest_guide_state_resolver

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
            latest_guide_enabled=False,
            latest_guide_toggle_callback=self._toggle_shift_latest_guide,
            latest_guide_state_resolver=self._latest_guide_state_resolver(manager),
            latest_guide_refresh_callback=self._latest_guide_refresh_callback(manager),
        )

    async def _toggle_shift_latest_guide(
        self,
        interaction: Interaction,
        *,
        enabled: bool,
        current_view: View,
    ) -> None:
        shift_register = await get_fresh_shift_register_config_or_respond(
            current_view.shift_register_manager,
            interaction,
        )
        if shift_register is None:
            return

        await self.toggle_auto_guide_from_settings(
            interaction,
            enabled=enabled,
            current_view=current_view,
            feature_config=shift_register,
        )

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

        async with self.sheet_write_lock(message.channel.id):
            metadata = await manager.fetch_google_sheets_metadata()
            manager.log_missing_worksheet_warnings(metadata)

            try:
                metadata = await manager.ensure_worksheets_and_upsert_sheet_config(
                    metadata
                )
                await manager.upsert_or_delete_user_shift(
                    user_info, shift, metadata=metadata
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
        name="announce_timeline",
        description=(
            "Post the shift registration timeline using configured announcement "
            "languages."
        ),
    )
    @app_commands.check(
        FeatureChannelBase.feature_enabled_app_command_predicate(
            feature_name,
            feature_display_name,
        )
    )
    async def announce_timeline(self, interaction: Interaction) -> None:
        await interaction.response.defer(ephemeral=False)

        source = require_guild_channel_source(
            interaction,
            action="post shift registration timeline announcement",
        )
        try:
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
            announcements = await render_shift_timeline_announcement_messages(
                self.timeline_template_key,
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
        except Exception as exc:  # noqa: BLE001
            await self._send_interaction_storage_error_or_raise(
                interaction,
                exc,
                source=source,
                operation="shift_register_announce_timeline",
            )
            return

        await _send_public_announcement_followups(interaction, announcements)

    @app_commands.command(
        name="announce_guide",
        description=(
            "Post the shift registration guide using configured announcement languages."
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
    await bot.add_cog(ShiftRegister(bot))
