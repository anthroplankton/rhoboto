from __future__ import annotations

from typing import TYPE_CHECKING, override

from discord import app_commands
from discord.utils import escape_markdown

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
from utils.google_sheets_urls import google_sheet_url_with_gid
from utils.key_async_lock import KeyAsyncLock
from utils.reactions import add_reaction_if_possible, remove_reaction_if_present
from utils.shift_register_manager import (
    SHIFT_REGISTER_SHEET_WRITE_LOCK,
    ShiftRegisterManager,
)
from utils.shift_register_structs import RecruitmentTimeRanges, Shift, ShiftParser
from utils.shift_register_timeline import (
    build_shift_timeline_template_values,
    render_shift_timeline_announcement_messages,
)
from utils.shift_scheduler import (
    ENCORE_SUPPORTER_SLOT,
    HONSO_SUPPORTER_SLOTS,
    STANDBY_SUPPORTER_SLOT,
    hour_label,
)
from utils.storage_errors import partial_success_storage_error

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from discord import Interaction, Message
    from discord.ui import View

    from bot import Rhoboto
    from cogs.base.feature_channel_context import ConfiguredFeatureChannelContext
    from components.ui_settings_flow import SettingsPanel
    from utils.shift_scheduler import DraftSchedule
    from utils.structs_base import UserInfo


def _format_display_name(name: str) -> str:
    return escape_markdown(name) if "`" in name else f"`{name}`"


def _format_draft_username(
    username: str,
    schedule: DraftSchedule,
    member_mentions: dict[str, str],
) -> str:
    return member_mentions.get(
        username,
        _format_display_name(schedule.display_names.get(username, username)),
    )


class ShiftRegister(
    FeatureChannelBase[ShiftRegisterManager, Shift, Shift],
    group_name="shift_register",
):
    feature_name = "shift_register"
    feature_display_name = SHIFT_REGISTER_DISPLAY_NAME
    guide_template_key = "shift.guide"
    auto_guide_template_key = "shift.auto_guide"
    timeline_template_key = "shift.timeline"
    sheet_write_lock = SHIFT_REGISTER_SHEET_WRITE_LOCK
    auto_guide_lock = KeyAsyncLock()

    ManagerType = ShiftRegisterManager
    ParserType = ShiftParser

    @override
    async def _guide_template_values(
        self,
        context: ConfiguredFeatureChannelContext[ShiftRegisterManager],
    ) -> dict[str, object]:
        values = await super()._guide_template_values(context)
        values[
            "team_source_channel_id"
        ] = await context.manager.get_saved_team_source_channel_id()
        return values

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

    @app_commands.command(
        name="generate_draft",
        description=(
            "Build the draft shift schedule from entries into the draft worksheet."
        ),
    )
    @app_commands.describe(
        runner="Nickname pinned to the runner (ランナー) lane for every hour.",
    )
    @app_commands.check(
        FeatureChannelBase.feature_enabled_app_command_predicate(
            feature_name,
            feature_display_name,
        )
    )
    async def generate_draft(
        self,
        interaction: Interaction,
        runner: str | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        source = require_guild_channel_source(
            interaction,
            action="generate shift draft schedule",
        )
        try:
            feature_channel_context = await self._get_feature_channel_context(source)
            context = await self._get_configured_feature_channel_context(
                feature_channel_context
            )
            if context is None:
                await self._send_missing_config_followup(interaction)
                return

            async with self.sheet_write_lock(source.channel.id):
                metadata = await context.manager.fetch_google_sheets_metadata()
                context.manager.log_missing_worksheet_warnings(metadata)
                metadata = (
                    await context.manager.ensure_worksheets_and_upsert_sheet_config(
                        metadata
                    )
                )
                schedule = await context.manager.generate_draft(metadata, runner=runner)
                draft_sheet_url = google_sheet_url_with_gid(
                    metadata.sheet_url,
                    metadata.draft_worksheet.id,
                )
                member_mentions = {
                    member.name: member.mention for member in source.guild.members
                }
        except Exception as exc:  # noqa: BLE001
            await self._send_interaction_storage_error_or_raise(
                interaction,
                exc,
                source=source,
                operation="shift_register_generate_draft",
            )
            return

        await interaction.followup.send(
            self._format_draft_report(schedule, draft_sheet_url, member_mentions),
            ephemeral=True,
        )

    @staticmethod
    def _format_draft_report(
        schedule: DraftSchedule,
        draft_sheet_url: str,
        member_mentions: dict[str, str],
    ) -> str:
        """Format the generated draft report.

        Assigned slots use a full-width vertical bar between encore and 本走,
        a full-width semicolon before standby, and ``、`` between users in the
        same supporter group. Missing groups omit their separator; ``No encore``
        appears only when another supporter is assigned.
        """
        lines = [
            "### ✅ 班表草稿已產生",
            f"- Runner（ランナー）：{schedule.runner or '`Not set`'}",  # noqa: RUF001
            (
                f"- ‼️ 已將 `{len(schedule.hours)}` 個小時的班表寫入 "
                f"[Shift Draft]({draft_sheet_url})，並覆蓋原有內容。"  # noqa: RUF001
            ),
        ]
        lines.append("- 已排入：")  # noqa: RUF001
        for assignment in schedule.assignments:
            encore_username = assignment.supporter_usernames_by_slot.get(
                ENCORE_SUPPORTER_SLOT
            )
            encore_name = (
                _format_draft_username(encore_username, schedule, member_mentions)
                if encore_username is not None
                else None
            )
            main_names = "、".join(
                _format_draft_username(
                    assignment.supporter_usernames_by_slot[supporter_slot],
                    schedule,
                    member_mentions,
                )
                for supporter_slot in HONSO_SUPPORTER_SLOTS
                if supporter_slot in assignment.supporter_usernames_by_slot
            )
            standby_username = assignment.supporter_usernames_by_slot.get(
                STANDBY_SUPPORTER_SLOT
            )
            standby_name = (
                _format_draft_username(standby_username, schedule, member_mentions)
                if standby_username is not None
                else None
            )
            if encore_name and main_names:
                names = f"{encore_name} ｜ {main_names}"  # noqa: RUF001
            elif encore_name:
                names = encore_name
            elif main_names:
                names = f"`No encore` ｜ {main_names}"  # noqa: RUF001
            else:
                names = ""
            if standby_name:
                names = (
                    f"{names}；{standby_name}"  # noqa: RUF001
                    if names
                    else f"`No encore`；{standby_name}"  # noqa: RUF001
                )
            shortage = (
                f"（缺 `{assignment.shortage}`）" if assignment.shortage else ""  # noqa: RUF001
            )
            names = f"：{names}" if names else ""  # noqa: RUF001
            lines.append(f"  - -# `{hour_label(assignment.hour)}`{shortage}{names}")
        unassigned_assignments = [
            assignment
            for assignment in schedule.assignments
            if assignment.unassigned_usernames
        ]
        if unassigned_assignments:
            lines.append("- 未排入（位置已滿）：")  # noqa: RUF001
            lines.extend(
                f"  - -# `{hour_label(assignment.hour)}`："  # noqa: RUF001
                + "、".join(
                    _format_draft_username(username, schedule, member_mentions)
                    for username in assignment.unassigned_usernames
                )
                for assignment in unassigned_assignments
            )
        return "\n".join(lines)


async def setup(bot: Rhoboto) -> None:
    await bot.add_cog(ShiftRegister(bot))
