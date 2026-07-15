from __future__ import annotations

# ruff: noqa: RUF001
import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from io import BytesIO
from typing import TYPE_CHECKING, override

from discord import (
    File,
    Forbidden,
    HTTPException,
    Role,
    TextChannel,
    Thread,
    User,
    app_commands,
)
from discord.utils import escape_markdown, escape_mentions

from bot import config
from cogs.base.discord_context import require_guild_channel_source
from cogs.base.feature_channel_base import FeatureChannelBase
from cogs.base.register_feature_channel_base import RegisterFeatureChannelBase
from cogs.base.register_feature_channel_context import (
    ConfiguredRegisterFeatureChannelContext,
    RegisterFeatureChannelContext,
)
from components.ui_settings_flow import prepare_replacement_settings_view
from components.ui_shift_register import (
    AUTO_CLOSE_INVALIDATED_MESSAGE,
    SHIFT_REGISTER_DISPLAY_NAME,
    AssignScheduleRoleConfirmView,
    GenerateShiftDraftConfirmView,
    GenerateShiftScheduleConfirmView,
    ScheduleRoleDecision,
    ShiftAutoCloseCallbacks,
    ShiftDeadlineCloseView,
    ShiftRegisterView,
    build_shift_register_settings_panel,
    get_fresh_shift_register_config_or_respond,
)
from models.feature_channel import FeatureChannel
from models.shift_register import ShiftRegisterConfig
from models.shift_timeline_event_state import (
    ShiftTimelineEventKind,
    ShiftTimelineEventState,
    ShiftTimelineEventStatus,
)
from utils.announcement_languages import ANNOUNCEMENT_RENDER_FAILURE_MESSAGE
from utils.google_sheets_urls import google_sheet_url_with_gid
from utils.key_async_lock import KeyAsyncLock
from utils.manager_base import SheetConfigNotFoundError
from utils.reactions import add_reaction_if_possible, transition_processing_reaction
from utils.shift_final import (
    DEFAULT_EVENT_DAY_FORMAT,
    A1Rectangle,
    EventDayWriteStatus,
    FinalScheduleConflictError,
    FinalScheduleInputError,
    FinalScheduleValidationError,
    FinalScheduleValidationKind,
    ScheduleUpdateRequest,
    build_schedule_update_request,
    parse_a1_range,
)
from utils.shift_register_manager import (
    SHIFT_REGISTER_SHEET_WRITE_LOCK,
    AutoCloseDeadlineNotFutureError,
    FinalScheduleImageRangeError,
    FinalScheduleReconfirmationRequired,
    FinalScheduleRoleSource,
    ScheduleUpdateResult,
    ShiftRegisterManager,
    ShiftTimelineScheduleChange,
    TeamSourceStatus,
    fresh_shift_channel_transaction,
)
from utils.shift_register_structs import (
    RecruitmentTimeRanges,
    Shift,
    ShiftParser,
    ShiftRegisterGoogleSheetsMetadata,
)
from utils.shift_register_timeline import (
    build_shift_timeline_template_values,
    render_shift_timeline_announcement_messages,
)
from utils.shift_schedule_image import (
    ScheduleImageRenderError,
    ScheduleImageTooLargeError,
    render_schedule_pdf_to_png,
)
from utils.shift_schedule_role import (
    ScheduleRolePlan,
    ScheduleRoleResolution,
    ScheduleRoleUpdateMode,
    plan_schedule_role_update,
    resolve_schedule_role_labels,
)
from utils.shift_scheduler import (
    ENCORE_SUPPORTER_SLOT,
    HONSO_SUPPORTER_SLOTS,
    STANDBY_SUPPORTER_SLOT,
    hour_label,
)
from utils.shift_timeline_scheduler import ShiftTimelineScheduler
from utils.storage_errors import StorageError, StorageErrorKind
from utils.structs_base import UserInfo

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping, Sequence

    from discord import Interaction, Member, Message
    from discord.ui import View

    from bot import Rhoboto
    from cogs.base.discord_context import GuildChannelSource
    from components.ui_settings_flow import SettingsPanel
    from utils.shift_scheduler import DraftSchedule


def _format_display_name(name: str) -> str:
    return escape_markdown(name) if "`" in name else f"`{name}`"


_SHIFT_REPORT_SECTION_PREFIXES = (
    "⚠️ 編成未登録：",
    "- 募集時間【",
    "- 未排入（",
    "附件包含生成時資料的 Notes 快照與 LLM 排班 prompt",
)
_MAX_BMP_CODE_POINT = 0xFFFF
_FINAL_CONTRACT_VALUE_LIMIT = 160
_SCHEDULE_IMAGE_FILENAMES = {
    "tentative": "shift-schedule-tentative.png",
    "confirmed": "shift-schedule-confirmed.png",
}


def _can_post_schedule_image(
    channel: TextChannel | Thread,
    member: Member,
) -> bool:
    permissions = channel.permissions_for(member)
    can_send = (
        permissions.send_messages_in_threads
        if isinstance(channel, Thread)
        else permissions.send_messages
    )
    return can_send and permissions.attach_files


def _schedule_image_permission_label(channel: TextChannel | Thread) -> str:
    if isinstance(channel, Thread):
        return "Send Messages in Threads 與 Attach Files"
    return "Send Messages 與 Attach Files"


def _discord_content_length(content: str) -> int:
    return len(content.encode("utf-16-le")) // 2


@dataclass(frozen=True)
class ShiftReportAssignment:
    hour: int
    encore: str | None
    honso: tuple[str, ...]
    standby: str | None


@dataclass(frozen=True)
class ScheduleRoleExecutionResult:
    added_member_ids: tuple[int, ...]
    removed_member_ids: tuple[int, ...]
    add_failed_member_ids: tuple[int, ...]
    remove_failed_member_ids: tuple[int, ...]


@dataclass(frozen=True)
class ScheduleRolePreviewSnapshot:
    sheet_url: str
    final_schedule_worksheet_id: int
    role_id: int
    mode: ScheduleRoleUpdateMode
    source: FinalScheduleRoleSource
    resolution: ScheduleRoleResolution


def _split_long_shift_report_section(section: str, limit: int) -> list[str]:
    chunks: list[str] = []
    start = 0
    width = 0
    last_break: int | None = None
    width_after_break = 0

    for index, char in enumerate(section):
        char_width = 2 if ord(char) > _MAX_BMP_CODE_POINT else 1
        width += char_width
        if last_break is not None:
            width_after_break += char_width
        if width > limit:
            if last_break is not None:
                chunks.append(section[start:last_break].removesuffix("\n"))
                start = last_break
                width = width_after_break
            else:
                chunks.append(section[start:index])
                start = index
                width = char_width
            last_break = None
            width_after_break = 0
        if char in "\n、":
            last_break = index + 1
            width_after_break = 0

    if start < len(section):
        chunks.append(section[start:])
    return chunks


def _split_shift_report(report: str, *, limit: int = 2000) -> list[str]:
    """Split a Shift report at semantic boundaries within Discord's limit."""
    if _discord_content_length(report) <= limit:
        return [report]

    messages: list[str] = []
    pending_lines: list[str] = []
    pending_length = 0
    for line in report.splitlines():
        line_chunks = _split_long_shift_report_section(line, limit)
        for chunk_index, chunk in enumerate(line_chunks):
            chunk_length = _discord_content_length(chunk)
            starts_section = chunk_index == 0 and line.startswith(
                _SHIFT_REPORT_SECTION_PREFIXES
            )
            if pending_lines and (
                starts_section or pending_length + 1 + chunk_length > limit
            ):
                messages.append("\n".join(pending_lines))
                pending_lines = []
                pending_length = 0
            if pending_lines:
                pending_length += 1
            pending_lines.append(chunk)
            pending_length += chunk_length
            if chunk_index < len(line_chunks) - 1:
                messages.append("\n".join(pending_lines))
                pending_lines = []
                pending_length = 0
    if pending_lines:
        messages.append("\n".join(pending_lines))
    return messages


def _format_shift_assignment_section(
    assignments: Sequence[ShiftReportAssignment],
    *,
    empty: bool,
) -> list[str]:
    lines = ["- 已排入（安可｜本走；待機）："]
    if empty:
        lines[0] += "なし"
        return lines
    for assignment in assignments:
        honso = list(assignment.honso)
        missing_honso = 3 - len(honso)
        if missing_honso:
            honso.append(f"缺 `{missing_honso}`")
        lines.append(
            f"  - -# `{hour_label(assignment.hour)}`："
            f"{assignment.encore or '缺'}｜{'、'.join(honso)}；"
            f"{assignment.standby or '缺'}"
        )
    return lines


def _format_generate_draft_confirmation(
    recruitment_ranges: RecruitmentTimeRanges,
    draft_sheet_url: str,
    team_source_status: TeamSourceStatus,
    team_summary_url: str | None,
) -> str:
    ranges = recruitment_ranges.ranges.ranges
    final_row = ranges[-1].end - ranges[0].start + 1
    return "\n".join(
        [
            "### ‼️ 確認產生班表草稿",
            (
                "請先備份需要保留的內容。確認後將覆蓋 "
                f"[Shift Draft]({draft_sheet_url}) 的以下位置："
            ),
            "- 班表：`A1:G31`",
            f"- Notes：`A{final_row + 2}`",
            (f"- 候補：`I1`、閾值・圖例 `I{final_row + 1}:M{final_row + 1}`"),
            f"- 反查：`J{final_row + 3}:L{final_row + 5}`",
            (f"- 編成一覧：Team Source 可用時從 `J{final_row + 6}` 寫入"),
            (
                "Team Source 同步：\n"
                "- 確認後會以目前 Discord 成員與 Team 資料更新 "
                f"[Team Summary]({team_summary_url})"
                if team_summary_url is not None
                else (
                    "Team Source 同步：\n⚠️ 未設定，本次不會同步"
                    if team_source_status is TeamSourceStatus.UNSET
                    else "Team Source 同步：\n⚠️ 設定無效，本次不會同步"
                )
            ),
            "",
            ("Notes・候補的展開位置若已有資料，將保留該資料並可能顯示 `#REF!`。"),
        ]
    )


def _event_day_status_message(request: ScheduleUpdateRequest) -> str | None:
    messages = {
        EventDayWriteStatus.OMITTED: "活動日期錨點未填，本次未寫入",
        EventDayWriteStatus.FORMAT_IGNORED: "活動日期格式已忽略（未提供錨點）",
        EventDayWriteStatus.INVALID_ANCHOR: "活動日期錨點格式無效，本次未寫入",
        EventDayWriteStatus.OVERLAPS_MAIN: "活動日期錨點與主範圍重疊，本次未寫入",
        EventDayWriteStatus.MISSING_EVENT_DATE: "DB 沒有活動日期，本次未寫入",
        EventDayWriteStatus.INVALID_FORMAT: "活動日期格式無效，本次未寫入",
    }
    return messages.get(request.event_day.status)


def _format_update_schedule_from_draft_confirmation(
    recruitment_ranges: RecruitmentTimeRanges,
    draft_sheet_url: str,
    final_sheet_url: str,
    request: ScheduleUpdateRequest,
) -> str:
    event_day = request.event_day
    event_day_line = (
        f"- 活動日期：`{event_day.anchor.a1}` = `{event_day.value}`"
        if event_day.status is EventDayWriteStatus.READY
        and event_day.anchor is not None
        and event_day.value is not None
        else f"- 活動日期：⚠️ {_event_day_status_message(request)}"
    )
    anchor_line = (
        f"- 這次會將新的 Final Schedule Anchor Cell `{request.main_anchor.a1}` "
        "寫回設定（僅在寫入成功後儲存）。"
        if request.anchor_to_persist is not None
        else f"- Final Schedule Anchor Cell：`{request.main_anchor.a1}`"
    )
    return "\n".join(
        [
            "### ‼️ 確認產生確定班表",
            "請先備份需要保留的內容。確認後才會讀取 Draft 並覆蓋 Final：",
            f"- 來源 [Shift Draft]({draft_sheet_url})：`{request.source_range}`",
            f"- 主範圍 [Final Schedule]({final_sheet_url})：`{request.main_range.a1}`",
            event_day_line,
            anchor_line,
            f"- 募集時間【{recruitment_ranges.announcement_display()}】",
            "‼️ 只會覆蓋上述目前主範圍；Final 範圍外的既有資料不會清除。",
            "⚠️ 若本次班表較短，主範圍以外的舊資料會保留，請先備份並自行確認。",
        ]
    )


def _format_final_report(
    result: ScheduleUpdateResult,
    final_sheet_url: str,
    recruitment_ranges: RecruitmentTimeRanges,
) -> str:
    runners = tuple(_format_display_name(runner) for runner in result.schedule.runners)
    lines = [
        "### ✅ 確定班表已產生",
        f"- Runner（ランナー）：{'、'.join(runners) if runners else 'なし'}",
        f"- 募集時間【{recruitment_ranges.announcement_display()}】",
        f"‼️ 已寫入 [Final Schedule]({final_sheet_url})",
        f"  - 主範圍：`{result.request.main_range.a1}`",
    ]
    event_day = result.request.event_day
    if (
        event_day.status is EventDayWriteStatus.READY
        and event_day.anchor is not None
        and event_day.value is not None
    ):
        lines.append(f"  - 活動日期：`{event_day.anchor.a1}` = `{event_day.value}`")
    else:
        message = _event_day_status_message(result.request)
        if message is not None:
            lines.append(f"⚠️ 警告：{message}")
    assignments = [
        ShiftReportAssignment(
            hour=row.hour,
            encore=_format_display_name(row.encore) if row.encore else None,
            honso=tuple(_format_display_name(name) for name in row.honso if name),
            standby=(_format_display_name(row.standby) if row.standby else None),
        )
        for row in result.schedule.rows
        if row.is_recruitment
    ]
    lines.extend(
        _format_shift_assignment_section(
            assignments,
            empty=not any(
                row.encore or any(row.honso) or row.standby
                for row in result.schedule.rows
                if row.is_recruitment
            ),
        )
    )
    return "\n".join(lines)


def _format_final_contract_error(error: FinalScheduleValidationError) -> str:
    location = (
        f"（第 {error.row} 列、第 {error.column} 欄）"
        if error.row is not None and error.column is not None
        else ""
    )
    problem = {
        FinalScheduleValidationKind.EMPTY: "Draft 沒有可讀取的內容",
        FinalScheduleValidationKind.HEADER: "Draft 標題列不符合契約",
        FinalScheduleValidationKind.AXIS: "Draft 時段軸不符合契約",
        FinalScheduleValidationKind.EXTRA_AXIS: "Draft 出現契約外的額外時段",
        FinalScheduleValidationKind.ROLE_VALUE: "Draft 崗位值不是文字或空白",
    }[error.kind]
    return (
        "### ⚠️📏 確定班表未產生\n"
        f"{problem}{location}。\n"
        f"- 預期：{_format_final_contract_value(error.expected)}\n"
        f"- 實際：{_format_final_contract_value(error.detected)}\n"
        "未寫入 Final；請修正 Draft 後重新執行 command。"
    )


def _format_final_contract_value(value: object) -> str:
    if value is None:
        return "缺少"
    if value == "":
        return "空白"
    if value is str:
        return "文字或空白"
    if isinstance(value, (tuple, list)):
        text = "｜".join("空白" if item == "" else str(item) for item in value)
    else:
        text = str(value)
    safe = escape_markdown(escape_mentions(text))
    return (
        f"{safe[:_FINAL_CONTRACT_VALUE_LIMIT]}"
        f"{'…' if len(safe) > _FINAL_CONTRACT_VALUE_LIMIT else ''}"
    )


def _format_final_conflict_report(error: FinalScheduleConflictError) -> str:
    lines = [
        "### ⚠️📏 確定班表未產生",
        "同一時段同一人被排入多個崗位，未寫入 Final：",
    ]
    lines.extend(
        f"  - -# `{hour_label(conflict.hour)}`："
        f"{_format_display_name(conflict.name)}（{'、'.join(conflict.roles)}）"
        for conflict in error.conflicts
    )
    lines.append("請修正 Draft 後重新執行 command。")
    return "\n".join(lines)


def _format_final_partial_success(
    request: ScheduleUpdateRequest,
    final_sheet_url: str,
) -> str:
    event_day = request.event_day
    date_outcome = (
        f"活動日期 `{event_day.anchor.a1}` 已寫入為 `{event_day.value}`。"
        if event_day.status is EventDayWriteStatus.READY
        and event_day.anchor is not None
        and event_day.value is not None
        else "活動日期未寫入。"
    )
    return "\n".join(
        [
            "### ⚠️🛠️ 確定班表部分完成",
            f"Final Schedule 的主範圍 `{request.main_range.a1}` 已寫入："
            f"[Final Schedule]({final_sheet_url})。",
            date_outcome,
            "DB 的 Final Schedule Anchor Cell 尚未更新；請確認工作表後，"
            f"使用相同的明確 anchor `{request.main_anchor.a1}` 重試。",
        ]
    )


async def _replace_with_shift_report(
    interaction: Interaction,
    report: str,
    *,
    view: View | None = None,
) -> Message:
    messages = _split_shift_report(report)
    control_message = await interaction.edit_original_response(
        content=messages[0],
        view=view if len(messages) == 1 else None,
    )
    for index, message in enumerate(messages[1:], start=1):
        is_last = index == len(messages) - 1
        if view is not None and is_last:
            control_message = await interaction.followup.send(
                message,
                ephemeral=True,
                view=view,
                wait=True,
            )
        else:
            await interaction.followup.send(message, ephemeral=True)
    return control_message


def _format_schedule_role_members(
    member_ids: Sequence[int],
    members_by_id: Mapping[int, Member],
) -> str:
    return (
        "、".join(members_by_id[member_id].mention for member_id in member_ids)
        or "なし"
    )


def _format_schedule_role_preview(
    role: Role,
    resolution: ScheduleRoleResolution,
    plan: ScheduleRolePlan,
    members_by_id: Mapping[int, Member],
    *,
    mode: ScheduleRoleUpdateMode,
) -> str:
    lines = [
        (
            "### ‼️ role 更新確認"
            if mode is ScheduleRoleUpdateMode.REPLACE
            else "### ⚠️ role 更新確認"
        ),
        (
            f"將賦予 {role.mention}："
            f"{_format_schedule_role_members(plan.add_member_ids, members_by_id)}"
        ),
    ]
    if plan.already_member_ids:
        lines.append(
            f"原本已有 {role.mention}："
            f"{_format_schedule_role_members(plan.already_member_ids, members_by_id)}"
        )
    if plan.remove_member_ids:
        lines.append(
            f"將清除以下成員的 {role.mention}："
            f"{_format_schedule_role_members(plan.remove_member_ids, members_by_id)}"
        )
    if resolution.unresolved_labels:
        lines.append(
            "⚠️ 找不到對應的 Discord 成員："
            + "、".join(
                _format_display_name(label) for label in resolution.unresolved_labels
            )
        )
    if resolution.duplicate_groups:
        lines.append("重複的成員：")
        lines.extend(
            f"- {_format_display_name(group.label)}："
            f"{_format_schedule_role_members(group.member_ids, members_by_id)}"
            for group in resolution.duplicate_groups
        )
    if mode is ScheduleRoleUpdateMode.REPLACE:
        lines.append("")
        lines.append(f"若繼續，將清除班表外成員的 {role.mention}。")
        if resolution.duplicate_groups:
            lines.append(
                f"若略過，將清除未被其他班表名稱辨識的重複成員之 {role.mention}。"
            )
        if resolution.unresolved_labels:
            lines.append(f"若其仍在 guild，所持有的 {role.mention} 也會被清除。")
    else:
        lines.append("")
    lines.append("尚未變更任何 role，請確認後再繼續。")
    return "\n".join(lines)


def _format_schedule_role_result(
    role: Role,
    resolution: ScheduleRoleResolution,
    plan: ScheduleRolePlan,
    execution: ScheduleRoleExecutionResult,
    members_by_id: Mapping[int, Member],
    *,
    duplicate_decision: ScheduleRoleDecision | None,
) -> str:
    result_has_error = bool(
        resolution.unresolved_labels
        or execution.add_failed_member_ids
        or execution.remove_failed_member_ids
    )
    added_members = _format_schedule_role_members(
        execution.added_member_ids, members_by_id
    )
    removed_members = _format_schedule_role_members(
        execution.removed_member_ids, members_by_id
    )
    add_failed_members = _format_schedule_role_members(
        execution.add_failed_member_ids, members_by_id
    )
    remove_failed_members = _format_schedule_role_members(
        execution.remove_failed_member_ids, members_by_id
    )
    lines = [
        "### ⚠️ role 更新結果" if result_has_error else "### ✅ role 更新結果",
        f"已經賦予 {role.mention}：{added_members}",
    ]
    if plan.already_member_ids:
        lines.append(
            f"原本已有 {role.mention}："
            f"{_format_schedule_role_members(plan.already_member_ids, members_by_id)}"
        )
    if execution.removed_member_ids:
        lines.append(f"已清除以下成員的 {role.mention}：{removed_members}")
    if resolution.unresolved_labels:
        lines.append(
            "⚠️ 找不到對應的 Discord 成員："
            + "、".join(
                _format_display_name(label) for label in resolution.unresolved_labels
            )
        )
    if execution.add_failed_member_ids:
        lines.append(f"⚠️ 無法賦予 {role.mention}：{add_failed_members}")
    if execution.remove_failed_member_ids:
        lines.append(f"⚠️ 無法清除 {role.mention}：{remove_failed_members}")
    if resolution.duplicate_groups and duplicate_decision in {
        ScheduleRoleDecision.INCLUDE,
        ScheduleRoleDecision.SKIP,
    }:
        label = (
            "已包含重複的成員："
            if duplicate_decision is ScheduleRoleDecision.INCLUDE
            else "已略過重複的成員："
        )
        lines.append(label)
        lines.extend(
            f"- {_format_display_name(group.label)}："
            f"{_format_schedule_role_members(group.member_ids, members_by_id)}"
            for group in resolution.duplicate_groups
        )
    return "\n".join(lines)


def _replace_schedule_role_state_line(
    content: str,
    replacement: str,
) -> str:
    lines = content.rsplit("\n", maxsplit=1)
    lines[-1] = replacement
    return "\n".join(lines)


async def _edit_schedule_role_control_message(
    interaction: Interaction,
    control_message: Message | None,
    content: str,
) -> None:
    edit = getattr(control_message, "edit", None)
    if edit is None:
        await interaction.edit_original_response(content=content, view=None)
    else:
        await edit(content=content, view=None)


async def _apply_schedule_role_plan(
    role: Role,
    plan: ScheduleRolePlan,
    members_by_id: Mapping[int, Member],
) -> ScheduleRoleExecutionResult:
    added: list[int] = []
    removed: list[int] = []
    add_failed: list[int] = []
    remove_failed: list[int] = []
    for member_id in plan.add_member_ids:
        try:
            await members_by_id[member_id].add_roles(role, atomic=True)
        except HTTPException:
            add_failed.append(member_id)
        else:
            added.append(member_id)
    for member_id in plan.remove_member_ids:
        try:
            await members_by_id[member_id].remove_roles(role, atomic=True)
        except HTTPException:
            remove_failed.append(member_id)
        else:
            removed.append(member_id)
    return ScheduleRoleExecutionResult(
        added_member_ids=tuple(added),
        removed_member_ids=tuple(removed),
        add_failed_member_ids=tuple(add_failed),
        remove_failed_member_ids=tuple(remove_failed),
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
    RegisterFeatureChannelBase[
        ShiftRegisterConfig,
        ShiftRegisterGoogleSheetsMetadata,
        ShiftRegisterManager,
        Shift,
        Shift,
    ],
    group_name="shift_register",
):
    feature_name = "shift_register"
    feature_display_name = SHIFT_REGISTER_DISPLAY_NAME
    guide_template_key = "shift.guide"
    auto_guide_template_key = "shift.auto_guide"
    timeline_template_key = "shift.timeline"
    sheet_write_lock = SHIFT_REGISTER_SHEET_WRITE_LOCK
    auto_guide_lock = KeyAsyncLock()
    schedule_role_lock = KeyAsyncLock()

    ManagerType = ShiftRegisterManager
    ParserType = ShiftParser

    def __init__(self, bot: Rhoboto) -> None:
        super().__init__(bot)
        self._timeline_scheduler = ShiftTimelineScheduler(
            self._handle_timeline_event,
            logger=self.logger,
        )
        self._timeline_bootstrap_task: asyncio.Task[None] | None = None
        self._pending_message_ids: dict[
            tuple[int, ShiftTimelineEventKind], tuple[int, int]
        ] = {}

    async def _get_shift_finalization_context_or_none(
        self,
        source: GuildChannelSource,
    ) -> (
        ConfiguredRegisterFeatureChannelContext[
            ShiftRegisterConfig,
            ShiftRegisterManager,
        ]
        | None
    ):
        feature_context = await self._get_register_feature_channel_context_or_none(
            guild_id=source.guild.id,
            channel_id=source.channel.id,
            require_enabled=False,
        )
        if feature_context is None:
            return None
        return await self._get_configured_register_feature_channel_context(
            feature_context
        )

    async def _read_schedule_role_snapshot(
        self,
        context: ConfiguredRegisterFeatureChannelContext[
            ShiftRegisterConfig,
            ShiftRegisterManager,
        ],
        *,
        members: Sequence[Member],
        role_id: int,
        mode: ScheduleRoleUpdateMode,
        final_schedule_range: A1Rectangle | None,
    ) -> ScheduleRolePreviewSnapshot:
        feature_config = context.feature_config
        recruitment_ranges = (
            RecruitmentTimeRanges.from_json(feature_config.recruitment_time_ranges)
            if final_schedule_range is None
            else None
        )
        metadata = await context.manager.fetch_google_sheets_metadata()
        context.manager.log_missing_worksheet_warnings(metadata)
        source = await context.manager.read_final_schedule_role_source(
            metadata,
            final_schedule_range=final_schedule_range,
            recruitment_ranges=recruitment_ranges,
            saved_anchor=feature_config.final_schedule_anchor_cell,
        )
        return ScheduleRolePreviewSnapshot(
            sheet_url=feature_config.sheet_url,
            final_schedule_worksheet_id=feature_config.final_schedule_worksheet_id,
            role_id=role_id,
            mode=mode,
            source=source,
            resolution=resolve_schedule_role_labels(source.labels, members),
        )

    async def cog_load(self) -> None:
        """Start deadline reconciliation after Discord reports readiness."""
        if (
            self._timeline_bootstrap_task is not None
            and not self._timeline_bootstrap_task.done()
        ):
            return
        self._timeline_bootstrap_task = asyncio.create_task(
            self._bootstrap_timeline_scheduler(),
            name="shift-timeline-bootstrap",
        )

    async def cog_unload(self) -> None:
        """Stop deadline work before the cog is removed from the bot."""
        bootstrap = self._timeline_bootstrap_task
        self._timeline_bootstrap_task = None
        if bootstrap is not None:
            bootstrap.cancel()
            await asyncio.gather(bootstrap, return_exceptions=True)
        await self._timeline_scheduler.close()
        self._pending_message_ids.clear()

    async def _bootstrap_timeline_scheduler(self) -> None:
        await self.bot.wait_until_ready()
        configs = await ShiftRegisterConfig.all().select_related("feature_channel")
        for config_item in configs:
            feature_channel = config_item.feature_channel
            manager = self.ManagerType(
                feature_channel,
                config.GOOGLE_SERVICE_ACCOUNT_PATH,
            )
            try:
                async with self.sheet_write_lock(feature_channel.channel_id):
                    result = await manager.reconcile_deadline_automation(
                        now=datetime.now(UTC)
                    )
                    change = result.schedule_change
                    if change is None and config_item.deadline_automation_enabled:
                        state = await ShiftTimelineEventState.get_or_none(
                            shift_register_id=config_item.id,
                            event_kind=ShiftTimelineEventKind.SUBMISSION_DEADLINE,
                        )
                        if (
                            state is not None
                            and state.status is not ShiftTimelineEventStatus.COMPLETED
                        ):
                            change = ShiftTimelineScheduleChange(
                                shift_register_id=config_item.id,
                                event_kind=state.event_kind,
                                scheduled_at=state.scheduled_at,
                                delivery_nonce=state.delivery_nonce,
                            )
                    if change is not None:
                        self._apply_timeline_schedule_change(change)
                if result.auto_close_disabled:
                    self.logger.warning(
                        "%s Guild=%s Channel=%s",
                        AUTO_CLOSE_INVALIDATED_MESSAGE,
                        feature_channel.guild_id,
                        feature_channel.channel_id,
                    )
            except asyncio.CancelledError:
                raise
            except Exception:
                self.logger.exception(
                    "Failed to reconcile Shift timeline deadline. Guild=%s Channel=%s",
                    feature_channel.guild_id,
                    feature_channel.channel_id,
                )

    def _apply_timeline_schedule_change(
        self,
        change: ShiftTimelineScheduleChange,
    ) -> None:
        key = (change.shift_register_id, change.event_kind)
        self._pending_message_ids.pop(key, None)
        if change.scheduled_at is None or change.delivery_nonce is None:
            self._timeline_scheduler.cancel(*key)
            return
        self._timeline_scheduler.schedule(
            shift_register_id=change.shift_register_id,
            event_kind=change.event_kind,
            scheduled_at=change.scheduled_at,
            delivery_nonce=change.delivery_nonce,
        )

    def _shift_auto_close_callbacks(self) -> ShiftAutoCloseCallbacks:
        return ShiftAutoCloseCallbacks(
            toggle=self._toggle_shift_auto_close,
            schedule_changed=self._apply_timeline_schedule_change,
            request_admin_notifications_reconcile=(
                self._request_admin_notifications_reconcile
            ),
        )

    def _request_admin_notifications_reconcile(self, guild_id: int) -> None:
        get_cog = getattr(self.bot, "get_cog", None)
        if not callable(get_cog):
            return
        notification_cog = get_cog("AdminNotifications")
        request_reconcile = getattr(
            notification_cog,
            "request_reconcile_guild",
            None,
        )
        if callable(request_reconcile):
            try:
                request_reconcile(guild_id)
            except Exception:
                self.logger.exception(
                    "Admin Notifications reconciliation request failed. Guild=%s",
                    guild_id,
                )

    def _cancel_submission_deadline(self, shift_register_id: int | None) -> None:
        if shift_register_id is None:
            return
        key = (
            shift_register_id,
            ShiftTimelineEventKind.SUBMISSION_DEADLINE,
        )
        self._pending_message_ids.pop(key, None)
        self._timeline_scheduler.cancel(*key)

    @override
    async def _enable_channel(self, guild_id: int, channel_id: int) -> None:
        feature_channel, _ = await FeatureChannel.get_or_create(
            guild_id=guild_id,
            channel_id=channel_id,
            feature_name=self.feature_name,
        )
        manager = self.ManagerType(
            feature_channel,
            config.GOOGLE_SERVICE_ACCOUNT_PATH,
        )
        async with self.sheet_write_lock(channel_id):
            shift_register_id = await manager.set_manual_feature_enabled(enabled=True)
        self._cancel_submission_deadline(shift_register_id)
        self.logger.info(
            "Enabled Feature: `%s` in Guild: `%s` Channel: `%s`",
            self.feature_name,
            guild_id,
            channel_id,
        )

    @override
    async def _disable_channel(self, guild_id: int, channel_id: int) -> bool:
        feature_channel = await FeatureChannel.get_or_none(
            guild_id=guild_id,
            channel_id=channel_id,
            feature_name=self.feature_name,
        )
        if feature_channel is None:
            self.logger.info(
                "No record to disable for Feature: `%s` in Guild: `%s` Channel: `%s`",
                self.feature_name,
                guild_id,
                channel_id,
            )
            return False
        manager = self.ManagerType(
            feature_channel,
            config.GOOGLE_SERVICE_ACCOUNT_PATH,
        )
        async with self.sheet_write_lock(channel_id):
            shift_register_id = await manager.set_manual_feature_enabled(enabled=False)
        self._cancel_submission_deadline(shift_register_id)
        self.logger.info(
            "Disabled Feature: `%s` in Guild: `%s` Channel: `%s`",
            self.feature_name,
            guild_id,
            channel_id,
        )
        return True

    @override
    async def _clear_feature_settings(self, guild_id: int, channel_id: int) -> None:
        feature_channel = await FeatureChannel.get_or_none(
            guild_id=guild_id,
            channel_id=channel_id,
            feature_name=self.feature_name,
        )
        if feature_channel is None:
            self.logger.info(
                "No record to clear for Feature: `%s` in Guild: `%s` Channel: `%s`",
                self.feature_name,
                guild_id,
                channel_id,
            )
            return
        manager = self.ManagerType(
            feature_channel,
            config.GOOGLE_SERVICE_ACCOUNT_PATH,
        )
        async with self.sheet_write_lock(channel_id):
            shift_register_id = await manager.clear_feature_settings()
        self._cancel_submission_deadline(shift_register_id)
        self._request_admin_notifications_reconcile(guild_id)
        self.logger.info(
            "Cleared feature settings for Feature: `%s` in Guild: `%s` Channel: `%s`",
            self.feature_name,
            guild_id,
            channel_id,
        )

    @override
    async def _refresh_auto_guide_if_enabled(
        self,
        feature_channel_context: RegisterFeatureChannelContext[ShiftRegisterManager],
        channel: object,
        *,
        feature_config: ShiftRegisterConfig | None = None,
    ) -> bool:
        try:
            fresh_feature_channel = await FeatureChannel.get_or_none(
                id=feature_channel_context.feature_channel.id
            )
        except Exception:
            self.logger.exception(
                "Failed to refresh auto guide for Feature: `%s` in Guild: `%s` "
                "Channel: `%s`",
                self.feature_name,
                feature_channel_context.guild_id,
                feature_channel_context.channel_id,
            )
            return False
        if fresh_feature_channel is None or not fresh_feature_channel.is_enabled:
            return True

        fresh_context = RegisterFeatureChannelContext(
            guild_id=feature_channel_context.guild_id,
            channel_id=feature_channel_context.channel_id,
            feature_channel=fresh_feature_channel,
            manager=feature_channel_context.manager,
        )
        return await super()._refresh_auto_guide_if_enabled(
            fresh_context,
            channel,
            feature_config=feature_config,
        )

    @override
    async def _guide_template_values(
        self,
        context: ConfiguredRegisterFeatureChannelContext[
            ShiftRegisterConfig, ShiftRegisterManager
        ],
    ) -> dict[str, object]:
        values = await super()._guide_template_values(context)
        values[
            "team_source_channel_id"
        ] = await context.manager.get_saved_team_source_channel_id()
        return values

    @override
    def _auto_guide_template_values(
        self,
        context: ConfiguredRegisterFeatureChannelContext[
            ShiftRegisterConfig, ShiftRegisterManager
        ],
        language: str,
    ) -> dict[str, object]:
        values = super()._auto_guide_template_values(context, language)
        feature_config = context.feature_config
        recruitment_ranges = RecruitmentTimeRanges.from_json(
            feature_config.recruitment_time_ranges
        )
        values.update(
            build_shift_timeline_template_values(
                language,
                day_number=feature_config.day_number,
                event_date=feature_config.event_date,
                recruitment_time_range=recruitment_ranges.announcement_display(),
                submission_deadline_at=feature_config.submission_deadline_at,
                draft_shift_proposal_at=feature_config.draft_shift_proposal_at,
                final_shift_notice_at=feature_config.final_shift_notice_at,
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
        sheet_config: ShiftRegisterConfig,
    ) -> SettingsPanel:
        return await build_shift_register_settings_panel(
            manager,
            sheet_config,
            latest_guide_enabled=False,
            latest_guide_toggle_callback=self._toggle_shift_latest_guide,
            latest_guide_state_resolver=self._latest_guide_state_resolver(manager),
            latest_guide_refresh_callback=self._latest_guide_refresh_callback(manager),
            auto_close_callbacks=self._shift_auto_close_callbacks(),
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

    async def _toggle_shift_auto_close(
        self,
        interaction: Interaction,
        *,
        enabled: bool,
        current_view: View,
    ) -> None:
        source = require_guild_channel_source(
            interaction,
            action="toggle Shift Register Auto Close",
        )
        manager = current_view.shift_register_manager
        try:
            async with fresh_shift_channel_transaction(
                manager,
                self.sheet_write_lock,
                channel_id=source.channel.id,
            ) as shift_register:
                schedule_change = await manager.set_deadline_automation_enabled(
                    enabled=enabled,
                    now=datetime.now(UTC),
                )
                shift_register.deadline_automation_enabled = enabled
        except AutoCloseDeadlineNotFutureError:
            raise
        except Exception as exc:  # noqa: BLE001
            await self._send_interaction_storage_error_or_raise(
                interaction,
                exc,
                source=source,
                operation="shift_register_auto_close_toggle",
            )
            return

        self._apply_timeline_schedule_change(schedule_change)
        try:
            panel = await self._build_settings_panel(
                interaction,
                manager,
                shift_register,
            )
        except Exception as exc:  # noqa: BLE001
            current_view.stop()
            await self._send_interaction_storage_error_or_raise(
                interaction,
                exc,
                source=source,
                operation="shift_register_auto_close_refresh_panel",
            )
            return

        replacement_view = prepare_replacement_settings_view(current_view, panel.view)
        await interaction.edit_original_response(
            content=None,
            embed=panel.embed,
            view=replacement_view,
        )

    async def _handle_timeline_event(  # noqa: C901, PLR0911, PLR0912, PLR0915
        self,
        shift_register_id: int,
        event_kind: ShiftTimelineEventKind,
        scheduled_at: datetime,
        delivery_nonce: int,
    ) -> None:
        if event_kind is not ShiftTimelineEventKind.SUBMISSION_DEADLINE:
            self.logger.warning(
                "Ignoring unknown Shift timeline event kind %r for Shift Register %s.",
                event_kind,
                shift_register_id,
            )
            return

        config_item = await (
            ShiftRegisterConfig.filter(id=shift_register_id)
            .select_related("feature_channel")
            .first()
        )
        if config_item is None:
            self.logger.info(
                "Ignoring removed Shift timeline event for Shift Register %s.",
                shift_register_id,
            )
            self._pending_message_ids.pop(
                (shift_register_id, event_kind),
                None,
            )
            return

        feature_channel = config_item.feature_channel
        manager = self.ManagerType(
            feature_channel,
            config.GOOGLE_SERVICE_ACCOUNT_PATH,
        )
        async with self.sheet_write_lock(feature_channel.channel_id):
            fresh_config = await manager.get_fresh_sheet_config()
            if fresh_config is None:
                self._pending_message_ids.pop(
                    (shift_register_id, event_kind),
                    None,
                )
                return
            execution = await manager.begin_submission_deadline_close(
                expected_scheduled_at=scheduled_at,
                expected_delivery_nonce=delivery_nonce,
                now=datetime.now(UTC),
            )

        key = (shift_register_id, event_kind)
        if execution is None:
            self._pending_message_ids.pop(key, None)
            return

        message_id = execution.message_id
        if execution.status is ShiftTimelineEventStatus.SCHEDULED:
            cached = self._pending_message_ids.get(key)
            if cached is not None and cached[0] == execution.delivery_nonce:
                message_id = cached[1]
            else:
                channel = self.bot.get_channel(execution.channel_id)
                if channel is None:
                    error_message = "Shift channel is not available."
                    raise RuntimeError(error_message)
                recruitment_ranges = RecruitmentTimeRanges.from_json(
                    fresh_config.recruitment_time_ranges
                )
                embeds = await self._render_localized_embeds(
                    execution.guild_id,
                    template_key="shift.deadline_close",
                    values_for_language=lambda language: (
                        build_shift_timeline_template_values(
                            language,
                            day_number=fresh_config.day_number,
                            event_date=fresh_config.event_date,
                            recruitment_time_range=(
                                recruitment_ranges.announcement_display()
                            ),
                            submission_deadline_at=(
                                fresh_config.submission_deadline_at
                            ),
                            draft_shift_proposal_at=(
                                fresh_config.draft_shift_proposal_at
                            ),
                            final_shift_notice_at=fresh_config.final_shift_notice_at,
                        )
                    ),
                    include_footer=True,
                )
                message = await channel.send(
                    embeds=embeds,
                    view=ShiftDeadlineCloseView(self._guide_sheet_url(fresh_config)),
                    nonce=execution.delivery_nonce,
                )
                message_id = message.id
                self._pending_message_ids[key] = (
                    execution.delivery_nonce,
                    message_id,
                )

            if message_id is None:
                self._pending_message_ids.pop(key, None)
                return
            async with self.sheet_write_lock(feature_channel.channel_id):
                marked = await manager.mark_submission_deadline_sent(
                    event_state_id=execution.event_state_id,
                    delivery_nonce=execution.delivery_nonce,
                    message_id=message_id,
                )
            if not marked:
                self._pending_message_ids.pop(key, None)
                return
            self._pending_message_ids.pop(key, None)
        elif execution.status is ShiftTimelineEventStatus.SENT:
            self._pending_message_ids.pop(key, None)
        else:
            self._pending_message_ids.pop(key, None)
            return

        guide_context = RegisterFeatureChannelContext(
            guild_id=execution.guild_id,
            channel_id=execution.channel_id,
            feature_channel=feature_channel,
            manager=manager,
        )
        try:
            deleted = await self._disable_auto_guide_and_delete_message(guide_context)
            if not deleted:
                self.logger.warning(
                    "Failed to clean up Latest Guide after Shift deadline close. "
                    "Guild=%s Channel=%s",
                    execution.guild_id,
                    execution.channel_id,
                )
        except Exception:
            self.logger.exception(
                "Failed to clean up Latest Guide after Shift deadline close. "
                "Guild=%s Channel=%s",
                execution.guild_id,
                execution.channel_id,
            )

        channel = self.bot.get_channel(execution.channel_id)
        if channel is None:
            self.logger.warning(
                "Failed to rename Shift deadline channel because it was not "
                "available. Guild=%s Channel=%s",
                execution.guild_id,
                execution.channel_id,
            )
        else:
            try:
                new_name = (
                    channel.name
                    if channel.name.startswith("〆")
                    else f"〆{channel.name[:99]}"
                )
                if new_name != channel.name:
                    await channel.edit(name=new_name)
            except Exception:
                self.logger.exception(
                    "Failed to rename Shift deadline channel. Guild=%s Channel=%s",
                    execution.guild_id,
                    execution.channel_id,
                )

        async with self.sheet_write_lock(feature_channel.channel_id):
            await manager.complete_submission_deadline(
                event_state_id=execution.event_state_id,
                delivery_nonce=execution.delivery_nonce,
            )

    @override
    async def _process_configured_message_submission(
        self,
        message: Message,
        context: ConfiguredRegisterFeatureChannelContext[
            ShiftRegisterConfig, ShiftRegisterManager
        ],
        submission: Shift,
        user_info: UserInfo,
    ) -> Shift | None:
        shift = submission
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
    ) -> Shift | None:
        invalid = False
        async with fresh_shift_channel_transaction(
            manager,
            self.sheet_write_lock,
            channel_id=message.channel.id,
        ) as fresh_config:
            fresh_feature_channel = await FeatureChannel.get_or_none(
                id=manager.feature_channel.id
            )
            if fresh_feature_channel is None or not fresh_feature_channel.is_enabled:
                self.logger.info(
                    "Skipped stale Shift registration after feature closure. "
                    "guild=%s channel=%s message=%s",
                    message.guild.id,
                    message.channel.id,
                    message.id,
                )
                return None
            manager.feature_channel = fresh_feature_channel
            recruitment_ranges = RecruitmentTimeRanges.from_json(
                fresh_config.recruitment_time_ranges
            )
            invalid = not recruitment_ranges.contains_slots(set(shift))
            if not invalid:
                self.logger.info(
                    (
                        "Parsed Shift Register submission. "
                        "operation=shift_register_parse feature=%s guild=%s "
                        "channel=%s message=%s slots=%s"
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

                metadata = await manager.fetch_google_sheets_metadata()
                manager.log_missing_worksheet_warnings(metadata)

                await manager.upsert_or_delete_user_shift(
                    user_info,
                    shift,
                    metadata=metadata,
                    recruitment_ranges=recruitment_ranges,
                )

        if invalid:
            await self._add_invalid_message_reactions(message)
            return None

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
            feature_channel_context = await self._get_register_feature_channel_context(
                source
            )
            context = await self._get_configured_register_feature_channel_context(
                feature_channel_context
            )
            if context is None:
                await self._send_missing_register_config_followup(interaction)
                return

            feature_config = context.feature_config
            recruitment_ranges = RecruitmentTimeRanges.from_json(
                feature_config.recruitment_time_ranges
            )
            announcements = await render_shift_timeline_announcement_messages(
                self.timeline_template_key,
                context.guild_id,
                self.logger,
                day_number=feature_config.day_number,
                event_date=feature_config.event_date,
                recruitment_time_range=recruitment_ranges.announcement_display(),
                submission_deadline_at=feature_config.submission_deadline_at,
                draft_shift_proposal_at=feature_config.draft_shift_proposal_at,
                final_shift_notice_at=feature_config.final_shift_notice_at,
            )
        except Exception as exc:  # noqa: BLE001
            await self._send_interaction_storage_error_or_raise(
                interaction,
                exc,
                source=source,
                operation="shift_register_announce_timeline",
            )
            return

        if not announcements:
            await interaction.followup.send(
                ANNOUNCEMENT_RENDER_FAILURE_MESSAGE,
                ephemeral=True,
            )
            return
        for announcement in announcements:
            await interaction.followup.send(
                announcement.content,
                ephemeral=False,
            )

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
        name="post_schedule_image",
        description="Post the current Final Schedule as an image.",
    )
    @app_commands.describe(
        schedule_status="Schedule status used in the attachment filename.",
        channel="Destination channel; defaults to the current channel.",
        final_schedule_range=("Optional Final Schedule rectangle, for example A1:J30."),
    )
    @app_commands.choices(
        schedule_status=[
            app_commands.Choice(
                name=app_commands.locale_str("Tentative"),
                value="tentative",
            ),
            app_commands.Choice(
                name=app_commands.locale_str("Confirmed"),
                value="confirmed",
            ),
        ]
    )
    async def post_schedule_image(  # noqa: C901, PLR0911, PLR0912
        self,
        interaction: Interaction,
        schedule_status: str,
        channel: TextChannel | Thread | None = None,
        final_schedule_range: str | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        source = require_guild_channel_source(
            interaction,
            action="post Final Schedule image",
        )
        try:
            selected_range = (
                parse_a1_range(final_schedule_range)
                if final_schedule_range is not None
                else None
            )
            destination = channel or source.channel
            if not isinstance(destination, (TextChannel, Thread)):
                await interaction.edit_original_response(
                    content=("⚠️ 請指定文字頻道或討論串作為發布目的地；未發布圖片。")
                )
                return

            bot_member = source.guild.me
            if bot_member is None:
                raise RuntimeError  # noqa: TRY301
            if not _can_post_schedule_image(destination, bot_member):
                await interaction.edit_original_response(
                    content=(
                        f"⚠️ Bot 無法在 {destination.mention} 發布班表圖片；"
                        f"需要 {_schedule_image_permission_label(destination)} 權限。"
                    )
                )
                return

            context = await self._get_shift_finalization_context_or_none(source)
            if context is None:
                await self._send_missing_register_config_followup(interaction)
                return

            async with fresh_shift_channel_transaction(
                context.manager,
                self.sheet_write_lock,
                channel_id=source.channel.id,
            ):
                metadata = await context.manager.fetch_google_sheets_metadata()
                pdf_bytes = await context.manager.export_final_schedule_pdf(
                    metadata,
                    final_schedule_range=selected_range,
                )

            png_bytes = await asyncio.to_thread(
                render_schedule_pdf_to_png,
                pdf_bytes,
            )
            if len(png_bytes) > source.guild.filesize_limit:
                raise ScheduleImageTooLargeError  # noqa: TRY301
        except FinalScheduleInputError:
            await interaction.edit_original_response(
                content=(
                    f"⚠️ {config.CONFUSED_EMOJI} Final Schedule Range "
                    "格式無效，未發布圖片。"
                )
            )
            return
        except SheetConfigNotFoundError:
            await self._send_missing_register_config_followup(interaction)
            return
        except FinalScheduleImageRangeError:
            await interaction.edit_original_response(
                content=(
                    "⚠️📏 Final Schedule 沒有可發布的資料範圍，"
                    "或指定範圍超出 worksheet；未發布圖片。"
                )
            )
            return
        except ScheduleImageTooLargeError:
            await interaction.edit_original_response(
                content=(
                    "⚠️ 班表圖片過大，請指定較小的 Final Schedule Range；未發布圖片。"
                )
            )
            return
        except ScheduleImageRenderError:
            await interaction.edit_original_response(
                content="⚠️🚧 班表圖片產生失敗，未發布圖片。"
            )
            return
        except Exception as exc:  # noqa: BLE001
            await self._send_interaction_storage_error_or_raise(
                interaction,
                exc,
                source=source,
                operation="shift_register_post_schedule_image",
            )
            return

        try:
            message = await destination.send(
                file=File(
                    BytesIO(png_bytes),
                    filename=_SCHEDULE_IMAGE_FILENAMES[schedule_status],
                )
            )
        except (Forbidden, HTTPException):
            await interaction.edit_original_response(
                content=("⚠️🛠️ Discord 無法發布班表圖片，未建立圖片訊息。")
            )
            return

        try:
            await interaction.edit_original_response(content=message.jump_url)
        except HTTPException:
            self.logger.warning(
                "Posted schedule image but failed to edit success response. "
                "operation=%s guild=%s channel=%s message=%s",
                "shift_register_post_schedule_image",
                source.guild.id,
                destination.id,
                message.id,
            )

    @app_commands.command(
        name="update_schedule_from_draft",
        description=("Update the current shift schedule from the Shift Draft."),
    )
    @app_commands.describe(
        final_schedule_anchor_cell=(
            "Top-left cell of the Final Schedule overwrite range."
        ),
        event_day_anchor_cell=(
            "Optional cell where the formatted event date is written."
        ),
        event_day_format=(f"Event date format. Default: {DEFAULT_EVENT_DAY_FORMAT}"),
    )
    async def update_schedule_from_draft(  # noqa: C901, PLR0911, PLR0912, PLR0915
        self,
        interaction: Interaction,
        final_schedule_anchor_cell: str | None = None,
        event_day_anchor_cell: str | None = None,
        event_day_format: app_commands.Range[str, 1, 512] | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        source = require_guild_channel_source(
            interaction,
            action="update schedule from Shift Draft",
        )
        request: ScheduleUpdateRequest | None = None
        final_sheet_url = ""
        try:
            context = await self._get_shift_finalization_context_or_none(source)
            if context is None:
                await self._send_missing_register_config_followup(interaction)
                return

            feature_config = context.feature_config
            recruitment_ranges = RecruitmentTimeRanges.from_json(
                feature_config.recruitment_time_ranges
            )
            request = build_schedule_update_request(
                recruitment_ranges=recruitment_ranges,
                saved_anchor=feature_config.final_schedule_anchor_cell,
                supplied_anchor=final_schedule_anchor_cell,
                event_date=feature_config.event_date,
                event_day_anchor=event_day_anchor_cell,
                event_day_format=event_day_format,
            )
            draft_sheet_url = google_sheet_url_with_gid(
                feature_config.sheet_url,
                feature_config.draft_worksheet_id,
            )
            final_sheet_url = google_sheet_url_with_gid(
                feature_config.sheet_url,
                feature_config.final_schedule_worksheet_id,
            )
            confirmation_content = _format_update_schedule_from_draft_confirmation(
                recruitment_ranges,
                draft_sheet_url,
                final_sheet_url,
                request,
            )
            fingerprint = (
                feature_config.sheet_url,
                feature_config.draft_worksheet_id,
                feature_config.final_schedule_worksheet_id,
                feature_config.final_schedule_anchor_cell,
                request,
            )
            view = GenerateShiftScheduleConfirmView(
                requesting_user_id=interaction.user.id,
                destination_label="Final Schedule",
                destination_url=final_sheet_url,
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
                    content="✖️ 確認逾時，未變更 Final Schedule。",
                    view=None,
                )
                return

            context = await self._get_shift_finalization_context_or_none(source)
            if context is None:
                await self._send_missing_register_config_followup(interaction)
                return
            async with fresh_shift_channel_transaction(
                context.manager,
                self.sheet_write_lock,
                channel_id=source.channel.id,
            ) as fresh_config:
                fresh_ranges = RecruitmentTimeRanges.from_json(
                    fresh_config.recruitment_time_ranges
                )
                fresh_request = build_schedule_update_request(
                    recruitment_ranges=fresh_ranges,
                    saved_anchor=fresh_config.final_schedule_anchor_cell,
                    supplied_anchor=final_schedule_anchor_cell,
                    event_date=fresh_config.event_date,
                    event_day_anchor=event_day_anchor_cell,
                    event_day_format=event_day_format,
                )
                fresh_fingerprint = (
                    fresh_config.sheet_url,
                    fresh_config.draft_worksheet_id,
                    fresh_config.final_schedule_worksheet_id,
                    fresh_config.final_schedule_anchor_cell,
                    fresh_request,
                )
                if fresh_fingerprint != fingerprint:
                    await interaction.edit_original_response(
                        content=(
                            "⚠️ 設定或覆蓋目標已變更，未變更 Final Schedule；"
                            "請重新執行 command。"
                        ),
                        view=None,
                    )
                    return
                metadata = await context.manager.fetch_google_sheets_metadata()
                context.manager.log_missing_worksheet_warnings(metadata)
                result = await context.manager.update_schedule_from_draft(
                    metadata,
                    request=fresh_request,
                )
                final_sheet_url = google_sheet_url_with_gid(
                    fresh_config.sheet_url,
                    fresh_config.final_schedule_worksheet_id,
                )
        except FinalScheduleInputError:
            await interaction.edit_original_response(
                content=(
                    f"⚠️ {config.CONFUSED_EMOJI} Final Schedule Anchor Cell "
                    "格式無效，未變更任何內容。"
                ),
                view=None,
            )
            return
        except SheetConfigNotFoundError:
            await interaction.edit_original_response(
                content=(
                    "⚠️ Shift Register 設定已不存在，未變更 Final Schedule；"
                    "請重新設定後再試。"
                ),
                view=None,
            )
            return
        except FinalScheduleReconfirmationRequired:
            await interaction.edit_original_response(
                content=(
                    "⚠️📏 Draft 或 Final worksheet 設定已修復或變更，"
                    "未讀取或寫入任何內容；請重新執行 command。"
                ),
                view=None,
            )
            return
        except FinalScheduleValidationError as exc:
            await _replace_with_shift_report(
                interaction,
                _format_final_contract_error(exc),
            )
            return
        except FinalScheduleConflictError as exc:
            await _replace_with_shift_report(
                interaction,
                _format_final_conflict_report(exc),
            )
            return
        except StorageError as exc:
            if (
                exc.kind is StorageErrorKind.PARTIAL_SUCCESS
                and exc.log_hint == "final_schedule_written_anchor_not_persisted"
                and request is not None
            ):
                await _replace_with_shift_report(
                    interaction,
                    _format_final_partial_success(request, final_sheet_url),
                )
                return
            await self._send_interaction_storage_error_or_raise(
                interaction,
                exc,
                source=source,
                operation="shift_register_update_schedule_from_draft",
            )
            return
        except Exception as exc:  # noqa: BLE001
            await self._send_interaction_storage_error_or_raise(
                interaction,
                exc,
                source=source,
                operation="shift_register_update_schedule_from_draft",
            )
            return

        await _replace_with_shift_report(
            interaction,
            _format_final_report(result, final_sheet_url, fresh_ranges),
        )

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
        runner="Discord user pinned to the Runner (ランナー) lane for every hour.",
    )
    async def generate_draft(
        self,
        interaction: Interaction,
        encore_power_threshold: app_commands.Range[float, 0],
        runner: User | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        source = require_guild_channel_source(
            interaction,
            action="generate shift draft schedule",
        )
        try:
            context = await self._get_shift_finalization_context_or_none(source)
            if context is None:
                await self._send_missing_register_config_followup(interaction)
                return

            recruitment_ranges = RecruitmentTimeRanges.from_json(
                context.feature_config.recruitment_time_ranges
            )
            draft_sheet_url = google_sheet_url_with_gid(
                context.feature_config.sheet_url,
                context.feature_config.draft_worksheet_id,
            )
            (
                team_source_status,
                team_summary_url,
            ) = await context.manager.get_saved_team_summary_destination()
            confirmation_content = _format_generate_draft_confirmation(
                recruitment_ranges,
                draft_sheet_url,
                team_source_status,
                team_summary_url,
            )
            view = GenerateShiftDraftConfirmView(
                requesting_user_id=interaction.user.id,
                destination_label="Shift Draft",
                destination_url=draft_sheet_url,
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
                    content="✖️ 確認逾時，未變更 Shift Draft。",
                    view=None,
                )
                return

            context = await self._get_shift_finalization_context_or_none(source)
            if context is None:
                await self._send_missing_register_config_followup(interaction)
                return
            async with fresh_shift_channel_transaction(
                context.manager,
                self.sheet_write_lock,
                channel_id=source.channel.id,
            ) as fresh_config:
                fresh_ranges = RecruitmentTimeRanges.from_json(
                    fresh_config.recruitment_time_ranges
                )
                fresh_draft_sheet_url = google_sheet_url_with_gid(
                    fresh_config.sheet_url,
                    fresh_config.draft_worksheet_id,
                )
                (
                    fresh_team_source_status,
                    fresh_team_summary_url,
                ) = await context.manager.get_saved_team_summary_destination()
                if (
                    _format_generate_draft_confirmation(
                        fresh_ranges,
                        fresh_draft_sheet_url,
                        fresh_team_source_status,
                        fresh_team_summary_url,
                    )
                    != confirmation_content
                ):
                    await interaction.edit_original_response(
                        content=(
                            "⚠️ 募集時段設定已變更，未變更 Shift Draft；"
                            "請重新執行 command。"
                        ),
                        view=None,
                    )
                    return

                metadata = await context.manager.fetch_google_sheets_metadata()
                context.manager.log_missing_worksheet_warnings(metadata)
                member_by_names = {
                    member.name: member for member in source.guild.members
                }
                runner_info = (
                    UserInfo(
                        username=runner.name,
                        display_name=runner.display_name,
                    )
                    if runner is not None
                    else None
                )
                result = await context.manager.generate_draft(
                    metadata,
                    member_by_names=member_by_names,
                    encore_power_threshold=float(encore_power_threshold),
                    runner=runner_info,
                    administrator_requirements=view.administrator_requirements,
                )
                schedule = result.schedule
                current_config = await context.manager.get_sheet_config()
                draft_sheet_url = google_sheet_url_with_gid(
                    current_config.sheet_url,
                    current_config.draft_worksheet_id,
                )
                member_mentions = {
                    username: member.mention
                    for username, member in member_by_names.items()
                }
        except Exception as exc:  # noqa: BLE001
            await self._send_interaction_storage_error_or_raise(
                interaction,
                exc,
                source=source,
                operation="shift_register_generate_draft",
            )
            return

        report_messages = _split_shift_report(
            self._format_draft_report(
                schedule,
                draft_sheet_url,
                member_mentions,
                encore_power_threshold=float(encore_power_threshold),
                recruitment_ranges=result.recruitment_ranges,
                team_summary_url=result.team_summary_url,
                team_source_warning=result.team_source_warning,
                unregistered_usernames=result.unregistered_usernames,
            )
        )
        for index, report_message in enumerate(report_messages):
            send_kwargs: dict[str, object] = {"ephemeral": True}
            if index == len(report_messages) - 1:
                send_kwargs["files"] = [
                    File(
                        BytesIO(result.notes_snapshot.encode("utf-8")),
                        filename="shift-draft-notes.txt",
                    ),
                    File(
                        BytesIO(result.llm_prompt.encode("utf-8")),
                        filename="shift-draft-llm-prompt.txt",
                    ),
                ]
            await interaction.followup.send(report_message, **send_kwargs)

    @app_commands.command(
        name="assign_schedule_role",
        description="Assign a Discord role from the current Final Schedule.",
    )
    @app_commands.describe(
        role="Discord role to update from the Final Schedule.",
        role_update_mode="Add scheduled members or fully replace role membership.",
        final_schedule_range="Optional bounded Final range, for example B2:G12.",
    )
    @app_commands.choices(
        role_update_mode=[
            app_commands.Choice(name="只新增", value="add_only"),
            app_commands.Choice(
                name="完全取代（清除班表外成員）",
                value="replace",
            ),
        ]
    )
    async def assign_schedule_role(  # noqa: C901, PLR0911, PLR0912, PLR0915
        self,
        interaction: Interaction,
        role: Role,
        role_update_mode: str = ScheduleRoleUpdateMode.ADD_ONLY.value,
        final_schedule_range: str | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        source = require_guild_channel_source(
            interaction,
            action="assign Final Schedule Discord role",
        )
        if not role.is_assignable():
            await interaction.edit_original_response(
                content=(
                    f"⚠️ {config.CONFUSED_EMOJI} Bot 無法新增或清除 {role.mention}，"
                    "請確認 role 類型與角色順位。"
                ),
                view=None,
            )
            return
        bot_member = source.guild.me
        if bot_member is None or not bot_member.guild_permissions.manage_roles:
            await interaction.edit_original_response(
                content=(
                    "⚠️ Bot 缺少 Manage Roles 權限，未讀取 Final Schedule，"
                    "也未變更任何 role。"
                ),
                view=None,
            )
            return
        try:
            selected_range = (
                parse_a1_range(final_schedule_range)
                if final_schedule_range is not None
                else None
            )
        except FinalScheduleInputError:
            await interaction.edit_original_response(
                content=(
                    f"⚠️ {config.CONFUSED_EMOJI} "
                    "Final Schedule Range 格式無效，未變更任何 role。"
                ),
                view=None,
            )
            return
        mode = ScheduleRoleUpdateMode(role_update_mode)
        control_message: Message | None = None
        control_content: str | None = None
        try:
            context = await self._get_shift_finalization_context_or_none(source)
            if context is None:
                await self._send_missing_register_config_followup(interaction)
                return
            snapshot = await self._read_schedule_role_snapshot(
                context,
                members=source.guild.members,
                role_id=role.id,
                mode=mode,
                final_schedule_range=selected_range,
            )
            members_by_id = {member.id: member for member in source.guild.members}
            has_duplicates = bool(snapshot.resolution.duplicate_groups)
            requires_preview = mode is ScheduleRoleUpdateMode.REPLACE or has_duplicates

            if requires_preview:
                preview_plan = plan_schedule_role_update(
                    snapshot.resolution,
                    tuple(member.id for member in role.members),
                    mode=mode,
                    include_duplicates=None if has_duplicates else False,
                )
                preview = _format_schedule_role_preview(
                    role,
                    snapshot.resolution,
                    preview_plan,
                    members_by_id,
                    mode=mode,
                )
                view = AssignScheduleRoleConfirmView(
                    requesting_user_id=interaction.user.id,
                    has_duplicates=has_duplicates,
                    replace_mode=mode is ScheduleRoleUpdateMode.REPLACE,
                )
                control_message = await _replace_with_shift_report(
                    interaction,
                    preview,
                    view=view,
                )
                control_content = _split_shift_report(preview)[-1]
                await view.wait()
                duplicate_decision = view.decision
                terminal_state = {
                    None: "✖️ 確認逾時，未變更任何 role。",
                    ScheduleRoleDecision.CANCEL: "✖️ 已取消，未變更任何 role。",
                    ScheduleRoleDecision.PERMISSION_LOST: (
                        "⚠️ 權限已變更，未變更任何 role。"
                    ),
                }.get(duplicate_decision)
                if terminal_state is not None:
                    await _edit_schedule_role_control_message(
                        interaction,
                        control_message,
                        _replace_schedule_role_state_line(
                            control_content or preview,
                            terminal_state,
                        ),
                    )
                    return

                await _edit_schedule_role_control_message(
                    interaction,
                    control_message,
                    _replace_schedule_role_state_line(
                        control_content or preview,
                        f"{config.PROCESSING_EMOJI} role 更新中…",
                    ),
                )
                context = await self._get_shift_finalization_context_or_none(source)
                if context is None:
                    await _edit_schedule_role_control_message(
                        interaction,
                        control_message,
                        _replace_schedule_role_state_line(
                            control_content or preview,
                            "⚠️ role 更新未完成，未變更任何 role。",
                        ),
                    )
                    await self._send_missing_register_config_followup(interaction)
                    return
                current_snapshot = await self._read_schedule_role_snapshot(
                    context,
                    members=source.guild.members,
                    role_id=role.id,
                    mode=mode,
                    final_schedule_range=selected_range,
                )
                if current_snapshot != snapshot:
                    await _edit_schedule_role_control_message(
                        interaction,
                        control_message,
                        _replace_schedule_role_state_line(
                            control_content or preview,
                            "⚠️ Final Schedule 或 Discord 成員資料已變更，"
                            "未變更任何 role；請重新執行 command。",
                        ),
                    )
                    return
                include_duplicates = duplicate_decision is ScheduleRoleDecision.INCLUDE
                members_by_id = {member.id: member for member in source.guild.members}
                async with self.schedule_role_lock((source.guild.id, role.id)):
                    plan = plan_schedule_role_update(
                        snapshot.resolution,
                        tuple(member.id for member in role.members),
                        mode=mode,
                        include_duplicates=include_duplicates,
                    )
                    execution = await _apply_schedule_role_plan(
                        role,
                        plan,
                        members_by_id,
                    )
                result = _format_schedule_role_result(
                    role,
                    snapshot.resolution,
                    plan,
                    execution,
                    members_by_id,
                    duplicate_decision=duplicate_decision,
                )
                if (
                    execution.add_failed_member_ids
                    or execution.remove_failed_member_ids
                ):
                    self.logger.warning(
                        "Schedule role operation had partial failures. "
                        "guild=%s role=%s adds_failed=%s removes_failed=%s",
                        source.guild.id,
                        role.id,
                        len(execution.add_failed_member_ids),
                        len(execution.remove_failed_member_ids),
                    )
                for message in _split_shift_report(result):
                    await interaction.followup.send(message, ephemeral=True)
                return

            async with self.schedule_role_lock((source.guild.id, role.id)):
                plan = plan_schedule_role_update(
                    snapshot.resolution,
                    tuple(member.id for member in role.members),
                    mode=mode,
                    include_duplicates=False,
                )
                execution = await _apply_schedule_role_plan(
                    role,
                    plan,
                    members_by_id,
                )
            result = _format_schedule_role_result(
                role,
                snapshot.resolution,
                plan,
                execution,
                members_by_id,
                duplicate_decision=None,
            )
            if execution.add_failed_member_ids or execution.remove_failed_member_ids:
                self.logger.warning(
                    "Schedule role operation had partial failures. "
                    "guild=%s role=%s adds_failed=%s removes_failed=%s",
                    source.guild.id,
                    role.id,
                    len(execution.add_failed_member_ids),
                    len(execution.remove_failed_member_ids),
                )
            await _replace_with_shift_report(interaction, result)
        except FinalScheduleInputError:
            await _edit_schedule_role_control_message(
                interaction,
                control_message,
                (
                    "⚠️ role 更新未完成，未變更任何 role。"
                    if control_message is not None
                    else (
                        f"⚠️ {config.CONFUSED_EMOJI} "
                        "Final Schedule Range 格式無效，未變更任何 role。"
                    )
                ),
            )
        except StorageError as exc:
            if control_message is not None:
                await _edit_schedule_role_control_message(
                    interaction,
                    control_message,
                    _replace_schedule_role_state_line(
                        control_content or "",
                        "⚠️ role 更新未完成，未變更任何 role。",
                    ),
                )
            await self._send_interaction_storage_error_or_raise(
                interaction,
                exc,
                source=source,
                operation="shift_register_assign_schedule_role",
            )
        except Exception as exc:  # noqa: BLE001
            if control_message is not None:
                await _edit_schedule_role_control_message(
                    interaction,
                    control_message,
                    _replace_schedule_role_state_line(
                        control_content or "",
                        "⚠️ role 更新未完成，未變更任何 role。",
                    ),
                )
            await self._send_interaction_storage_error_or_raise(
                interaction,
                exc,
                source=source,
                operation="shift_register_assign_schedule_role",
            )

    @staticmethod
    def _format_draft_report(
        schedule: DraftSchedule,
        draft_sheet_url: str,
        member_mentions: dict[str, str],
        *,
        encore_power_threshold: float,
        recruitment_ranges: RecruitmentTimeRanges,
        team_summary_url: str | None,
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
            f"- Runner（ランナー）：{schedule.runner or '`Not set`'}",
            f"- 安可綜合力閾值：{encore_power_threshold:g}",
        ]
        if team_summary_url is not None:
            lines.append(f"🔄 已同步 [Team Summary]({team_summary_url})")
        lines.append(
            f"‼️ 已將班表寫入 [Shift Draft]({draft_sheet_url})，並覆蓋原有內容。"
        )
        if team_source_warning is not None:
            lines.append(team_source_warning)
        if unregistered_usernames:
            lines.append(
                "⚠️ 編成未登録："
                + "、".join(
                    _format_draft_username(username, schedule, member_mentions)
                    for username in unregistered_usernames
                )
            )
        lines.append(f"- 募集時間【{recruitment_ranges.announcement_display()}】")
        assignment_rows: list[ShiftReportAssignment] = []
        if schedule.display_names:
            for assignment in report_assignments:
                encore_username = assignment.supporter_usernames_by_slot.get(
                    ENCORE_SUPPORTER_SLOT
                )
                encore_name = (
                    _format_draft_username(
                        encore_username,
                        schedule,
                        member_mentions,
                    )
                    if encore_username is not None
                    else None
                )
                main_names = tuple(
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
                    _format_draft_username(
                        standby_username,
                        schedule,
                        member_mentions,
                    )
                    if standby_username is not None
                    else None
                )
                assignment_rows.append(
                    ShiftReportAssignment(
                        hour=assignment.hour,
                        encore=encore_name,
                        honso=main_names,
                        standby=standby_name,
                    )
                )
        else:
            report_assignments = []
        lines.extend(
            _format_shift_assignment_section(
                assignment_rows,
                empty=not schedule.display_names,
            )
        )
        unassigned_assignments = [
            assignment
            for assignment in report_assignments
            if assignment.unassigned_usernames
        ]
        if unassigned_assignments:
            lines.append("- 未排入（位置已滿）：")
            lines.extend(
                f"  - -# `{hour_label(assignment.hour)}`："
                + "、".join(
                    _format_draft_username(username, schedule, member_mentions)
                    for username in assignment.unassigned_usernames
                )
                for assignment in unassigned_assignments
            )
        lines.append(
            "附件包含生成時資料的 Notes 快照與 LLM 排班 prompt，不會隨 Sheet 調整更新。"
        )
        return "\n".join(lines)


async def setup(bot: Rhoboto) -> None:
    await bot.add_cog(ShiftRegister(bot))
