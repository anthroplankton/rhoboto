from __future__ import annotations

from io import BytesIO
from typing import TYPE_CHECKING, override

from discord import File, app_commands
from discord.utils import escape_markdown

from bot import config
from cogs.base.discord_context import require_guild_channel_source
from cogs.base.feature_channel_base import (
    FeatureChannelBase,
    _send_public_announcement_followups,
)
from components.ui_shift_register import (
    SHIFT_REGISTER_DISPLAY_NAME,
    GenerateDraftConfirmView,
    ShiftRegisterView,
    build_shift_register_settings_panel,
    get_fresh_shift_register_config_or_respond,
)
from utils.google_sheets_urls import google_sheet_url_with_gid
from utils.key_async_lock import KeyAsyncLock
from utils.reactions import add_reaction_if_possible, transition_processing_reaction
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


def _format_generate_draft_confirmation(
    recruitment_ranges: RecruitmentTimeRanges,
    draft_sheet_url: str,
) -> str:
    ranges = recruitment_ranges.ranges.ranges
    final_row = ranges[-1].end - ranges[0].start + 1
    return "\n".join(
        [
            "### ‼️ 確認產生班表草稿",
            (
                "請先備份需要保留的內容。確認後將覆蓋 "
                f"[Shift Draft]({draft_sheet_url}) 的以下位置："  # noqa: RUF001
            ),
            "- 班表：`A1:G31`",  # noqa: RUF001
            f"- Notes：`A{final_row + 2}`",  # noqa: RUF001
            (
                f"- 候補：`I1`、閾值・圖例 "  # noqa: RUF001
                f"`I{final_row + 1}:M{final_row + 1}`"
            ),
            f"- 反查：`J{final_row + 3}:L{final_row + 5}`",  # noqa: RUF001
            (
                f"- 編成一覧：Team Source 可用時從 `J{final_row + 6}` 寫入"  # noqa: RUF001
            ),
            "",
            (
                "Notes・候補的展開位置若已有資料，將保留該資料並可能顯示 "  # noqa: RUF001
                "`#REF!`。"
            ),
        ]
    )


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
            recruitment_ranges,
        )

    async def _write_shift_registration(
        self,
        message: Message,
        user_info: UserInfo,
        shift: Shift,
        manager: ShiftRegisterManager,
        recruitment_ranges: RecruitmentTimeRanges,
    ) -> Shift:
        self.logger.info(
            (
                "Parsed Shift Register submission. operation=shift_register_parse "
                "feature=%s guild=%s channel=%s message=%s slots=%s"
            ),
            self.feature_name,
            message.guild.id,
            message.channel.id,
            message.id,
            len(set(shift)),
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
                    user_info,
                    shift,
                    metadata=metadata,
                    recruitment_ranges=recruitment_ranges,
                )
            except Exception as exc:
                error = partial_success_storage_error(exc)
                if error is None:
                    raise
                raise error from error.__cause__

        await transition_processing_reaction(
            message,
            ("✅",),
            processing_emoji=config.PROCESSING_EMOJI,
            user=self.bot.user,
            log=self.logger,
        )

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
        encore_power_threshold=(
            "Minimum Team Power required for Encore; Power must be greater than it."
        ),
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
        encore_power_threshold: app_commands.Range[float, 0],
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

            recruitment_ranges = RecruitmentTimeRanges.from_json(
                context.feature_config.recruitment_time_ranges
            )
            draft_sheet_url = google_sheet_url_with_gid(
                context.feature_config.sheet_url,
                context.feature_config.draft_worksheet_id,
            )
            confirmation_content = _format_generate_draft_confirmation(
                recruitment_ranges,
                draft_sheet_url,
            )
            view = GenerateDraftConfirmView(
                requesting_user_id=interaction.user.id,
                draft_sheet_url=draft_sheet_url,
            )
            await interaction.edit_original_response(
                content=confirmation_content,
                view=view,
            )
            await view.wait()
            if view.value is False:
                await interaction.edit_original_response(view=None)
                return
            if view.value is None:
                await interaction.edit_original_response(
                    content="✖️ 確認逾時，未變更 Shift Draft。",  # noqa: RUF001
                    view=None,
                )
                return

            feature_channel_context = await self._get_feature_channel_context(source)
            context = await self._get_configured_feature_channel_context(
                feature_channel_context
            )
            if context is None:
                await self._send_missing_config_followup(interaction)
                return
            fresh_ranges = RecruitmentTimeRanges.from_json(
                context.feature_config.recruitment_time_ranges
            )
            fresh_draft_sheet_url = google_sheet_url_with_gid(
                context.feature_config.sheet_url,
                context.feature_config.draft_worksheet_id,
            )
            if (
                _format_generate_draft_confirmation(
                    fresh_ranges,
                    fresh_draft_sheet_url,
                )
                != confirmation_content
            ):
                await interaction.edit_original_response(
                    content=(
                        "⚠️ 募集時段設定已變更，未變更 Shift Draft；"  # noqa: RUF001
                        "請重新執行 command。"
                    ),
                    view=None,
                )
                return

            async with self.sheet_write_lock(source.channel.id):
                metadata = await context.manager.fetch_google_sheets_metadata()
                context.manager.log_missing_worksheet_warnings(metadata)
                metadata = (
                    await context.manager.ensure_worksheets_and_upsert_sheet_config(
                        metadata
                    )
                )
                result = await context.manager.generate_draft(
                    metadata,
                    encore_power_threshold=float(encore_power_threshold),
                    runner=runner,
                )
                schedule = result.schedule
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
            self._format_draft_report(
                schedule,
                draft_sheet_url,
                member_mentions,
                encore_power_threshold=float(encore_power_threshold),
                recruitment_ranges=result.recruitment_ranges,
                team_source_warning=result.team_source_warning,
                unregistered_usernames=result.unregistered_usernames,
            ),
            file=File(
                BytesIO(result.notes_snapshot.encode("utf-8")),
                filename="shift-draft-notes.txt",
            ),
            ephemeral=True,
        )

    @staticmethod
    def _format_draft_report(
        schedule: DraftSchedule,
        draft_sheet_url: str,
        member_mentions: dict[str, str],
        *,
        encore_power_threshold: float,
        recruitment_ranges: RecruitmentTimeRanges,
        team_source_warning: str | None,
        unregistered_usernames: tuple[str, ...] = (),
    ) -> str:
        """Format the generated draft report.

        The report always shows encore, 本走, and standby in that order. Empty
        slots display their per-group shortage so each role remains visible.
        """
        report_assignments = [
            assignment
            for assignment in schedule.assignments
            if recruitment_ranges.contains_slots({assignment.hour})
        ]
        lines = [
            "### ✅ 班表草稿已產生",
            f"- Runner（ランナー）：{schedule.runner or '`Not set`'}",  # noqa: RUF001
            f"- 安可綜合力閾值：{encore_power_threshold:g}",  # noqa: RUF001
            (
                f"‼️ 已將班表寫入 [Shift Draft]({draft_sheet_url})，並覆蓋原有內容。"  # noqa: RUF001
            ),
        ]
        if team_source_warning is not None:
            lines.append(team_source_warning)
        if unregistered_usernames:
            lines.append(
                "⚠️ 編成未登録："  # noqa: RUF001
                + "、".join(
                    _format_draft_username(username, schedule, member_mentions)
                    for username in unregistered_usernames
                )
            )
        lines.append(f"- 募集時間【{recruitment_ranges.announcement_display()}】")
        lines.append("- 已排入（安可｜本走；待機）：")  # noqa: RUF001
        if not schedule.display_names:
            lines[-1] += "なし"
            report_assignments = []
        for assignment in report_assignments:
            encore_username = assignment.supporter_usernames_by_slot.get(
                ENCORE_SUPPORTER_SLOT
            )
            encore_name = (
                _format_draft_username(encore_username, schedule, member_mentions)
                if encore_username is not None
                else None
            )
            main_name_parts = [
                _format_draft_username(
                    assignment.supporter_usernames_by_slot[supporter_slot],
                    schedule,
                    member_mentions,
                )
                for supporter_slot in HONSO_SUPPORTER_SLOTS
                if supporter_slot in assignment.supporter_usernames_by_slot
            ]
            missing_main_count = len(HONSO_SUPPORTER_SLOTS) - len(main_name_parts)
            if missing_main_count:
                main_name_parts.append(f"缺 `{missing_main_count}`")
            main_names = "、".join(main_name_parts)
            standby_username = assignment.supporter_usernames_by_slot.get(
                STANDBY_SUPPORTER_SLOT
            )
            standby_name = (
                _format_draft_username(standby_username, schedule, member_mentions)
                if standby_username is not None
                else None
            )
            names = f"{encore_name or '缺'}｜{main_names}；{standby_name or '缺'}"  # noqa: RUF001
            lines.append(
                f"  - -# `{hour_label(assignment.hour)}`：{names}"  # noqa: RUF001
            )
        unassigned_assignments = [
            assignment
            for assignment in report_assignments
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
        lines.append(
            "附件是生成時資料的 Notes 快照，不會隨 Sheet 調整更新。"  # noqa: RUF001
        )
        return "\n".join(lines)


async def setup(bot: Rhoboto) -> None:
    await bot.add_cog(ShiftRegister(bot))
