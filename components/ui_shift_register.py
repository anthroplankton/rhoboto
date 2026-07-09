from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from discord import ButtonStyle, Embed, Interaction, TextStyle
from discord.ui import Button, Modal, TextInput

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
    send_current_panel_followup,
    send_settings_partial_success,
    send_settings_refresh_failure,
    send_settings_storage_error,
    send_stale_setup_panel_if_configured,
    settings_description,
    settings_title,
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

if TYPE_CHECKING:
    from datetime import date, datetime

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


async def send_shift_settings_missing(interaction: Interaction) -> None:
    await interaction.response.send_message(
        SHIFT_REGISTER_SETTINGS_MISSING_MESSAGE,
        ephemeral=True,
    )


async def get_fresh_shift_register_config_or_respond(
    shift_register_manager: ShiftRegisterManager,
    interaction: Interaction,
) -> object | None:
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
    shift_register: object,
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
    final_schedule_anchor_cell = getattr(
        shift_register,
        "final_schedule_anchor_cell",
        "A1",
    )
    latest_guide_enabled = await resolve_latest_guide_enabled(
        enabled=latest_guide_enabled,
        state_resolver=latest_guide_state_resolver,
    )
    embed = build_current_settings_embed(
        sheet_url=shift_register.sheet_url,
        metadata=active_metadata,
        final_schedule_anchor_cell=final_schedule_anchor_cell,
        shift_register=shift_register,
        color=config.DEFAULT_EMBED_COLOR,
        is_save_action=is_save_action,
        latest_guide_enabled=latest_guide_enabled,
    )
    view = ShiftRegisterView(
        shift_register_manager=shift_register_manager,
        has_existing_settings=True,
        sheet_url=shift_register.sheet_url,
        entry_worksheet_title=active_metadata.entry_worksheets.title,
        draft_worksheet_title=active_metadata.draft_worksheet.title,
        final_schedule_worksheet_title=active_metadata.final_schedule_worksheet.title,
        final_schedule_anchor_cell=final_schedule_anchor_cell,
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


def _timeline_defaults_from_config(shift_register: object) -> dict[str, str]:
    day_number = getattr(shift_register, "day_number", None)
    return {
        "day_number": "" if day_number is None else str(day_number),
        "event_date": _format_modal_date(getattr(shift_register, "event_date", None)),
        "submission_deadline_at": _format_modal_datetime(
            getattr(shift_register, "submission_deadline_at", None)
        ),
        "draft_shift_proposal_at": _format_modal_datetime(
            getattr(shift_register, "draft_shift_proposal_at", None)
        ),
        "final_shift_notice_at": _format_modal_datetime(
            getattr(shift_register, "final_shift_notice_at", None)
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

        sheet_url = self.sheet_url.value
        entry_worksheet_title = self.entry_worksheet_title.value
        draft_worksheet_title = self.draft_worksheet_title.value
        final_schedule_worksheet_title = self.final_schedule_worksheet_title.value
        final_schedule_anchor_cell = self.final_schedule_anchor_cell.value

        try:
            metadata = (
                await self.shift_register_manager.upsert_sheet_config_and_worksheets(
                    sheet_url=sheet_url,
                    entry_worksheet_title=entry_worksheet_title,
                    draft_worksheet_title=draft_worksheet_title,
                    final_schedule_worksheet_title=final_schedule_worksheet_title,
                )
            )
        except SETTINGS_STORAGE_EXCEPTIONS as exc:
            await send_settings_partial_success(
                interaction,
                exc,
                operation="shift_register_setup",
                feature_name=SHIFT_REGISTER_FEATURE_NAME,
                log=logger,
            )
            return
        try:
            await self.shift_register_manager.update_final_schedule_anchor_cell(
                final_schedule_anchor_cell
            )
        except SETTINGS_STORAGE_EXCEPTIONS as exc:
            await send_settings_partial_success(
                interaction,
                exc,
                operation="shift_register_setup_anchor_save",
                feature_name=SHIFT_REGISTER_FEATURE_NAME,
                log=logger,
            )
            return

        try:
            shift_register = await self.shift_register_manager.get_sheet_config()
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
        except SETTINGS_STORAGE_EXCEPTIONS as exc:
            await send_settings_refresh_failure(
                interaction,
                exc,
                operation="shift_register_setup_refresh_panel",
                feature_name=SHIFT_REGISTER_FEATURE_NAME,
                log=logger,
            )
            return
        await send_current_panel_followup(interaction, panel)
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
            placeholder="YYYY-MM-DD or YYYY/MM/DD. Leave blank to clear.",
            default=event_date,
            required=False,
            style=TextStyle.short,
        )
        self.submission_deadline_at: TextInput = TextInput(
            label="Submission Deadline (JST)",
            placeholder="e.g. 2026-08-12 21 or 8/12 21. Leave blank to clear.",
            default=submission_deadline_at,
            required=False,
            style=TextStyle.short,
        )
        self.draft_shift_proposal_at: TextInput = TextInput(
            label="Draft Shift Proposal (JST)",
            placeholder="e.g. 2026-08-13 20 or 8/13 20. Leave blank to clear.",
            default=draft_shift_proposal_at,
            required=False,
            style=TextStyle.short,
        )
        self.final_shift_notice_at: TextInput = TextInput(
            label="Final Shift Notice (JST)",
            placeholder="e.g. 2026-08-14 18 or 8/14 18. Leave blank to clear.",
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
                existing_event_date=getattr(shift_register, "event_date", None),
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
            await self.shift_register_manager.update_recruitment_time_ranges(ranges)
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
            final_schedule_anchor_cell = getattr(
                shift_register,
                "final_schedule_anchor_cell",
                self.final_schedule_anchor_cell,
            )
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

        ranges = RecruitmentTimeRanges.from_json(
            getattr(shift_register, "recruitment_time_ranges", None)
        )
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
    shift_register: object,
    color: int,
    *,
    is_save_action: bool = False,
    latest_guide_enabled: bool = False,
) -> Embed:
    """
    Build an embed showing the current shift register settings.

    Args:
        sheet_url (str): The Google Sheet link.
        metadata (ShiftRegisterGoogleSheetsMetadata):
            The shift register metadata object.
        final_schedule_anchor_cell (str):
            The anchor cell for the final schedule worksheet.
        shift_register (object):
            The saved Shift Register configuration.
        color (int): Embed color.
        is_save_action (bool):
            If True, this is a settings save, otherwise a settings query (view).

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
        name="Final Schedule Anchor Cell",
        value=f"- `{final_schedule_anchor_cell}`",
        inline=False,
    )
    embed.add_field(
        name=LATEST_GUIDE_FIELD_NAME,
        value=f"- {latest_guide_status_value(enabled=latest_guide_enabled)}",
        inline=False,
    )

    submission_deadline_at = getattr(
        shift_register,
        "submission_deadline_at",
        None,
    )
    draft_shift_proposal_at = getattr(
        shift_register,
        "draft_shift_proposal_at",
        None,
    )
    final_shift_notice_at = getattr(
        shift_register,
        "final_shift_notice_at",
        None,
    )
    timeline_rows = [
        f"- **Day Number** = "
        f"`{_format_optional_value(getattr(shift_register, 'day_number', None))}`",
        f"- **Event Date** = "
        f"`{_format_settings_date(getattr(shift_register, 'event_date', None))}`",
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
        getattr(shift_register, "recruitment_time_ranges", None)
    )
    embed.add_field(
        name="Recruitment Time Range",
        value=f"- `{recruitment_ranges.display()}`",
        inline=False,
    )

    return embed
