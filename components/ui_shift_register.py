from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from discord import ButtonStyle, Embed, Interaction, Object, TextStyle
from discord.ui import Button, ChannelSelect, Modal, TextInput, View

from bot import config
from components.ui_auto_guide import (
    LATEST_GUIDE_FIELD_NAME,
    LatestGuideButton,
    LatestGuideRefreshCallback,
    LatestGuideStateResolver,
    LatestGuideToggleCallback,
    latest_guide_status_value,
    refresh_latest_guide_after_settings_save,
    resolve_latest_guide_enabled,
)
from components.ui_permissions import require_settings_permissions
from components.ui_settings_flow import (
    SETTINGS_STORAGE_EXCEPTIONS,
    SettingsPanel,
    SettingsTimeoutView,
    prepare_replacement_settings_view,
    send_current_panel_followup,
    send_settings_contract_error,
    send_settings_partial_success,
    send_settings_refresh_failure,
    send_settings_storage_error,
    send_settings_view_followup,
    send_stale_setup_panel_if_configured,
    settings_description,
    settings_title,
)
from utils.google_sheets_errors import GoogleSheetsError, GoogleSheetsErrorKind
from utils.google_sheets_urls import (
    extract_google_sheet_id,
    google_sheet_url_with_gid,
    normalize_google_sheet_url,
)
from utils.shift_register_manager import (
    SHIFT_REGISTER_SHEET_WRITE_LOCK,
    TeamSourceResolution,
    TeamSourceStatus,
    fresh_shift_channel_transaction,
)
from utils.shift_register_structs import (
    DraftWorksheetMetadata,
    EntryWorksheetMetadata,
    FinalScheduleWorksheetMetadata,
    HourRangeFormatError,
    RecruitmentTimeRanges,
    ShiftRegisterGoogleSheetsMetadata,
)
from utils.shift_register_timeline import (
    ShiftTimelineInput,
    ShiftTimelineParseError,
    as_jst,
    format_iso_hour,
    parse_shift_timeline_input,
)
from utils.structs_base import WorksheetContractError

if TYPE_CHECKING:
    from datetime import date, datetime

    from models.shift_register import ShiftRegisterConfig
    from utils.shift_register_manager import ShiftRegisterManager


SHIFT_REGISTER_DISPLAY_NAME = "Shift Register"
SHIFT_REGISTER_CONTROLS = (
    "Use the buttons below to update sheet settings, shift timeline, "
    "or recruitment time range."
)
SHIFT_REGISTER_CURRENT_CONTROLS = SHIFT_REGISTER_CONTROLS
SHIFT_REGISTER_SAVED_CONTROLS = SHIFT_REGISTER_CONTROLS
SHIFT_REGISTER_SETTINGS_MISSING_MESSAGE = (
    "Shift Register settings are no longer configured for this channel."
)
SHIFT_REGISTER_FEATURE_NAME = "shift_register"

logger = logging.getLogger(__name__)


class GenerateShiftScheduleConfirmView(View):
    """Confirm one administrator's destructive Shift Schedule generation request."""

    def __init__(
        self,
        *,
        requesting_user_id: int,
        destination_label: str,
        destination_url: str,
        timeout: float = 20.0,
    ) -> None:
        super().__init__(timeout=timeout)
        self.requesting_user_id = requesting_user_id
        self.destination_label = destination_label
        self.destination_url = destination_url
        self.value: bool | None = None
        self.add_item(GenerateShiftScheduleConfirmButton())
        self.add_item(GenerateShiftScheduleCancelButton())

    async def authorize(self, interaction: Interaction) -> bool:
        if interaction.user.id != self.requesting_user_id:
            await interaction.response.send_message(
                "⚠️ 只有執行此 command 的管理員可以操作。",
                ephemeral=True,
            )
            return False
        if await require_settings_permissions(interaction):
            return True
        self.value = False
        self.stop()
        return False


class GenerateShiftScheduleConfirmButton(Button):
    def __init__(self) -> None:
        super().__init__(label="確認生成", style=ButtonStyle.danger)

    async def callback(self, interaction: Interaction) -> None:
        view = self.view
        if not isinstance(
            view, GenerateShiftScheduleConfirmView
        ) or not await view.authorize(interaction):
            return
        view.value = True
        await interaction.response.edit_message(
            content=(
                "已確認生成，正在處理 "  # noqa: RUF001
                f"[{view.destination_label}]({view.destination_url})。"
            ),
            view=None,
        )
        view.stop()


class GenerateShiftScheduleCancelButton(Button):
    def __init__(self) -> None:
        super().__init__(label="取消", style=ButtonStyle.secondary)

    async def callback(self, interaction: Interaction) -> None:
        view = self.view
        if not isinstance(
            view, GenerateShiftScheduleConfirmView
        ) or not await view.authorize(interaction):
            return
        view.value = False
        await interaction.response.edit_message(
            content=(
                f"✖️ 已取消生成，未變更 {view.destination_label}。"  # noqa: RUF001
            ),
            view=None,
        )
        view.stop()


async def send_shift_settings_missing(interaction: Interaction) -> None:
    if interaction.response.is_done():
        await interaction.followup.send(
            SHIFT_REGISTER_SETTINGS_MISSING_MESSAGE,
            ephemeral=True,
        )
    else:
        await interaction.response.send_message(
            SHIFT_REGISTER_SETTINGS_MISSING_MESSAGE,
            ephemeral=True,
        )


async def get_fresh_shift_register_config_or_respond(
    shift_register_manager: ShiftRegisterManager,
    interaction: Interaction,
) -> ShiftRegisterConfig | None:
    try:
        shift_register = await shift_register_manager.get_fresh_sheet_config()
    except SETTINGS_STORAGE_EXCEPTIONS as exc:
        await send_settings_storage_error(
            interaction,
            exc,
            operation="shift_register_settings_fetch_config",
            feature_name=SHIFT_REGISTER_FEATURE_NAME,
            log=logger,
        )
        return None
    if shift_register is None:
        await send_shift_settings_missing(interaction)
        return None
    return shift_register


async def build_shift_register_settings_panel(
    shift_register_manager: ShiftRegisterManager,
    shift_register: ShiftRegisterConfig,
    *,
    is_save_action: bool = False,
    metadata: ShiftRegisterGoogleSheetsMetadata | None = None,
    latest_guide_enabled: bool = False,
    latest_guide_toggle_callback: LatestGuideToggleCallback | None = None,
    latest_guide_state_resolver: LatestGuideStateResolver | None = None,
    latest_guide_refresh_callback: LatestGuideRefreshCallback | None = None,
) -> SettingsPanel:
    active_metadata = (
        metadata or await shift_register_manager.fetch_google_sheets_metadata()
    )
    final_schedule_anchor_cell = shift_register.final_schedule_anchor_cell
    latest_guide_enabled = await resolve_latest_guide_enabled(
        enabled=latest_guide_enabled,
        state_resolver=latest_guide_state_resolver,
    )
    team_source = await shift_register_manager.resolve_team_source()
    embed = build_current_settings_embed(
        sheet_url=shift_register.sheet_url,
        metadata=active_metadata,
        final_schedule_anchor_cell=final_schedule_anchor_cell,
        shift_register=shift_register,
        color=config.DEFAULT_EMBED_COLOR,
        is_save_action=is_save_action,
        latest_guide_enabled=latest_guide_enabled,
        team_source=team_source,
    )
    view = ShiftRegisterView(
        shift_register_manager=shift_register_manager,
        has_existing_settings=True,
        sheet_url=shift_register.sheet_url,
        entry_worksheet_title=active_metadata.entry_worksheets.title,
        draft_worksheet_title=active_metadata.draft_worksheet.title,
        final_schedule_worksheet_title=active_metadata.final_schedule_worksheet.title,
        final_schedule_anchor_cell=final_schedule_anchor_cell,
        selected_team_channel_id=(
            team_source.source.config.feature_channel.channel_id
            if team_source.status is TeamSourceStatus.AVAILABLE
            and team_source.source is not None
            else None
        ),
        latest_guide_enabled=latest_guide_enabled,
        latest_guide_toggle_callback=latest_guide_toggle_callback,
        latest_guide_state_resolver=latest_guide_state_resolver,
        latest_guide_refresh_callback=latest_guide_refresh_callback,
    )
    return SettingsPanel(embed=embed, view=view)


def _format_optional_value(value: object | None) -> str:
    if value is None:
        return "Not set"
    return str(value)


def _format_modal_date(value: date | None) -> str:
    if value is None:
        return ""
    return value.isoformat()


def _format_modal_datetime(value: datetime | None) -> str:
    if value is None:
        return ""
    return as_jst(value).strftime("%Y-%m-%d %H")


def _format_settings_date(value: date | None) -> str:
    if value is None:
        return "Not set"
    return value.isoformat()


def _format_settings_datetime(value: datetime | None) -> str:
    if value is None:
        return "Not set"
    return format_iso_hour(value)


def _format_team_source(  # noqa: PLR0911
    resolution: TeamSourceResolution,
) -> str:
    source = resolution.source
    if resolution.status is TeamSourceStatus.AVAILABLE and source is not None:
        worksheet = next(
            (
                worksheet
                for worksheet in source.metadata
                if worksheet.id == source.config.landing_worksheet_id
            ),
            None,
        )
        if worksheet is None or worksheet.id is None:
            return (
                "- The configured Team source is invalid. Repair its worksheet "
                "settings."
            )
        worksheet_url = google_sheet_url_with_gid(
            source.config.sheet_url,
            worksheet.id,
        )
        return (
            f"- **Channel** = <#{source.config.feature_channel.channel_id}>\n"
            f"- **Google Sheet** = [Open Team Register Sheet]({worksheet_url})"
        )
    if resolution.status is TeamSourceStatus.UNSET:
        return (
            "- No Team source is selected. Shift registrations will continue without "
            "Team references."
        )
    if resolution.status is TeamSourceStatus.MISSING:
        return "- No configured Team Register exists in this server."
    if resolution.status is TeamSourceStatus.AMBIGUOUS:
        return (
            "- Multiple Team Registers are configured. Use Edit Team Source "
            "to select one."
        )
    if resolution.status is TeamSourceStatus.INVALID:
        return (
            "- The configured Team source is invalid. Repair its worksheet "
            "settings or header."
        )
    return "- The Team source could not be read at this time."


def _timeline_defaults_from_config(
    shift_register: ShiftRegisterConfig,
) -> dict[str, str]:
    day_number = shift_register.day_number
    return {
        "day_number": "" if day_number is None else str(day_number),
        "event_date": _format_modal_date(shift_register.event_date),
        "submission_deadline_at": _format_modal_datetime(
            shift_register.submission_deadline_at
        ),
        "draft_shift_proposal_at": _format_modal_datetime(
            shift_register.draft_shift_proposal_at
        ),
        "final_shift_notice_at": _format_modal_datetime(
            shift_register.final_shift_notice_at
        ),
    }


def _timeline_values_from_modal(modal: ShiftTimelineModal) -> ShiftTimelineInput:
    return ShiftTimelineInput(
        day_number=modal.day_number.value,
        event_date=modal.event_date.value,
        submission_deadline_at=modal.submission_deadline_at.value,
        draft_shift_proposal_at=modal.draft_shift_proposal_at.value,
        final_shift_notice_at=modal.final_shift_notice_at.value,
    )


def _timeline_defaults_from_modal(modal: ShiftTimelineModal) -> dict[str, str]:
    return {
        "day_number": modal.day_number.value,
        "event_date": modal.event_date.value,
        "submission_deadline_at": modal.submission_deadline_at.value,
        "draft_shift_proposal_at": modal.draft_shift_proposal_at.value,
        "final_shift_notice_at": modal.final_shift_notice_at.value,
    }


async def _send_modal_validation_error(
    interaction: Interaction,
    *,
    title: str,
    errors: list[str],
    view: SettingsTimeoutView,
) -> None:
    error_lines = "\n".join(f"- {error}" for error in errors)
    await interaction.response.send_message(
        f"{title} could not be saved:\n{error_lines}",
        view=view,
        ephemeral=True,
    )


async def _send_saved_shift_register_panel(
    interaction: Interaction,
    shift_register_manager: ShiftRegisterManager,
    *,
    latest_guide_enabled: bool = False,
    latest_guide_toggle_callback: LatestGuideToggleCallback | None = None,
    latest_guide_state_resolver: LatestGuideStateResolver | None = None,
    latest_guide_refresh_callback: LatestGuideRefreshCallback | None = None,
) -> None:
    try:
        shift_register = await shift_register_manager.get_sheet_config()
        panel = await build_shift_register_settings_panel(
            shift_register_manager,
            shift_register,
            is_save_action=True,
            latest_guide_enabled=latest_guide_enabled,
            latest_guide_toggle_callback=latest_guide_toggle_callback,
            latest_guide_state_resolver=latest_guide_state_resolver,
            latest_guide_refresh_callback=latest_guide_refresh_callback,
        )
    except SETTINGS_STORAGE_EXCEPTIONS as exc:
        await send_settings_refresh_failure(
            interaction,
            exc,
            operation="shift_register_settings_refresh",
            feature_name=SHIFT_REGISTER_FEATURE_NAME,
            log=logger,
        )
        return
    await send_current_panel_followup(interaction, panel)
    await refresh_latest_guide_after_settings_save(
        interaction,
        shift_register,
        latest_guide_refresh_callback,
    )


class ShiftRegisterSheetModal(Modal):
    """Modal for shift register setup. Only collects user input and calls callback."""

    def __init__(
        self,
        shift_register_manager: ShiftRegisterManager,
        sheet_url: str = "",
        entry_worksheet_title: str | None = None,
        draft_worksheet_title: str | None = None,
        final_schedule_worksheet_title: str | None = None,
        final_schedule_anchor_cell: str = "A1",
        *,
        requires_existing_settings: bool = False,
        latest_guide_enabled: bool = False,
        latest_guide_toggle_callback: LatestGuideToggleCallback | None = None,
        latest_guide_state_resolver: LatestGuideStateResolver | None = None,
        latest_guide_refresh_callback: LatestGuideRefreshCallback | None = None,
    ) -> None:
        super().__init__(title="Shift Register Setup")
        entry_worksheet_title = entry_worksheet_title or next(
            EntryWorksheetMetadata.default_title_generator()
        )
        draft_worksheet_title = draft_worksheet_title or next(
            DraftWorksheetMetadata.default_title_generator()
        )
        final_schedule_worksheet_title = final_schedule_worksheet_title or next(
            FinalScheduleWorksheetMetadata.default_title_generator()
        )

        self.sheet_url: TextInput = TextInput(
            label="Google Sheet Link",
            placeholder="Paste your Google Sheet URL here",
            default=sheet_url,
            required=True,
            style=TextStyle.short,
        )
        self.entry_worksheet_title: TextInput = TextInput(
            label="Entry Worksheet Title",
            placeholder="Title for the entry worksheet",
            default=entry_worksheet_title,
            required=True,
            style=TextStyle.short,
        )
        self.draft_worksheet_title: TextInput = TextInput(
            label="Draft Worksheet Title",
            placeholder="Title for the draft worksheet",
            default=draft_worksheet_title,
            required=True,
            style=TextStyle.short,
        )
        self.final_schedule_worksheet_title: TextInput = TextInput(
            label="Final Schedule Worksheet Title",
            placeholder="Title for the final schedule worksheet",
            default=final_schedule_worksheet_title,
            required=True,
            style=TextStyle.short,
        )
        self.final_schedule_anchor_cell: TextInput = TextInput(
            label="Final Schedule Anchor Cell",
            placeholder=(
                "e.g. A1. Will be used to anchor the final schedule in the worksheet."
            ),
            default=final_schedule_anchor_cell,
            required=True,
            style=TextStyle.short,
        )
        self.add_item(self.sheet_url)
        self.add_item(self.entry_worksheet_title)
        self.add_item(self.draft_worksheet_title)
        self.add_item(self.final_schedule_worksheet_title)
        self.add_item(self.final_schedule_anchor_cell)
        self.shift_register_manager = shift_register_manager
        self.requires_existing_settings = requires_existing_settings
        self.latest_guide_enabled = latest_guide_enabled
        self.latest_guide_toggle_callback = latest_guide_toggle_callback
        self.latest_guide_state_resolver = latest_guide_state_resolver
        self.latest_guide_refresh_callback = latest_guide_refresh_callback

    async def on_submit(self, interaction: Interaction) -> None:
        """
        Handle modal submission for Shift Register setup.

        Args:
            interaction (Interaction): Discord interaction object.
        """
        if not await require_settings_permissions(interaction):
            return

        if self.requires_existing_settings:
            shift_register = await get_fresh_shift_register_config_or_respond(
                self.shift_register_manager,
                interaction,
            )
            if shift_register is None:
                return

        await interaction.response.defer(ephemeral=True)

        entry_worksheet_title = self.entry_worksheet_title.value
        draft_worksheet_title = self.draft_worksheet_title.value
        final_schedule_worksheet_title = self.final_schedule_worksheet_title.value
        final_schedule_anchor_cell = self.final_schedule_anchor_cell.value

        settings_saved = False
        try:
            sheet_url = normalize_google_sheet_url(self.sheet_url.value)
            extract_google_sheet_id(sheet_url)
            async with SHIFT_REGISTER_SHEET_WRITE_LOCK(
                self.shift_register_manager.feature_channel.channel_id
            ):
                upsert = self.shift_register_manager.upsert_sheet_config_and_worksheets
                metadata = await upsert(
                    sheet_url=sheet_url,
                    entry_worksheet_title=entry_worksheet_title,
                    draft_worksheet_title=draft_worksheet_title,
                    final_schedule_worksheet_title=final_schedule_worksheet_title,
                    final_schedule_anchor_cell=final_schedule_anchor_cell,
                )
                settings_saved = True
        except WorksheetContractError as error:
            await send_settings_contract_error(
                interaction,
                error,
                operation="shift_register_setup",
                feature_name=SHIFT_REGISTER_FEATURE_NAME,
                log=logger,
            )
            return
        except ValueError as exc:
            error = GoogleSheetsError(
                GoogleSheetsErrorKind.INVALID_URL,
                "Check the Google Sheet link and save the settings again.",
            )
            error.__cause__ = exc
            await send_settings_storage_error(
                interaction,
                error,
                operation="shift_register_setup",
                feature_name=SHIFT_REGISTER_FEATURE_NAME,
                log=logger,
            )
            return
        except SETTINGS_STORAGE_EXCEPTIONS as exc:
            responder = (
                send_settings_partial_success
                if settings_saved
                else send_settings_storage_error
            )
            await responder(
                interaction,
                exc,
                operation="shift_register_setup",
                feature_name=SHIFT_REGISTER_FEATURE_NAME,
                log=logger,
            )
            return

        try:
            shift_register = await self.shift_register_manager.get_sheet_config()
            if self.requires_existing_settings:
                panel = await build_shift_register_settings_panel(
                    self.shift_register_manager,
                    shift_register,
                    is_save_action=True,
                    metadata=metadata,
                    latest_guide_enabled=self.latest_guide_enabled,
                    latest_guide_toggle_callback=self.latest_guide_toggle_callback,
                    latest_guide_state_resolver=self.latest_guide_state_resolver,
                    latest_guide_refresh_callback=self.latest_guide_refresh_callback,
                )
            else:
                team_source_content, team_source_view = await build_team_source_view(
                    self.shift_register_manager,
                    selected_channel_id=None,
                    return_label="Set Later",
                    is_save_action=True,
                    latest_guide_enabled=self.latest_guide_enabled,
                    latest_guide_toggle_callback=self.latest_guide_toggle_callback,
                    latest_guide_state_resolver=self.latest_guide_state_resolver,
                    latest_guide_refresh_callback=self.latest_guide_refresh_callback,
                )
        except SETTINGS_STORAGE_EXCEPTIONS as exc:
            await send_settings_refresh_failure(
                interaction,
                exc,
                operation="shift_register_setup_refresh_panel",
                feature_name=SHIFT_REGISTER_FEATURE_NAME,
                log=logger,
            )
            return
        if self.requires_existing_settings:
            await send_current_panel_followup(interaction, panel)
        else:
            await send_settings_view_followup(
                interaction,
                content=team_source_content,
                view=team_source_view,
            )
        await refresh_latest_guide_after_settings_save(
            interaction,
            shift_register,
            self.latest_guide_refresh_callback,
        )


class ShiftTimelineModal(Modal):
    """Modal for Shift Register timeline settings."""

    def __init__(
        self,
        shift_register_manager: ShiftRegisterManager,
        *,
        day_number: str = "",
        event_date: str = "",
        submission_deadline_at: str = "",
        draft_shift_proposal_at: str = "",
        final_shift_notice_at: str = "",
        requires_existing_settings: bool = True,
        latest_guide_enabled: bool = False,
        latest_guide_toggle_callback: LatestGuideToggleCallback | None = None,
        latest_guide_state_resolver: LatestGuideStateResolver | None = None,
        latest_guide_refresh_callback: LatestGuideRefreshCallback | None = None,
    ) -> None:
        super().__init__(title="Shift Timeline")
        self.day_number: TextInput = TextInput(
            label="Day Number",
            placeholder="e.g. 2. Leave blank to clear.",
            default=day_number,
            required=False,
            style=TextStyle.short,
        )
        self.event_date: TextInput = TextInput(
            label="Event Date",
            placeholder="YYYY-M-D or YYYY/M/D. Leave blank to clear.",
            default=event_date,
            required=False,
            style=TextStyle.short,
        )
        self.submission_deadline_at: TextInput = TextInput(
            label="Submission Deadline (JST)",
            placeholder="YYYY-M-D HH or M/D HH. Leave blank to clear.",
            default=submission_deadline_at,
            required=False,
            style=TextStyle.short,
        )
        self.draft_shift_proposal_at: TextInput = TextInput(
            label="Draft Shift Proposal (JST)",
            placeholder="YYYY-M-D HH or M/D HH. Leave blank to clear.",
            default=draft_shift_proposal_at,
            required=False,
            style=TextStyle.short,
        )
        self.final_shift_notice_at: TextInput = TextInput(
            label="Final Shift Notice (JST)",
            placeholder="YYYY-M-D HH or M/D HH. Leave blank to clear.",
            default=final_shift_notice_at,
            required=False,
            style=TextStyle.short,
        )
        self.add_item(self.day_number)
        self.add_item(self.event_date)
        self.add_item(self.submission_deadline_at)
        self.add_item(self.draft_shift_proposal_at)
        self.add_item(self.final_shift_notice_at)
        self.shift_register_manager = shift_register_manager
        self.requires_existing_settings = requires_existing_settings
        self.latest_guide_enabled = latest_guide_enabled
        self.latest_guide_toggle_callback = latest_guide_toggle_callback
        self.latest_guide_state_resolver = latest_guide_state_resolver
        self.latest_guide_refresh_callback = latest_guide_refresh_callback

    async def on_submit(self, interaction: Interaction) -> None:
        if not await require_settings_permissions(interaction):
            return

        shift_register = None
        if self.requires_existing_settings:
            shift_register = await get_fresh_shift_register_config_or_respond(
                self.shift_register_manager,
                interaction,
            )
            if shift_register is None:
                return

        try:
            values = parse_shift_timeline_input(
                _timeline_values_from_modal(self),
                existing_event_date=(
                    shift_register.event_date if shift_register is not None else None
                ),
            )
        except ShiftTimelineParseError as exc:
            await _send_modal_validation_error(
                interaction,
                title="Shift timeline",
                errors=exc.errors,
                view=ShiftTimelineEditAgainView(
                    self.shift_register_manager,
                    **_timeline_defaults_from_modal(self),
                    latest_guide_enabled=self.latest_guide_enabled,
                    latest_guide_toggle_callback=self.latest_guide_toggle_callback,
                    latest_guide_state_resolver=self.latest_guide_state_resolver,
                    latest_guide_refresh_callback=self.latest_guide_refresh_callback,
                ),
            )
            return

        await interaction.response.defer(ephemeral=True)
        try:
            await self.shift_register_manager.update_timeline(
                day_number=values.day_number,
                event_date=values.event_date,
                submission_deadline_at=values.submission_deadline_at,
                draft_shift_proposal_at=values.draft_shift_proposal_at,
                final_shift_notice_at=values.final_shift_notice_at,
            )
        except SETTINGS_STORAGE_EXCEPTIONS as exc:
            await send_settings_storage_error(
                interaction,
                exc,
                operation="shift_register_timeline_save",
                feature_name=SHIFT_REGISTER_FEATURE_NAME,
                log=logger,
            )
            return
        await _send_saved_shift_register_panel(
            interaction,
            self.shift_register_manager,
            latest_guide_enabled=self.latest_guide_enabled,
            latest_guide_toggle_callback=self.latest_guide_toggle_callback,
            latest_guide_state_resolver=self.latest_guide_state_resolver,
            latest_guide_refresh_callback=self.latest_guide_refresh_callback,
        )


class ShiftRecruitmentRangeModal(Modal):
    """Modal for Shift Register recruitment time range settings."""

    def __init__(
        self,
        shift_register_manager: ShiftRegisterManager,
        *,
        recruitment_time_range: str = "",
        requires_existing_settings: bool = True,
        latest_guide_enabled: bool = False,
        latest_guide_toggle_callback: LatestGuideToggleCallback | None = None,
        latest_guide_state_resolver: LatestGuideStateResolver | None = None,
        latest_guide_refresh_callback: LatestGuideRefreshCallback | None = None,
    ) -> None:
        super().__init__(title="Recruitment Time Range")
        self.recruitment_time_range: TextInput = TextInput(
            label="Recruitment Time Range",
            placeholder="e.g. 4-28 or 4-12, 20-28. Leave blank to reset to 4-28.",
            default=recruitment_time_range,
            required=False,
            style=TextStyle.short,
        )
        self.add_item(self.recruitment_time_range)
        self.shift_register_manager = shift_register_manager
        self.requires_existing_settings = requires_existing_settings
        self.latest_guide_enabled = latest_guide_enabled
        self.latest_guide_toggle_callback = latest_guide_toggle_callback
        self.latest_guide_state_resolver = latest_guide_state_resolver
        self.latest_guide_refresh_callback = latest_guide_refresh_callback

    async def on_submit(self, interaction: Interaction) -> None:
        if not await require_settings_permissions(interaction):
            return

        if self.requires_existing_settings:
            shift_register = await get_fresh_shift_register_config_or_respond(
                self.shift_register_manager,
                interaction,
            )
            if shift_register is None:
                return

        try:
            ranges = RecruitmentTimeRanges.from_modal_input(
                self.recruitment_time_range.value
            )
        except HourRangeFormatError:
            await _send_modal_validation_error(
                interaction,
                title="Recruitment time range",
                errors=[
                    "Use ranges like 4-28 or 4-12, 20-28 within 0-30.",
                ],
                view=ShiftRecruitmentRangeEditAgainView(
                    self.shift_register_manager,
                    recruitment_time_range=self.recruitment_time_range.value,
                    latest_guide_enabled=self.latest_guide_enabled,
                    latest_guide_toggle_callback=self.latest_guide_toggle_callback,
                    latest_guide_state_resolver=self.latest_guide_state_resolver,
                    latest_guide_refresh_callback=self.latest_guide_refresh_callback,
                ),
            )
            return

        await interaction.response.defer(ephemeral=True)
        try:
            async with fresh_shift_channel_transaction(
                self.shift_register_manager,
                SHIFT_REGISTER_SHEET_WRITE_LOCK,
                channel_id=self.shift_register_manager.feature_channel.channel_id,
            ):
                await self.shift_register_manager.update_recruitment_time_ranges(ranges)
        except WorksheetContractError as error:
            await send_settings_contract_error(
                interaction,
                error,
                operation="shift_register_recruitment_range_save",
                feature_name=SHIFT_REGISTER_FEATURE_NAME,
                log=logger,
            )
            return
        except SETTINGS_STORAGE_EXCEPTIONS as exc:
            await send_settings_storage_error(
                interaction,
                exc,
                operation="shift_register_recruitment_range_save",
                feature_name=SHIFT_REGISTER_FEATURE_NAME,
                log=logger,
            )
            return
        await _send_saved_shift_register_panel(
            interaction,
            self.shift_register_manager,
            latest_guide_enabled=self.latest_guide_enabled,
            latest_guide_toggle_callback=self.latest_guide_toggle_callback,
            latest_guide_state_resolver=self.latest_guide_state_resolver,
            latest_guide_refresh_callback=self.latest_guide_refresh_callback,
        )


class ShiftRegisterButton(Button):
    """Dynamic button for shift register setup/edit."""

    def __init__(
        self,
        label: str,
        shift_register_manager: ShiftRegisterManager,
        sheet_url: str = "",
        entry_worksheet_title: str | None = None,
        draft_worksheet_title: str | None = None,
        final_schedule_worksheet_title: str | None = None,
        final_schedule_anchor_cell: str = "A1",
        *,
        requires_existing_settings: bool = False,
        latest_guide_enabled: bool = False,
        latest_guide_toggle_callback: LatestGuideToggleCallback | None = None,
        latest_guide_state_resolver: LatestGuideStateResolver | None = None,
        latest_guide_refresh_callback: LatestGuideRefreshCallback | None = None,
    ) -> None:
        super().__init__(label=label, style=ButtonStyle.primary)
        self.shift_register_manager = shift_register_manager
        self.sheet_url = sheet_url
        self.entry_worksheet_title = entry_worksheet_title
        self.draft_worksheet_title = draft_worksheet_title
        self.final_schedule_worksheet_title = final_schedule_worksheet_title
        self.final_schedule_anchor_cell = final_schedule_anchor_cell
        self.requires_existing_settings = requires_existing_settings
        self.latest_guide_enabled = latest_guide_enabled
        self.latest_guide_toggle_callback = latest_guide_toggle_callback
        self.latest_guide_state_resolver = latest_guide_state_resolver
        self.latest_guide_refresh_callback = latest_guide_refresh_callback

    async def callback(self, interaction: Interaction) -> None:
        if not await require_settings_permissions(interaction):
            return

        async def build_current_panel(shift_register: object) -> SettingsPanel:
            return await build_shift_register_settings_panel(
                self.shift_register_manager,
                shift_register,
                latest_guide_enabled=self.latest_guide_enabled,
                latest_guide_toggle_callback=self.latest_guide_toggle_callback,
                latest_guide_state_resolver=self.latest_guide_state_resolver,
                latest_guide_refresh_callback=self.latest_guide_refresh_callback,
            )

        if not self.requires_existing_settings:
            handled = await send_stale_setup_panel_if_configured(
                interaction,
                self.shift_register_manager,
                feature_name=SHIFT_REGISTER_FEATURE_NAME,
                feature_display_name=SHIFT_REGISTER_DISPLAY_NAME,
                build_current_panel=build_current_panel,
                log=logger,
            )
            if handled:
                return

        if self.requires_existing_settings:
            shift_register = await get_fresh_shift_register_config_or_respond(
                self.shift_register_manager,
                interaction,
            )
            if shift_register is None:
                return
            sheet_url = shift_register.sheet_url
            entry_worksheet_title = self.entry_worksheet_title
            draft_worksheet_title = self.draft_worksheet_title
            final_schedule_worksheet_title = self.final_schedule_worksheet_title
            final_schedule_anchor_cell = shift_register.final_schedule_anchor_cell
        else:
            sheet_url = self.sheet_url
            entry_worksheet_title = self.entry_worksheet_title
            draft_worksheet_title = self.draft_worksheet_title
            final_schedule_worksheet_title = self.final_schedule_worksheet_title
            final_schedule_anchor_cell = self.final_schedule_anchor_cell

        await interaction.response.send_modal(
            ShiftRegisterSheetModal(
                shift_register_manager=self.shift_register_manager,
                sheet_url=sheet_url,
                entry_worksheet_title=entry_worksheet_title,
                draft_worksheet_title=draft_worksheet_title,
                final_schedule_worksheet_title=final_schedule_worksheet_title,
                final_schedule_anchor_cell=final_schedule_anchor_cell,
                requires_existing_settings=self.requires_existing_settings,
                latest_guide_enabled=self.latest_guide_enabled,
                latest_guide_toggle_callback=self.latest_guide_toggle_callback,
                latest_guide_state_resolver=self.latest_guide_state_resolver,
                latest_guide_refresh_callback=self.latest_guide_refresh_callback,
            )
        )


class ShiftTimelineButton(Button):
    """Button that opens the Shift Timeline modal with fresh saved values."""

    def __init__(
        self,
        shift_register_manager: ShiftRegisterManager,
        *,
        latest_guide_enabled: bool = False,
        latest_guide_toggle_callback: LatestGuideToggleCallback | None = None,
        latest_guide_state_resolver: LatestGuideStateResolver | None = None,
        latest_guide_refresh_callback: LatestGuideRefreshCallback | None = None,
    ) -> None:
        super().__init__(label="Edit Shift Timeline", style=ButtonStyle.secondary)
        self.shift_register_manager = shift_register_manager
        self.latest_guide_enabled = latest_guide_enabled
        self.latest_guide_toggle_callback = latest_guide_toggle_callback
        self.latest_guide_state_resolver = latest_guide_state_resolver
        self.latest_guide_refresh_callback = latest_guide_refresh_callback

    async def callback(self, interaction: Interaction) -> None:
        if not await require_settings_permissions(interaction):
            return

        shift_register = await get_fresh_shift_register_config_or_respond(
            self.shift_register_manager,
            interaction,
        )
        if shift_register is None:
            return

        await interaction.response.send_modal(
            ShiftTimelineModal(
                self.shift_register_manager,
                **_timeline_defaults_from_config(shift_register),
                latest_guide_enabled=self.latest_guide_enabled,
                latest_guide_toggle_callback=self.latest_guide_toggle_callback,
                latest_guide_state_resolver=self.latest_guide_state_resolver,
                latest_guide_refresh_callback=self.latest_guide_refresh_callback,
            )
        )


class ShiftRecruitmentRangeButton(Button):
    """Button that opens the recruitment time range modal with fresh values."""

    def __init__(
        self,
        shift_register_manager: ShiftRegisterManager,
        *,
        latest_guide_enabled: bool = False,
        latest_guide_toggle_callback: LatestGuideToggleCallback | None = None,
        latest_guide_state_resolver: LatestGuideStateResolver | None = None,
        latest_guide_refresh_callback: LatestGuideRefreshCallback | None = None,
    ) -> None:
        super().__init__(
            label="Edit Recruitment Time Range",
            style=ButtonStyle.secondary,
        )
        self.shift_register_manager = shift_register_manager
        self.latest_guide_enabled = latest_guide_enabled
        self.latest_guide_toggle_callback = latest_guide_toggle_callback
        self.latest_guide_state_resolver = latest_guide_state_resolver
        self.latest_guide_refresh_callback = latest_guide_refresh_callback

    async def callback(self, interaction: Interaction) -> None:
        if not await require_settings_permissions(interaction):
            return

        shift_register = await get_fresh_shift_register_config_or_respond(
            self.shift_register_manager,
            interaction,
        )
        if shift_register is None:
            return

        ranges = RecruitmentTimeRanges.from_json(shift_register.recruitment_time_ranges)
        await interaction.response.send_modal(
            ShiftRecruitmentRangeModal(
                self.shift_register_manager,
                recruitment_time_range=ranges.display(),
                latest_guide_enabled=self.latest_guide_enabled,
                latest_guide_toggle_callback=self.latest_guide_toggle_callback,
                latest_guide_state_resolver=self.latest_guide_state_resolver,
                latest_guide_refresh_callback=self.latest_guide_refresh_callback,
            )
        )


class TeamSourceSelect(ChannelSelect):
    """Draft a Team Register channel without saving it."""

    def __init__(self, selected_channel_id: int | None = None) -> None:
        super().__init__(
            placeholder="Select a Team Register channel",
            min_values=1,
            max_values=1,
            default_values=(
                [Object(id=selected_channel_id)]
                if selected_channel_id is not None
                else None
            ),
        )

    async def callback(self, interaction: Interaction) -> None:
        if not await require_settings_permissions(interaction):
            return
        if not isinstance(self.view, TeamSourceView):
            return
        self.view.selected_channel_id = self.values[0].id
        await interaction.response.edit_message(view=self.view)


async def build_team_source_view(
    shift_register_manager: ShiftRegisterManager,
    *,
    selected_channel_id: int | None,
    return_label: str,
    is_save_action: bool = False,
    latest_guide_enabled: bool = False,
    latest_guide_toggle_callback: LatestGuideToggleCallback | None = None,
    latest_guide_state_resolver: LatestGuideStateResolver | None = None,
    latest_guide_refresh_callback: LatestGuideRefreshCallback | None = None,
) -> tuple[str, TeamSourceView]:
    """Build the optional Team Source selection view for a Shift Register."""
    candidate_channel_ids = (
        await shift_register_manager.get_team_source_candidate_channel_ids()
    )
    if not candidate_channel_ids:
        return (
            "⚠️ No Team Register is configured in this server. "
            "Shift registrations will continue without Team references.",
            TeamSourceView(
                shift_register_manager,
                has_candidates=False,
                return_label=return_label,
                is_save_action=is_save_action,
                latest_guide_enabled=latest_guide_enabled,
                latest_guide_toggle_callback=latest_guide_toggle_callback,
                latest_guide_state_resolver=latest_guide_state_resolver,
                latest_guide_refresh_callback=latest_guide_refresh_callback,
            ),
        )
    return (
        "Team Source is optional. Shift registrations will continue without Team "
        "references until you apply one.",
        TeamSourceView(
            shift_register_manager,
            selected_channel_id=(
                selected_channel_id
                if selected_channel_id is not None
                else candidate_channel_ids[0]
                if len(candidate_channel_ids) == 1
                else None
            ),
            has_candidates=True,
            return_label=return_label,
            is_save_action=is_save_action,
            latest_guide_enabled=latest_guide_enabled,
            latest_guide_toggle_callback=latest_guide_toggle_callback,
            latest_guide_state_resolver=latest_guide_state_resolver,
            latest_guide_refresh_callback=latest_guide_refresh_callback,
        ),
    )


class ReturnToShiftSettingsButton(Button):
    def __init__(self, label: str) -> None:
        super().__init__(label=label, style=ButtonStyle.secondary)

    async def callback(self, interaction: Interaction) -> None:
        if not await require_settings_permissions(interaction):
            return
        if not isinstance(self.view, TeamSourceView):
            return
        await interaction.response.defer()
        try:
            panel = await self.view.build_settings_panel()
        except SETTINGS_STORAGE_EXCEPTIONS as exc:
            await send_settings_refresh_failure(
                interaction,
                exc,
                operation="shift_register_team_source_return",
                feature_name=SHIFT_REGISTER_FEATURE_NAME,
                log=logger,
                clear_current_message=True,
            )
            return
        view = prepare_replacement_settings_view(self.view, panel.view)
        await interaction.edit_original_response(
            content=None,
            embed=panel.embed,
            view=view,
        )


class ApplyTeamSourceButton(Button):
    def __init__(self) -> None:
        super().__init__(label="Apply & Repair", style=ButtonStyle.primary)

    async def callback(self, interaction: Interaction) -> None:  # noqa: PLR0911
        if not await require_settings_permissions(interaction):
            return
        if not isinstance(self.view, TeamSourceView):
            return
        channel_id = self.view.selected_channel_id
        if channel_id is None:
            await interaction.response.send_message(
                "⚠️ Select a Team Register channel first.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        manager = self.view.shift_register_manager
        try:
            async with fresh_shift_channel_transaction(
                manager,
                SHIFT_REGISTER_SHEET_WRITE_LOCK,
                channel_id=manager.feature_channel.channel_id,
            ):
                resolution = await manager.select_team_source_and_repair(channel_id)
        except WorksheetContractError as error:
            await send_settings_contract_error(
                interaction,
                error,
                operation="shift_register_team_source_repair",
                feature_name=SHIFT_REGISTER_FEATURE_NAME,
                log=logger,
            )
            return
        except SETTINGS_STORAGE_EXCEPTIONS as exc:
            await send_settings_storage_error(
                interaction,
                exc,
                operation="shift_register_team_source_repair",
                feature_name=SHIFT_REGISTER_FEATURE_NAME,
                log=logger,
            )
            return

        if resolution.status is not TeamSourceStatus.AVAILABLE:
            await interaction.followup.send(
                _team_source_apply_error(resolution.status),
                ephemeral=True,
            )
            return

        try:
            panel = await self.view.build_settings_panel()
        except SETTINGS_STORAGE_EXCEPTIONS as exc:
            await send_settings_refresh_failure(
                interaction,
                exc,
                operation="shift_register_team_source_refresh",
                feature_name=SHIFT_REGISTER_FEATURE_NAME,
                log=logger,
            )
            return
        await interaction.followup.send(
            "✅ Team source saved and references repaired.",
            embed=panel.embed,
            view=panel.view,
            ephemeral=True,
        )


class TeamSourceView(SettingsTimeoutView):
    def __init__(
        self,
        shift_register_manager: ShiftRegisterManager,
        *,
        selected_channel_id: int | None = None,
        has_candidates: bool = True,
        return_label: str = "Back to Settings",
        is_save_action: bool = False,
        latest_guide_enabled: bool = False,
        latest_guide_toggle_callback: LatestGuideToggleCallback | None = None,
        latest_guide_state_resolver: LatestGuideStateResolver | None = None,
        latest_guide_refresh_callback: LatestGuideRefreshCallback | None = None,
    ) -> None:
        super().__init__()
        self.shift_register_manager = shift_register_manager
        self.selected_channel_id = selected_channel_id
        self.is_save_action = is_save_action
        self.latest_guide_enabled = latest_guide_enabled
        self.latest_guide_toggle_callback = latest_guide_toggle_callback
        self.latest_guide_state_resolver = latest_guide_state_resolver
        self.latest_guide_refresh_callback = latest_guide_refresh_callback
        if has_candidates:
            self.add_item(TeamSourceSelect(selected_channel_id))
            self.add_item(ApplyTeamSourceButton())
        self.add_item(ReturnToShiftSettingsButton(return_label))

    async def build_settings_panel(self) -> SettingsPanel:
        shift_register = await self.shift_register_manager.get_sheet_config()
        return await build_shift_register_settings_panel(
            self.shift_register_manager,
            shift_register,
            is_save_action=self.is_save_action,
            latest_guide_enabled=self.latest_guide_enabled,
            latest_guide_toggle_callback=self.latest_guide_toggle_callback,
            latest_guide_state_resolver=self.latest_guide_state_resolver,
            latest_guide_refresh_callback=self.latest_guide_refresh_callback,
        )


class ManageTeamSourceButton(Button):
    def __init__(
        self,
        shift_register_manager: ShiftRegisterManager,
        *,
        selected_channel_id: int | None = None,
        latest_guide_enabled: bool = False,
        latest_guide_toggle_callback: LatestGuideToggleCallback | None = None,
        latest_guide_state_resolver: LatestGuideStateResolver | None = None,
        latest_guide_refresh_callback: LatestGuideRefreshCallback | None = None,
    ) -> None:
        super().__init__(label="Edit Team Source", style=ButtonStyle.secondary)
        self.shift_register_manager = shift_register_manager
        self.selected_channel_id = selected_channel_id
        self.latest_guide_enabled = latest_guide_enabled
        self.latest_guide_toggle_callback = latest_guide_toggle_callback
        self.latest_guide_state_resolver = latest_guide_state_resolver
        self.latest_guide_refresh_callback = latest_guide_refresh_callback

    async def callback(self, interaction: Interaction) -> None:
        if not await require_settings_permissions(interaction):
            return
        await interaction.response.defer()
        try:
            content, view = await build_team_source_view(
                self.shift_register_manager,
                selected_channel_id=self.selected_channel_id,
                return_label="Back to Settings",
                latest_guide_enabled=self.latest_guide_enabled,
                latest_guide_toggle_callback=self.latest_guide_toggle_callback,
                latest_guide_state_resolver=self.latest_guide_state_resolver,
                latest_guide_refresh_callback=self.latest_guide_refresh_callback,
            )
        except SETTINGS_STORAGE_EXCEPTIONS as exc:
            await send_settings_storage_error(
                interaction,
                exc,
                operation="shift_register_team_source_candidates",
                feature_name=SHIFT_REGISTER_FEATURE_NAME,
                log=logger,
            )
            return
        view = prepare_replacement_settings_view(self.view, view)
        await interaction.edit_original_response(
            content=content,
            embed=None,
            view=view,
        )


def _team_source_apply_error(status: TeamSourceStatus) -> str:
    if status is TeamSourceStatus.AMBIGUOUS:
        return "⚠️ More than one Team Register matches this channel."
    if status is TeamSourceStatus.MISSING:
        return "⚠️ No configured Team Register was found in this channel."
    return "⚠️ The selected Team source or Summary worksheet is invalid."


class ShiftTimelineEditAgainButton(Button):
    """Button that reopens an invalid Shift Timeline modal submission."""

    def __init__(
        self,
        shift_register_manager: ShiftRegisterManager,
        *,
        day_number: str,
        event_date: str,
        submission_deadline_at: str,
        draft_shift_proposal_at: str,
        final_shift_notice_at: str,
        latest_guide_enabled: bool = False,
        latest_guide_toggle_callback: LatestGuideToggleCallback | None = None,
        latest_guide_state_resolver: LatestGuideStateResolver | None = None,
        latest_guide_refresh_callback: LatestGuideRefreshCallback | None = None,
    ) -> None:
        super().__init__(label="Edit Again", style=ButtonStyle.primary)
        self.shift_register_manager = shift_register_manager
        self.values = {
            "day_number": day_number,
            "event_date": event_date,
            "submission_deadline_at": submission_deadline_at,
            "draft_shift_proposal_at": draft_shift_proposal_at,
            "final_shift_notice_at": final_shift_notice_at,
        }
        self.latest_guide_enabled = latest_guide_enabled
        self.latest_guide_toggle_callback = latest_guide_toggle_callback
        self.latest_guide_state_resolver = latest_guide_state_resolver
        self.latest_guide_refresh_callback = latest_guide_refresh_callback

    async def callback(self, interaction: Interaction) -> None:
        if not await require_settings_permissions(interaction):
            return

        shift_register = await get_fresh_shift_register_config_or_respond(
            self.shift_register_manager,
            interaction,
        )
        if shift_register is None:
            return

        await interaction.response.send_modal(
            ShiftTimelineModal(
                self.shift_register_manager,
                **self.values,
                latest_guide_enabled=self.latest_guide_enabled,
                latest_guide_toggle_callback=self.latest_guide_toggle_callback,
                latest_guide_state_resolver=self.latest_guide_state_resolver,
                latest_guide_refresh_callback=self.latest_guide_refresh_callback,
            )
        )


class ShiftTimelineEditAgainView(SettingsTimeoutView):
    """Retry view for invalid Shift Timeline modal submissions."""

    def __init__(
        self,
        shift_register_manager: ShiftRegisterManager,
        *,
        day_number: str,
        event_date: str,
        submission_deadline_at: str,
        draft_shift_proposal_at: str,
        final_shift_notice_at: str,
        latest_guide_enabled: bool = False,
        latest_guide_toggle_callback: LatestGuideToggleCallback | None = None,
        latest_guide_state_resolver: LatestGuideStateResolver | None = None,
        latest_guide_refresh_callback: LatestGuideRefreshCallback | None = None,
    ) -> None:
        super().__init__()
        self.add_item(
            ShiftTimelineEditAgainButton(
                shift_register_manager,
                day_number=day_number,
                event_date=event_date,
                submission_deadline_at=submission_deadline_at,
                draft_shift_proposal_at=draft_shift_proposal_at,
                final_shift_notice_at=final_shift_notice_at,
                latest_guide_enabled=latest_guide_enabled,
                latest_guide_toggle_callback=latest_guide_toggle_callback,
                latest_guide_state_resolver=latest_guide_state_resolver,
                latest_guide_refresh_callback=latest_guide_refresh_callback,
            )
        )


class ShiftRecruitmentRangeEditAgainButton(Button):
    """Button that reopens an invalid recruitment range submission."""

    def __init__(
        self,
        shift_register_manager: ShiftRegisterManager,
        *,
        recruitment_time_range: str,
        latest_guide_enabled: bool = False,
        latest_guide_toggle_callback: LatestGuideToggleCallback | None = None,
        latest_guide_state_resolver: LatestGuideStateResolver | None = None,
        latest_guide_refresh_callback: LatestGuideRefreshCallback | None = None,
    ) -> None:
        super().__init__(label="Edit Again", style=ButtonStyle.primary)
        self.shift_register_manager = shift_register_manager
        self.recruitment_time_range = recruitment_time_range
        self.latest_guide_enabled = latest_guide_enabled
        self.latest_guide_toggle_callback = latest_guide_toggle_callback
        self.latest_guide_state_resolver = latest_guide_state_resolver
        self.latest_guide_refresh_callback = latest_guide_refresh_callback

    async def callback(self, interaction: Interaction) -> None:
        if not await require_settings_permissions(interaction):
            return

        shift_register = await get_fresh_shift_register_config_or_respond(
            self.shift_register_manager,
            interaction,
        )
        if shift_register is None:
            return

        await interaction.response.send_modal(
            ShiftRecruitmentRangeModal(
                self.shift_register_manager,
                recruitment_time_range=self.recruitment_time_range,
                latest_guide_enabled=self.latest_guide_enabled,
                latest_guide_toggle_callback=self.latest_guide_toggle_callback,
                latest_guide_state_resolver=self.latest_guide_state_resolver,
                latest_guide_refresh_callback=self.latest_guide_refresh_callback,
            )
        )


class ShiftRecruitmentRangeEditAgainView(SettingsTimeoutView):
    """Retry view for invalid recruitment time range modal submissions."""

    def __init__(
        self,
        shift_register_manager: ShiftRegisterManager,
        *,
        recruitment_time_range: str,
        latest_guide_enabled: bool = False,
        latest_guide_toggle_callback: LatestGuideToggleCallback | None = None,
        latest_guide_state_resolver: LatestGuideStateResolver | None = None,
        latest_guide_refresh_callback: LatestGuideRefreshCallback | None = None,
    ) -> None:
        super().__init__()
        self.add_item(
            ShiftRecruitmentRangeEditAgainButton(
                shift_register_manager,
                recruitment_time_range=recruitment_time_range,
                latest_guide_enabled=latest_guide_enabled,
                latest_guide_toggle_callback=latest_guide_toggle_callback,
                latest_guide_state_resolver=latest_guide_state_resolver,
                latest_guide_refresh_callback=latest_guide_refresh_callback,
            )
        )


class ShiftRegisterView(SettingsTimeoutView):
    """View for shift register setup/edit button."""

    def __init__(
        self,
        shift_register_manager: ShiftRegisterManager,
        *,
        has_existing_settings: bool = False,
        sheet_url: str = "",
        entry_worksheet_title: str | None = None,
        draft_worksheet_title: str | None = None,
        final_schedule_worksheet_title: str | None = None,
        final_schedule_anchor_cell: str = "A1",
        selected_team_channel_id: int | None = None,
        latest_guide_enabled: bool = False,
        latest_guide_toggle_callback: LatestGuideToggleCallback | None = None,
        latest_guide_state_resolver: LatestGuideStateResolver | None = None,
        latest_guide_refresh_callback: LatestGuideRefreshCallback | None = None,
    ) -> None:
        super().__init__()
        self.shift_register_manager = shift_register_manager
        self.latest_guide_enabled = latest_guide_enabled
        self.latest_guide_toggle_callback = latest_guide_toggle_callback
        self.latest_guide_state_resolver = latest_guide_state_resolver
        self.latest_guide_refresh_callback = latest_guide_refresh_callback
        label = (
            "Edit Sheet Settings" if has_existing_settings else "Set Up Shift Register"
        )
        button = ShiftRegisterButton(
            label=label,
            shift_register_manager=shift_register_manager,
            sheet_url=sheet_url,
            entry_worksheet_title=entry_worksheet_title,
            draft_worksheet_title=draft_worksheet_title,
            final_schedule_worksheet_title=final_schedule_worksheet_title,
            final_schedule_anchor_cell=final_schedule_anchor_cell,
            requires_existing_settings=has_existing_settings,
            latest_guide_enabled=latest_guide_enabled,
            latest_guide_toggle_callback=latest_guide_toggle_callback,
            latest_guide_state_resolver=latest_guide_state_resolver,
            latest_guide_refresh_callback=latest_guide_refresh_callback,
        )
        if has_existing_settings and latest_guide_toggle_callback is not None:
            self.add_item(
                LatestGuideButton(
                    enabled=latest_guide_enabled,
                    toggle_callback=latest_guide_toggle_callback,
                )
            )
        self.add_item(button)
        if has_existing_settings:
            self.add_item(
                ManageTeamSourceButton(
                    shift_register_manager,
                    selected_channel_id=selected_team_channel_id,
                    latest_guide_enabled=latest_guide_enabled,
                    latest_guide_toggle_callback=latest_guide_toggle_callback,
                    latest_guide_state_resolver=latest_guide_state_resolver,
                    latest_guide_refresh_callback=latest_guide_refresh_callback,
                )
            )
            self.add_item(
                ShiftTimelineButton(
                    shift_register_manager,
                    latest_guide_enabled=latest_guide_enabled,
                    latest_guide_toggle_callback=latest_guide_toggle_callback,
                    latest_guide_state_resolver=latest_guide_state_resolver,
                    latest_guide_refresh_callback=latest_guide_refresh_callback,
                )
            )
            self.add_item(
                ShiftRecruitmentRangeButton(
                    shift_register_manager,
                    latest_guide_enabled=latest_guide_enabled,
                    latest_guide_toggle_callback=latest_guide_toggle_callback,
                    latest_guide_state_resolver=latest_guide_state_resolver,
                    latest_guide_refresh_callback=latest_guide_refresh_callback,
                )
            )


def build_current_settings_embed(
    sheet_url: str,
    metadata: ShiftRegisterGoogleSheetsMetadata,
    final_schedule_anchor_cell: str,
    shift_register: ShiftRegisterConfig,
    color: int,
    *,
    is_save_action: bool = False,
    latest_guide_enabled: bool = False,
    team_source: TeamSourceResolution,
) -> Embed:
    """
    Build an embed showing the current shift register settings.

    Args:
        sheet_url (str): The Google Sheet link.
        metadata (ShiftRegisterGoogleSheetsMetadata):
            The shift register metadata object.
        final_schedule_anchor_cell (str):
            The anchor cell for the final schedule worksheet.
        shift_register (ShiftRegisterConfig):
            The saved Shift Register configuration.
        color (int): Embed color.
        is_save_action (bool):
            If True, this is a settings save, otherwise a settings query (view).
        team_source (TeamSourceResolution):
            Resolved same-guild Team source status.

    Returns:
        Embed: The constructed embed.
    """
    embed = Embed(
        title=settings_title(
            SHIFT_REGISTER_DISPLAY_NAME,
            is_save_action=is_save_action,
        ),
        color=color,
    )
    embed.description = settings_description(
        SHIFT_REGISTER_DISPLAY_NAME,
        (
            SHIFT_REGISTER_SAVED_CONTROLS
            if is_save_action
            else SHIFT_REGISTER_CURRENT_CONTROLS
        ),
        is_save_action=is_save_action,
    )

    embed.add_field(
        name="Google Sheet", value=f"- **Link** = {sheet_url}", inline=False
    )

    worksheet_rows = [
        f"- **Entry** = `{metadata.entry_worksheets.title or '**Not Found**'}` : "
        f"`{metadata.entry_worksheets.id}`",
        f"- **Draft** = `{metadata.draft_worksheet.title or '**Not Found**'}` : "
        f"`{metadata.draft_worksheet.id}`",
        f"- **Final Schedule** = "
        f"`{metadata.final_schedule_worksheet.title or '**Not Found**'}` : "
        f"`{metadata.final_schedule_worksheet.id}`",
    ]
    embed.add_field(
        name="Worksheets & IDs",
        value="\n".join(worksheet_rows),
        inline=False,
    )
    embed.add_field(
        name="Team Source",
        value=_format_team_source(team_source),
        inline=False,
    )

    embed.add_field(
        name="Final Schedule Anchor Cell",
        value=f"- `{final_schedule_anchor_cell}`",
        inline=False,
    )
    embed.add_field(
        name=LATEST_GUIDE_FIELD_NAME,
        value=f"- {latest_guide_status_value(enabled=latest_guide_enabled)}",
        inline=False,
    )

    submission_deadline_at = shift_register.submission_deadline_at
    draft_shift_proposal_at = shift_register.draft_shift_proposal_at
    final_shift_notice_at = shift_register.final_shift_notice_at
    timeline_rows = [
        f"- **Day Number** = `{_format_optional_value(shift_register.day_number)}`",
        f"- **Event Date** = `{_format_settings_date(shift_register.event_date)}`",
        f"- **Submission Deadline** = "
        f"`{_format_settings_datetime(submission_deadline_at)}`",
        f"- **Draft Shift Proposal** = "
        f"`{_format_settings_datetime(draft_shift_proposal_at)}`",
        f"- **Final Shift Notice** = "
        f"`{_format_settings_datetime(final_shift_notice_at)}`",
    ]
    embed.add_field(
        name="Shift Timeline",
        value="\n".join(timeline_rows),
        inline=False,
    )

    recruitment_ranges = RecruitmentTimeRanges.from_json(
        shift_register.recruitment_time_ranges
    )
    embed.add_field(
        name="Recruitment Time Range",
        value=f"- `{recruitment_ranges.display()}`",
        inline=False,
    )

    return embed
