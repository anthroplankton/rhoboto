from __future__ import annotations

import datetime as dt
import logging
from types import SimpleNamespace

import pytest
from discord import ButtonStyle
from tortoise.exceptions import DBConnectionError, IntegrityError

from components.ui_feature_channel import DisableAndClearConfirmView
from components.ui_language_settings import AnnouncementLanguageSettingsView
from components.ui_permissions import MISSING_SETTINGS_PERMISSION_MESSAGE
from components.ui_settings_flow import (
    SETTINGS_VIEW_TIMEOUT_SECONDS,
    attach_settings_view_message,
)
from components.ui_shift_register import (
    ShiftRecruitmentRangeModal,
    ShiftRegisterButton,
    ShiftRegisterSheetModal,
    ShiftRegisterView,
    ShiftTimelineModal,
)
from components.ui_team_register import (
    BackToTeamSettingsButton,
    EditEncoreRolesButton,
    EncoreRoleEditView,
    EncoreRolePreviewView,
    EncoreRoleSelect,
    TeamRegisterButton,
    TeamRegisterSheetModal,
    TeamRegisterView,
)
from tests.fakes import FakeInteraction, FakeRole
from utils.google_sheets_errors import GoogleSheetsError, GoogleSheetsErrorKind


class RecordingTeamRegisterManager:
    def __init__(self) -> None:
        self.upsert_calls: list[dict[str, object]] = []
        self.encore_role_updates: list[list[object]] = []
        self.encore_role_id_updates: list[list[int]] = []
        self.encore_role_ids: list[int] = []
        self.config_exists = True
        self.sheet_url = "https://sheet.example"
        self.metadata = team_register_metadata()
        self.upsert_error: Exception | None = None
        self.save_error: Exception | None = None
        self.fresh_config_error: Exception | None = None
        self.refresh_error: Exception | None = None
        self.metadata_error: GoogleSheetsError | None = None

    async def upsert_sheet_config_and_worksheets(
        self,
        *,
        sheet_url: str,
        team_worksheet_titles: list[str],
        summary_worksheet_title: str,
    ) -> SimpleNamespace:
        if self.upsert_error is not None:
            raise self.upsert_error
        self.config_exists = True
        self.upsert_calls.append(
            {
                "sheet_url": sheet_url,
                "team_worksheet_titles": team_worksheet_titles,
                "summary_worksheet_title": summary_worksheet_title,
            }
        )
        return SimpleNamespace(
            team_worksheets=[SimpleNamespace(title="Team 1", id=101)],
            summary_worksheet=SimpleNamespace(title="Summary", id=201),
        )

    async def get_sheet_config(self) -> SimpleNamespace:
        if self.fresh_config_error is not None:
            raise self.fresh_config_error
        if not self.config_exists:
            msg = "Sheet configuration not found."
            raise RuntimeError(msg)
        return SimpleNamespace(
            sheet_url=self.sheet_url,
            encore_role_ids=self.encore_role_ids,
        )

    async def get_fresh_sheet_config(self) -> SimpleNamespace | None:
        if self.fresh_config_error is not None:
            raise self.fresh_config_error
        if not self.config_exists:
            return None
        return await self.get_sheet_config()

    async def fetch_google_sheets_metadata(self) -> SimpleNamespace:
        if self.refresh_error is not None:
            raise self.refresh_error
        if self.metadata_error is not None:
            raise self.metadata_error
        return self.metadata

    async def update_encore_roles_record(self, roles: list[object]) -> None:
        self.encore_role_updates.append(roles)

    async def update_encore_role_ids_record(self, role_ids: list[int]) -> None:
        if self.save_error is not None:
            raise self.save_error
        self.encore_role_id_updates.append(role_ids)
        self.encore_role_ids = role_ids


class RecordingShiftRegisterManager:
    def __init__(self) -> None:
        self.upsert_calls: list[dict[str, object]] = []
        self.anchor_updates: list[str] = []
        self.timeline_updates: list[dict[str, object]] = []
        self.recruitment_range_updates: list[object] = []
        self.config_exists = True
        self.sheet_url = "https://sheet.example"
        self.final_schedule_anchor_cell = "B2"
        self.day_number = 2
        self.event_date = dt.date(2026, 8, 12)
        self.submission_deadline_at = dt.datetime(2026, 8, 12, 12, tzinfo=dt.UTC)
        self.draft_shift_proposal_at = dt.datetime(2026, 8, 13, 11, tzinfo=dt.UTC)
        self.final_shift_notice_at = dt.datetime(2026, 8, 14, 9, tzinfo=dt.UTC)
        self.recruitment_time_ranges = [{"start": 4, "end": 28}]
        self.metadata = SimpleNamespace(
            sheet_url="https://sheet.example",
            entry_worksheets=SimpleNamespace(title="Entry", id=101),
            draft_worksheet=SimpleNamespace(title="Draft", id=102),
            final_schedule_worksheet=SimpleNamespace(title="Final", id=103),
        )
        self.upsert_error: Exception | None = None
        self.save_error: Exception | None = None
        self.fresh_config_error: Exception | None = None
        self.refresh_error: Exception | None = None
        self.metadata_error: GoogleSheetsError | None = None

    async def upsert_sheet_config_and_worksheets(
        self,
        *,
        sheet_url: str,
        entry_worksheet_title: str,
        draft_worksheet_title: str,
        final_schedule_worksheet_title: str,
    ) -> SimpleNamespace:
        if self.upsert_error is not None:
            raise self.upsert_error
        self.upsert_calls.append(
            {
                "sheet_url": sheet_url,
                "entry_worksheet_title": entry_worksheet_title,
                "draft_worksheet_title": draft_worksheet_title,
                "final_schedule_worksheet_title": final_schedule_worksheet_title,
            }
        )
        return SimpleNamespace(
            entry_worksheets=SimpleNamespace(title="Entry", id=101),
            draft_worksheet=SimpleNamespace(title="Draft", id=102),
            final_schedule_worksheet=SimpleNamespace(title="Final", id=103),
        )

    async def update_final_schedule_anchor_cell(self, anchor_cell: str) -> None:
        if self.save_error is not None:
            raise self.save_error
        self.anchor_updates.append(anchor_cell)

    async def update_timeline(
        self,
        *,
        day_number: int | None,
        event_date: dt.date | None,
        submission_deadline_at: dt.datetime | None,
        draft_shift_proposal_at: dt.datetime | None,
        final_shift_notice_at: dt.datetime | None,
    ) -> None:
        if self.save_error is not None:
            raise self.save_error
        self.timeline_updates.append(
            {
                "day_number": day_number,
                "event_date": event_date,
                "submission_deadline_at": submission_deadline_at,
                "draft_shift_proposal_at": draft_shift_proposal_at,
                "final_shift_notice_at": final_shift_notice_at,
            }
        )
        self.day_number = day_number
        self.event_date = event_date
        self.submission_deadline_at = submission_deadline_at
        self.draft_shift_proposal_at = draft_shift_proposal_at
        self.final_shift_notice_at = final_shift_notice_at

    async def update_recruitment_time_ranges(self, ranges: object) -> None:
        if self.save_error is not None:
            raise self.save_error
        self.recruitment_range_updates.append(ranges)
        self.recruitment_time_ranges = ranges.to_json()

    async def get_sheet_config(self) -> SimpleNamespace:
        if self.fresh_config_error is not None:
            raise self.fresh_config_error
        if not self.config_exists:
            msg = "Sheet configuration not found."
            raise RuntimeError(msg)
        return SimpleNamespace(
            sheet_url=self.sheet_url,
            final_schedule_anchor_cell=(
                self.anchor_updates[-1]
                if self.anchor_updates
                else self.final_schedule_anchor_cell
            ),
            day_number=self.day_number,
            event_date=self.event_date,
            submission_deadline_at=self.submission_deadline_at,
            draft_shift_proposal_at=self.draft_shift_proposal_at,
            final_shift_notice_at=self.final_shift_notice_at,
            recruitment_time_ranges=self.recruitment_time_ranges,
        )

    async def get_fresh_sheet_config(self) -> SimpleNamespace | None:
        if self.fresh_config_error is not None:
            raise self.fresh_config_error
        if not self.config_exists:
            return None
        return await self.get_sheet_config()

    async def fetch_google_sheets_metadata(self) -> SimpleNamespace:
        if self.refresh_error is not None:
            raise self.refresh_error
        if self.metadata_error is not None:
            raise self.metadata_error
        return self.metadata


class FailingTeamRegisterManager(RecordingTeamRegisterManager):
    async def upsert_sheet_config_and_worksheets(
        self,
        **_: object,
    ) -> SimpleNamespace:
        raise GoogleSheetsError(
            GoogleSheetsErrorKind.PERMISSION,
            "Check the sheet sharing settings and service account access.",
        )


class FailingShiftRegisterManager(RecordingShiftRegisterManager):
    async def upsert_sheet_config_and_worksheets(
        self,
        **_: object,
    ) -> SimpleNamespace:
        raise GoogleSheetsError(
            GoogleSheetsErrorKind.INVALID_URL,
            "Check the Google Sheet link and save the settings again.",
        )


def unauthorized_interaction() -> FakeInteraction:
    return FakeInteraction(manage_channels=False)


def assert_permission_denied(interaction: FakeInteraction) -> None:
    assert interaction.response.messages == [
        (MISSING_SETTINGS_PERMISSION_MESSAGE, {"ephemeral": True})
    ]


def assert_no_private_storage_terms(content: str) -> None:
    assert "database" not in content.lower()
    assert "service account" not in content.lower()
    assert "credential" not in content.lower()


def assert_safe_settings_storage_message(content: str) -> None:
    assert "could not complete this action" in content
    assert "Reference: `STG-" in content
    assert_no_private_storage_terms(content)


class FakeMessage:
    def __init__(self) -> None:
        self.edits: list[tuple[tuple[object, ...], dict[str, object]]] = []

    async def edit(self, *args: object, **kwargs: object) -> None:
        self.edits.append((args, kwargs))


class FirstSendTimeoutFollowup:
    def __init__(self) -> None:
        self.calls = 0
        self.messages: list[tuple[str | None, dict[str, object]]] = []
        self.sent_message_objects: list[SimpleNamespace] = []

    async def send(
        self,
        content: str | None = None,
        **kwargs: object,
    ) -> SimpleNamespace:
        self.calls += 1
        if self.calls == 1:
            message = "discord delivery timeout"
            raise TimeoutError(message)
        self.messages.append((content, kwargs))
        message = SimpleNamespace()
        self.sent_message_objects.append(message)
        return message


def team_register_metadata() -> SimpleNamespace:
    return SimpleNamespace(
        sheet_url="https://sheet.example",
        team_worksheets=[SimpleNamespace(title="Team 1", id=101)],
        summary_worksheet=SimpleNamespace(title="Summary", id=201),
    )


def child_with_label(view: object, label: str) -> object:
    return next(
        child for child in view.children if getattr(child, "label", None) == label
    )


def test_team_register_setup_view_uses_set_up_button_label() -> None:
    manager = RecordingTeamRegisterManager()

    view = TeamRegisterView(manager, has_existing_settings=False)

    assert [child.label for child in view.children] == ["Set Up Team Register"]


def test_shift_register_setup_view_uses_set_up_button_label() -> None:
    manager = RecordingShiftRegisterManager()

    view = ShiftRegisterView(manager, has_existing_settings=False)

    assert [child.label for child in view.children] == ["Set Up Shift Register"]


@pytest.mark.asyncio
async def test_team_settings_button_denies_unauthorized_user() -> None:
    manager = RecordingTeamRegisterManager()
    interaction = unauthorized_interaction()
    button = TeamRegisterButton("Set Up Team Register", manager)

    await button.callback(interaction)

    assert_permission_denied(interaction)
    assert interaction.response.modals == []


@pytest.mark.asyncio
async def test_team_settings_button_allows_authorized_user() -> None:
    manager = RecordingTeamRegisterManager()
    manager.config_exists = False
    interaction = FakeInteraction()
    button = TeamRegisterButton("Set Up Team Register", manager)

    await button.callback(interaction)

    assert len(interaction.response.modals) == 1
    assert isinstance(interaction.response.modals[0], TeamRegisterSheetModal)
    assert interaction.response.messages == []


@pytest.mark.asyncio
async def test_team_setup_button_with_existing_config_sends_current_panel() -> None:
    manager = RecordingTeamRegisterManager()
    interaction = FakeInteraction()
    button = TeamRegisterButton("Set Up Team Register", manager)

    await button.callback(interaction)

    assert interaction.response.deferred == [True]
    assert interaction.response.modals == []
    assert len(interaction.followup.messages) == 1
    content, kwargs = interaction.followup.messages[0]
    assert content == (
        "Team Register is already configured for this channel. "
        "Here are the current settings."
    )
    assert kwargs["ephemeral"] is True
    assert kwargs["embed"].title == "Team Register Settings"
    assert kwargs["embed"].description == (
        "Team Register is configured for this channel. "
        "Use the buttons below to update sheet settings or Encore roles."
    )
    assert kwargs["wait"] is True
    assert kwargs["view"].message is interaction.followup.sent_message_objects[0]


@pytest.mark.asyncio
async def test_team_setup_button_defer_timeout_is_not_storage_error() -> None:
    async def raise_timeout(**_: object) -> None:
        message = "discord delivery timeout"
        raise TimeoutError(message)

    manager = RecordingTeamRegisterManager()
    interaction = FakeInteraction()
    interaction.response.defer = raise_timeout
    button = TeamRegisterButton("Set Up Team Register", manager)

    with pytest.raises(TimeoutError, match="discord delivery timeout"):
        await button.callback(interaction)

    assert interaction.followup.messages == []


@pytest.mark.asyncio
async def test_team_setup_button_existing_config_storage_error_sends_safe_message() -> (
    None
):
    manager = RecordingTeamRegisterManager()
    manager.fresh_config_error = DBConnectionError("private database host")
    interaction = FakeInteraction()
    button = TeamRegisterButton("Set Up Team Register", manager)

    await button.callback(interaction)

    assert interaction.response.modals == []
    assert interaction.followup.messages == []
    assert len(interaction.response.messages) == 1
    content, kwargs = interaction.response.messages[0]
    assert content is not None
    assert_safe_settings_storage_message(content)
    assert kwargs == {"ephemeral": True}


@pytest.mark.asyncio
async def test_team_edit_settings_button_uses_fresh_missing_settings_guard() -> None:
    manager = RecordingTeamRegisterManager()
    manager.config_exists = False
    interaction = FakeInteraction()
    view = TeamRegisterView(
        manager,
        has_existing_settings=True,
        metadata=team_register_metadata(),
    )

    await child_with_label(view, "Edit Team Register Settings").callback(interaction)

    assert interaction.response.messages == [
        (
            "Team Register settings are no longer configured for this channel.",
            {"ephemeral": True},
        )
    ]
    assert interaction.response.modals == []


@pytest.mark.asyncio
async def test_team_edit_settings_button_storage_error_sends_safe_message() -> None:
    manager = RecordingTeamRegisterManager()
    manager.fresh_config_error = DBConnectionError("private database host")
    interaction = FakeInteraction()
    view = TeamRegisterView(
        manager,
        has_existing_settings=True,
        metadata=team_register_metadata(),
    )

    await child_with_label(view, "Edit Team Register Settings").callback(interaction)

    assert interaction.response.modals == []
    assert interaction.response.edits == []
    assert len(interaction.response.messages) == 1
    content, kwargs = interaction.response.messages[0]
    assert content is not None
    assert_safe_settings_storage_message(content)
    assert kwargs == {"ephemeral": True}


def test_team_existing_settings_view_buttons_use_secondary_style() -> None:
    manager = RecordingTeamRegisterManager()
    view = TeamRegisterView(
        manager,
        has_existing_settings=True,
        metadata=team_register_metadata(),
        encore_role_ids=[],
        roles=[],
    )

    edit_settings = child_with_label(view, "Edit Team Register Settings")
    edit_encore_roles = child_with_label(view, "Edit Encore Roles")

    assert edit_settings.style is ButtonStyle.secondary
    assert edit_encore_roles.style is ButtonStyle.secondary


@pytest.mark.asyncio
async def test_team_settings_view_disables_controls_on_timeout() -> None:
    manager = RecordingTeamRegisterManager()
    view = TeamRegisterView(
        manager,
        has_existing_settings=True,
        metadata=team_register_metadata(),
        encore_role_ids=[],
        roles=[],
    )
    message = FakeMessage()
    attach_settings_view_message(view, message)

    await view.on_timeout()

    assert view.timeout == SETTINGS_VIEW_TIMEOUT_SECONDS
    assert all(child.disabled for child in view.children)
    assert message.edits == [((), {"view": view})]


def test_team_saved_view_without_active_encore_roles_highlights_encore_button() -> None:
    manager = RecordingTeamRegisterManager()
    view = TeamRegisterView(
        manager,
        has_existing_settings=True,
        metadata=team_register_metadata(),
        encore_role_ids=[],
        roles=[],
        is_save_action=True,
    )

    edit_settings = child_with_label(view, "Edit Team Register Settings")
    edit_encore_roles = child_with_label(view, "Edit Encore Roles")

    assert edit_settings.style is ButtonStyle.secondary
    assert edit_encore_roles.style is ButtonStyle.primary


@pytest.mark.asyncio
async def test_team_existing_edit_button_uses_local_modal_defaults() -> None:
    manager = RecordingTeamRegisterManager()
    manager.sheet_url = "https://fresh.sheet.example"
    manager.metadata_error = GoogleSheetsError(
        GoogleSheetsErrorKind.PERMISSION,
        "Check the sheet sharing settings and service account access.",
    )
    interaction = FakeInteraction()
    view = TeamRegisterView(
        manager,
        has_existing_settings=True,
        sheet_url="https://stale.sheet.example",
        team_worksheet_titles=["Stale Team"],
        summary_worksheet_title="Stale Summary",
        metadata=team_register_metadata(),
    )

    await child_with_label(view, "Edit Team Register Settings").callback(interaction)

    assert interaction.response.messages == []
    assert len(interaction.response.modals) == 1
    modal = interaction.response.modals[0]
    assert isinstance(modal, TeamRegisterSheetModal)
    assert modal.sheet_url.default == "https://fresh.sheet.example"
    assert modal.worksheet_titles.default == "Stale Team"
    assert modal.summary_worksheet_title.default == "Stale Summary"


@pytest.mark.asyncio
async def test_team_modal_submit_denies_unauthorized_user() -> None:
    manager = RecordingTeamRegisterManager()
    interaction = unauthorized_interaction()
    modal = TeamRegisterSheetModal(manager, sheet_url="https://sheet.example")

    await modal.on_submit(interaction)

    assert_permission_denied(interaction)
    assert interaction.response.deferred == []
    assert manager.upsert_calls == []


@pytest.mark.asyncio
async def test_team_modal_submit_allows_authorized_user() -> None:
    manager = RecordingTeamRegisterManager()
    interaction = FakeInteraction()
    modal = TeamRegisterSheetModal(
        manager,
        sheet_url="https://sheet.example",
        team_worksheet_titles=["Team 1"],
        summary_worksheet_title="Summary",
    )

    await modal.on_submit(interaction)

    assert interaction.response.deferred == [True]
    assert len(manager.upsert_calls) == 1
    assert len(interaction.followup.messages) == 1
    _, kwargs = interaction.followup.messages[0]
    embed = kwargs["embed"]
    assert embed.title == "Team Register Settings Saved"
    assert embed.description == (
        "Your Team Register settings were saved. "
        "Use the buttons below to edit sheet settings or Encore roles."
    )
    assert kwargs["wait"] is True
    assert kwargs["view"].message is interaction.followup.sent_message_objects[0]


@pytest.mark.asyncio
async def test_team_setup_modal_submit_can_create_missing_settings() -> None:
    manager = RecordingTeamRegisterManager()
    manager.config_exists = False
    interaction = FakeInteraction()
    modal = TeamRegisterSheetModal(
        manager,
        sheet_url="https://sheet.example",
        team_worksheet_titles=["Team 1"],
        summary_worksheet_title="Summary",
    )

    await modal.on_submit(interaction)

    assert interaction.response.deferred == [True]
    assert len(manager.upsert_calls) == 1
    assert len(interaction.followup.messages) == 1


@pytest.mark.asyncio
async def test_team_setup_modal_panel_delivery_timeout_is_not_storage_error() -> None:
    manager = RecordingTeamRegisterManager()
    interaction = FakeInteraction()
    interaction.followup = FirstSendTimeoutFollowup()
    modal = TeamRegisterSheetModal(
        manager,
        sheet_url="https://sheet.example",
        team_worksheet_titles=["Team 1"],
        summary_worksheet_title="Summary",
    )

    with pytest.raises(TimeoutError, match="discord delivery timeout"):
        await modal.on_submit(interaction)

    assert interaction.response.deferred == [True]
    assert len(manager.upsert_calls) == 1
    assert interaction.followup.messages == []


@pytest.mark.asyncio
async def test_team_edit_modal_submit_uses_fresh_missing_settings_guard() -> None:
    manager = RecordingTeamRegisterManager()
    manager.config_exists = False
    interaction = FakeInteraction()
    modal = TeamRegisterSheetModal(
        manager,
        sheet_url="https://sheet.example",
        team_worksheet_titles=["Team 1"],
        summary_worksheet_title="Summary",
        requires_existing_settings=True,
    )

    await modal.on_submit(interaction)

    assert interaction.response.messages == [
        (
            "Team Register settings are no longer configured for this channel.",
            {"ephemeral": True},
        )
    ]
    assert interaction.response.deferred == []
    assert manager.upsert_calls == []


@pytest.mark.asyncio
async def test_team_setup_modal_reports_partial_success_for_sheet_save_error() -> None:
    manager = FailingTeamRegisterManager()
    interaction = FakeInteraction()
    modal = TeamRegisterSheetModal(
        manager,
        sheet_url="https://private.sheet.example",
        team_worksheet_titles=["Team 1"],
        summary_worksheet_title="Summary",
    )

    await modal.on_submit(interaction)

    assert interaction.response.deferred == [True]
    assert len(interaction.followup.messages) == 1
    content, kwargs = interaction.followup.messages[0]
    assert content is not None
    assert "Some changes may have been saved" in content
    assert "Reference: `STG-" in content
    assert_no_private_storage_terms(content)
    assert kwargs == {"ephemeral": True}
    assert "private.sheet.example" not in str(interaction.followup.messages)


@pytest.mark.asyncio
async def test_team_setup_modal_reports_partial_success_when_initial_save_fails() -> (
    None
):
    manager = RecordingTeamRegisterManager()
    manager.upsert_error = DBConnectionError("private database host")
    interaction = FakeInteraction()
    modal = TeamRegisterSheetModal(
        manager,
        sheet_url="https://sheet.example",
        team_worksheet_titles=["Team 1"],
        summary_worksheet_title="Summary",
    )

    await modal.on_submit(interaction)

    assert interaction.response.deferred == [True]
    assert len(interaction.followup.messages) == 1
    content, kwargs = interaction.followup.messages[0]
    assert content is not None
    assert "Some changes may have been saved" in content
    assert "Reference: `STG-" in content
    assert_no_private_storage_terms(content)
    assert kwargs == {"ephemeral": True}


@pytest.mark.asyncio
async def test_team_setup_modal_reports_partial_success_when_config_refresh_fails() -> (
    None
):
    manager = RecordingTeamRegisterManager()
    manager.fresh_config_error = DBConnectionError("private database host")
    interaction = FakeInteraction()
    modal = TeamRegisterSheetModal(
        manager,
        sheet_url="https://sheet.example",
        team_worksheet_titles=["Team 1"],
        summary_worksheet_title="Summary",
    )

    await modal.on_submit(interaction)

    assert interaction.response.deferred == [True]
    assert len(manager.upsert_calls) == 1
    assert len(interaction.followup.messages) == 1
    content, kwargs = interaction.followup.messages[0]
    assert content is not None
    assert "Some changes may have been saved" in content
    assert "Reference: `STG-" in content
    assert_no_private_storage_terms(content)
    assert kwargs == {"ephemeral": True}


@pytest.mark.asyncio
async def test_edit_encore_roles_button_denies_unauthorized_user() -> None:
    manager = RecordingTeamRegisterManager()
    interaction = unauthorized_interaction()
    button = EditEncoreRolesButton(manager, metadata=team_register_metadata())

    await button.callback(interaction)

    assert_permission_denied(interaction)
    assert interaction.response.edits == []


@pytest.mark.asyncio
async def test_edit_encore_roles_button_shows_role_edit_view() -> None:
    manager = RecordingTeamRegisterManager()
    manager.encore_role_ids = [1]
    role = FakeRole(id=1, name="Encore", position=10)
    interaction = FakeInteraction(roles=[role])
    button = EditEncoreRolesButton(manager, metadata=team_register_metadata())

    await button.callback(interaction)

    assert len(interaction.response.edits) == 1
    content, edit_kwargs = interaction.response.edits[0]
    assert content is None
    assert isinstance(edit_kwargs["view"], EncoreRoleEditView)


@pytest.mark.asyncio
async def test_edit_encore_roles_transfers_message_to_role_edit_view() -> None:
    manager = RecordingTeamRegisterManager()
    manager.encore_role_ids = [1]
    role = FakeRole(id=1, name="Encore", position=10)
    interaction = FakeInteraction(roles=[role])
    message = FakeMessage()
    view = TeamRegisterView(
        manager,
        has_existing_settings=True,
        metadata=team_register_metadata(),
        roles=[role],
        encore_role_ids=[1],
    )
    attach_settings_view_message(view, message)

    await child_with_label(view, "Edit Encore Roles").callback(interaction)

    updated_view = interaction.response.edits[0][1]["view"]
    assert view.is_finished()
    assert updated_view.message is message


@pytest.mark.asyncio
async def test_edit_encore_roles_button_rejects_more_than_25_active_roles() -> None:
    manager = RecordingTeamRegisterManager()
    roles = [FakeRole(id=i, name=f"Role {i}", position=i) for i in range(1, 27)]
    manager.encore_role_ids = [role.id for role in roles]
    interaction = FakeInteraction(roles=roles)
    button = EditEncoreRolesButton(manager, metadata=team_register_metadata())

    await button.callback(interaction)

    content, edit_kwargs = interaction.response.edits[0]
    assert content is None
    assert edit_kwargs["embed"].title == "Cannot Edit Encore Roles"
    assert isinstance(edit_kwargs["view"], TeamRegisterView)


@pytest.mark.asyncio
async def test_edit_encore_roles_too_many_transfers_message_to_settings() -> None:
    manager = RecordingTeamRegisterManager()
    roles = [FakeRole(id=i, name=f"Role {i}", position=i) for i in range(1, 27)]
    manager.encore_role_ids = [role.id for role in roles]
    interaction = FakeInteraction(roles=roles)
    message = FakeMessage()
    view = TeamRegisterView(
        manager,
        has_existing_settings=True,
        metadata=team_register_metadata(),
        roles=roles,
        encore_role_ids=manager.encore_role_ids,
    )
    attach_settings_view_message(view, message)

    await child_with_label(view, "Edit Encore Roles").callback(interaction)

    updated_view = interaction.response.edits[0][1]["view"]
    assert view.is_finished()
    assert updated_view.message is message


@pytest.mark.asyncio
async def test_edit_encore_roles_button_uses_fresh_missing_settings_guard() -> None:
    manager = RecordingTeamRegisterManager()
    manager.config_exists = False
    interaction = FakeInteraction()
    button = EditEncoreRolesButton(manager, metadata=team_register_metadata())

    await button.callback(interaction)

    assert interaction.response.messages == [
        (
            "Team Register settings are no longer configured for this channel.",
            {"ephemeral": True},
        )
    ]
    assert interaction.response.edits == []


@pytest.mark.asyncio
async def test_encore_role_select_creates_preview_without_saving() -> None:
    manager = RecordingTeamRegisterManager()
    role = FakeRole(id=1, name="Encore", position=10)
    interaction = FakeInteraction(roles=[role])
    view = EncoreRoleEditView(
        manager,
        metadata=team_register_metadata(),
        roles=[role],
        encore_role_ids=[],
        retained_missing_role_ids=[99],
    )
    select = next(
        child for child in view.children if isinstance(child, EncoreRoleSelect)
    )
    select._values = [role]  # noqa: SLF001

    await select.callback(interaction)

    assert manager.encore_role_updates == []
    assert manager.encore_role_id_updates == []
    assert len(interaction.response.edits) == 1
    content, edit_kwargs = interaction.response.edits[0]
    assert content is None
    assert isinstance(edit_kwargs["view"], EncoreRolePreviewView)


@pytest.mark.asyncio
async def test_encore_role_select_transfers_message_to_preview() -> None:
    manager = RecordingTeamRegisterManager()
    role = FakeRole(id=1, name="Encore", position=10)
    interaction = FakeInteraction(roles=[role])
    message = FakeMessage()
    view = EncoreRoleEditView(
        manager,
        metadata=team_register_metadata(),
        roles=[role],
        encore_role_ids=[],
        retained_missing_role_ids=[99],
    )
    attach_settings_view_message(view, message)
    select = next(
        child for child in view.children if isinstance(child, EncoreRoleSelect)
    )
    select._values = [role]  # noqa: SLF001

    await select.callback(interaction)

    updated_view = interaction.response.edits[0][1]["view"]
    assert view.is_finished()
    assert updated_view.message is message


@pytest.mark.asyncio
async def test_encore_role_select_uses_fresh_missing_settings_guard() -> None:
    manager = RecordingTeamRegisterManager()
    manager.config_exists = False
    role = FakeRole(id=1, name="Encore", position=10)
    interaction = FakeInteraction(roles=[role])
    view = EncoreRoleEditView(
        manager,
        metadata=team_register_metadata(),
        roles=[role],
        encore_role_ids=[],
        retained_missing_role_ids=[99],
    )
    select = next(
        child for child in view.children if isinstance(child, EncoreRoleSelect)
    )
    select._values = [role]  # noqa: SLF001

    await select.callback(interaction)

    assert interaction.response.messages == [
        (
            "Team Register settings are no longer configured for this channel.",
            {"ephemeral": True},
        )
    ]
    assert manager.encore_role_id_updates == []
    assert interaction.response.edits == []


@pytest.mark.asyncio
async def test_encore_role_select_denies_unauthorized_user() -> None:
    manager = RecordingTeamRegisterManager()
    role = FakeRole(id=1, name="Encore", position=10)
    interaction = unauthorized_interaction()
    select = EncoreRoleSelect(
        manager,
        roles=[role],
        encore_role_ids=[],
        retained_missing_role_ids=[99],
        metadata=team_register_metadata(),
    )
    select._values = [role]  # noqa: SLF001

    await select.callback(interaction)

    assert_permission_denied(interaction)
    assert manager.encore_role_id_updates == []
    assert interaction.response.edits == []


@pytest.mark.asyncio
async def test_encore_role_confirm_saves_selected_and_retained_missing_ids() -> None:
    manager = RecordingTeamRegisterManager()
    role = FakeRole(id=1, name="Encore", position=10)
    interaction = FakeInteraction(roles=[role])
    view = EncoreRolePreviewView(
        manager,
        selected_roles=[role],
        retained_missing_role_ids=[99],
        metadata=team_register_metadata(),
    )

    await child_with_label(view, "Confirm Save").callback(interaction)

    assert manager.encore_role_id_updates == [[1, 99]]
    assert len(interaction.response.edits) == 1


@pytest.mark.asyncio
async def test_encore_role_edit_timeout_disables_controls_without_saving() -> None:
    manager = RecordingTeamRegisterManager()
    role = FakeRole(id=1, name="Encore", position=10)
    view = EncoreRoleEditView(
        manager,
        metadata=team_register_metadata(),
        roles=[role],
        encore_role_ids=[1, 99],
        retained_missing_role_ids=[99],
    )
    message = FakeMessage()
    attach_settings_view_message(view, message)

    await view.on_timeout()

    assert manager.encore_role_id_updates == []
    assert all(child.disabled for child in view.children)
    assert (
        "Unsaved Encore role changes were not saved." in message.edits[0][1]["content"]
    )
    assert message.edits[0][1]["view"] is view


@pytest.mark.asyncio
async def test_encore_role_preview_timeout_disables_controls_without_saving() -> None:
    manager = RecordingTeamRegisterManager()
    role = FakeRole(id=1, name="Encore", position=10)
    view = EncoreRolePreviewView(
        manager,
        selected_roles=[role],
        retained_missing_role_ids=[99],
        metadata=team_register_metadata(),
    )
    message = FakeMessage()
    attach_settings_view_message(view, message)

    await view.on_timeout()

    assert manager.encore_role_id_updates == []
    assert all(child.disabled for child in view.children)
    assert (
        "Unsaved Encore role changes were not saved." in message.edits[0][1]["content"]
    )
    assert message.edits[0][1]["view"] is view


@pytest.mark.asyncio
async def test_encore_role_confirm_transfers_message_to_settings() -> None:
    manager = RecordingTeamRegisterManager()
    role = FakeRole(id=1, name="Encore", position=10)
    interaction = FakeInteraction(roles=[role])
    message = FakeMessage()
    view = EncoreRolePreviewView(
        manager,
        selected_roles=[role],
        retained_missing_role_ids=[],
        metadata=team_register_metadata(),
    )
    attach_settings_view_message(view, message)

    await child_with_label(view, "Confirm Save").callback(interaction)

    updated_view = interaction.response.edits[0][1]["view"]
    assert view.is_finished()
    assert updated_view.message is message


@pytest.mark.asyncio
async def test_encore_role_confirm_refreshes_metadata_after_save() -> None:
    manager = RecordingTeamRegisterManager()
    role = FakeRole(id=1, name="Encore", position=10)
    interaction = FakeInteraction(roles=[role])
    refreshed_metadata = team_register_metadata()
    refreshed_metadata.team_worksheets[0].title = "Fresh Team"
    manager.metadata = refreshed_metadata
    view = EncoreRolePreviewView(
        manager,
        selected_roles=[role],
        retained_missing_role_ids=[],
        metadata=team_register_metadata(),
    )

    await child_with_label(view, "Confirm Save").callback(interaction)

    content, edit_kwargs = interaction.response.edits[0]
    assert content is None
    embed = edit_kwargs["embed"]
    worksheets_field = next(
        field for field in embed.fields if field.name == "Worksheets & IDs"
    )
    assert "Fresh Team" in worksheets_field.value


@pytest.mark.asyncio
async def test_encore_role_confirm_reports_saved_when_metadata_refresh_fails() -> None:
    manager = RecordingTeamRegisterManager()
    role = FakeRole(id=1, name="Encore", position=10)
    interaction = FakeInteraction(roles=[role])
    manager.refresh_error = GoogleSheetsError(
        GoogleSheetsErrorKind.PERMISSION,
        "Check the sheet sharing settings and service account access.",
    )
    view = EncoreRolePreviewView(
        manager,
        selected_roles=[role],
        retained_missing_role_ids=[],
        metadata=team_register_metadata(),
    )

    await child_with_label(view, "Confirm Save").callback(interaction)

    assert manager.encore_role_id_updates == [[1]]
    assert view.is_finished()
    assert len(interaction.response.edits) == 1
    content, edit_kwargs = interaction.response.edits[0]
    assert content is not None
    assert "Some changes may have been saved" in content
    assert "settings view could not be refreshed" in content
    assert "Reference: `STG-" in content
    assert_no_private_storage_terms(content)
    assert edit_kwargs == {"embed": None, "view": None}


@pytest.mark.asyncio
async def test_encore_role_confirm_refresh_failure_logs_storage_fields_safely(
    caplog: pytest.LogCaptureFixture,
) -> None:
    manager = RecordingTeamRegisterManager()
    role = FakeRole(id=1, name="Encore", position=10)
    interaction = FakeInteraction(roles=[role])
    manager.refresh_error = DBConnectionError("private database host")
    caplog.set_level(logging.WARNING, logger="components.ui_team_register")
    view = EncoreRolePreviewView(
        manager,
        selected_roles=[role],
        retained_missing_role_ids=[],
        metadata=team_register_metadata(),
    )

    await child_with_label(view, "Confirm Save").callback(interaction)

    content, _edit_kwargs = interaction.response.edits[0]
    assert content is not None
    assert content.count("Some changes may have been saved") == 1
    assert "settings view could not be refreshed" in content
    assert "Reference: `STG-" in content
    assert "partial_success" in caplog.text
    assert "private database host" not in caplog.text


@pytest.mark.asyncio
async def test_encore_role_confirm_reports_storage_error_when_save_fails() -> None:
    manager = RecordingTeamRegisterManager()
    manager.save_error = DBConnectionError("private database host")
    role = FakeRole(id=1, name="Encore", position=10)
    interaction = FakeInteraction(roles=[role])
    view = EncoreRolePreviewView(
        manager,
        selected_roles=[role],
        retained_missing_role_ids=[],
        metadata=team_register_metadata(),
    )

    await child_with_label(view, "Confirm Save").callback(interaction)

    assert manager.encore_role_id_updates == []
    assert len(interaction.response.messages) == 1
    content, kwargs = interaction.response.messages[0]
    assert content is not None
    assert "could not complete this action" in content
    assert "Reference: `STG-" in content
    assert "Some changes may have been saved" not in content
    assert_no_private_storage_terms(content)
    assert kwargs == {"ephemeral": True}


@pytest.mark.asyncio
async def test_encore_role_confirm_denies_unauthorized_user() -> None:
    manager = RecordingTeamRegisterManager()
    role = FakeRole(id=1, name="Encore", position=10)
    interaction = unauthorized_interaction()
    view = EncoreRolePreviewView(
        manager,
        selected_roles=[role],
        retained_missing_role_ids=[99],
        metadata=team_register_metadata(),
    )

    await child_with_label(view, "Confirm Save").callback(interaction)

    assert_permission_denied(interaction)
    assert manager.encore_role_id_updates == []


@pytest.mark.asyncio
async def test_encore_role_confirm_uses_fresh_missing_settings_guard() -> None:
    manager = RecordingTeamRegisterManager()
    manager.config_exists = False
    role = FakeRole(id=1, name="Encore", position=10)
    interaction = FakeInteraction(roles=[role])
    view = EncoreRolePreviewView(
        manager,
        selected_roles=[role],
        retained_missing_role_ids=[],
        metadata=team_register_metadata(),
    )

    await child_with_label(view, "Confirm Save").callback(interaction)

    assert manager.encore_role_id_updates == []
    assert interaction.response.messages == [
        (
            "Team Register settings are no longer configured for this channel.",
            {"ephemeral": True},
        )
    ]


@pytest.mark.asyncio
async def test_encore_role_cancel_returns_to_settings_without_saving() -> None:
    manager = RecordingTeamRegisterManager()
    role = FakeRole(id=1, name="Encore", position=10)
    interaction = FakeInteraction(roles=[role])
    view = EncoreRolePreviewView(
        manager,
        selected_roles=[role],
        retained_missing_role_ids=[99],
        metadata=team_register_metadata(),
    )

    await child_with_label(view, "Cancel").callback(interaction)

    assert manager.encore_role_id_updates == []
    content, edit_kwargs = interaction.response.edits[0]
    assert content is None
    assert edit_kwargs["embed"].title == "Team Register Settings"
    assert isinstance(edit_kwargs["view"], TeamRegisterView)


@pytest.mark.asyncio
async def test_encore_role_cancel_transfers_message_to_settings() -> None:
    manager = RecordingTeamRegisterManager()
    role = FakeRole(id=1, name="Encore", position=10)
    interaction = FakeInteraction(roles=[role])
    message = FakeMessage()
    view = EncoreRolePreviewView(
        manager,
        selected_roles=[role],
        retained_missing_role_ids=[99],
        metadata=team_register_metadata(),
    )
    attach_settings_view_message(view, message)

    await child_with_label(view, "Cancel").callback(interaction)

    updated_view = interaction.response.edits[0][1]["view"]
    assert view.is_finished()
    assert updated_view.message is message


@pytest.mark.asyncio
async def test_encore_role_cancel_denies_unauthorized_user() -> None:
    manager = RecordingTeamRegisterManager()
    role = FakeRole(id=1, name="Encore", position=10)
    interaction = unauthorized_interaction()
    view = EncoreRolePreviewView(
        manager,
        selected_roles=[role],
        retained_missing_role_ids=[99],
        metadata=team_register_metadata(),
    )

    await child_with_label(view, "Cancel").callback(interaction)

    assert_permission_denied(interaction)
    assert manager.encore_role_id_updates == []
    assert not all(child.disabled for child in view.children)


@pytest.mark.asyncio
async def test_remove_missing_ids_from_edit_view_previews_removal_without_saving() -> (
    None
):
    manager = RecordingTeamRegisterManager()
    role = FakeRole(id=1, name="Encore", position=10)
    interaction = FakeInteraction(roles=[role])
    view = EncoreRoleEditView(
        manager,
        metadata=team_register_metadata(),
        roles=[role],
        encore_role_ids=[1, 99],
        retained_missing_role_ids=[99],
    )

    await child_with_label(view, "Remove Missing IDs").callback(interaction)

    assert manager.encore_role_id_updates == []
    content, edit_kwargs = interaction.response.edits[0]
    assert content is None
    updated_view = edit_kwargs["view"]
    assert isinstance(updated_view, EncoreRolePreviewView)
    assert updated_view.selected_roles == (role,)
    assert updated_view.retained_missing_role_ids == ()
    assert updated_view.removed_missing_role_ids == (99,)
    assert child_with_label(updated_view, "Confirm Save") is not None
    assert child_with_label(updated_view, "Cancel") is not None
    assert all(
        getattr(child, "label", None) != "Remove Missing IDs"
        for child in updated_view.children
    )


@pytest.mark.asyncio
async def test_remove_missing_stops_old_view_and_transfers_message_to_preview() -> None:
    manager = RecordingTeamRegisterManager()
    role = FakeRole(id=1, name="Encore", position=10)
    interaction = FakeInteraction(roles=[role])
    message = FakeMessage()
    view = EncoreRoleEditView(
        manager,
        metadata=team_register_metadata(),
        roles=[role],
        encore_role_ids=[1, 99],
        retained_missing_role_ids=[99],
    )
    attach_settings_view_message(view, message)

    await child_with_label(view, "Remove Missing IDs").callback(interaction)

    updated_view = interaction.response.edits[0][1]["view"]
    assert view.is_finished()
    assert updated_view.message is message


@pytest.mark.asyncio
async def test_remove_missing_denies_unauthorized_user() -> None:
    manager = RecordingTeamRegisterManager()
    role = FakeRole(id=1, name="Encore", position=10)
    interaction = unauthorized_interaction()
    view = EncoreRoleEditView(
        manager,
        metadata=team_register_metadata(),
        roles=[role],
        encore_role_ids=[1, 99],
        retained_missing_role_ids=[99],
    )

    await child_with_label(view, "Remove Missing IDs").callback(interaction)

    assert_permission_denied(interaction)
    assert manager.encore_role_id_updates == []
    assert interaction.response.edits == []


@pytest.mark.asyncio
async def test_remove_missing_uses_fresh_missing_settings_guard() -> None:
    manager = RecordingTeamRegisterManager()
    manager.config_exists = False
    role = FakeRole(id=1, name="Encore", position=10)
    interaction = FakeInteraction(roles=[role])
    view = EncoreRoleEditView(
        manager,
        metadata=team_register_metadata(),
        roles=[role],
        encore_role_ids=[1, 99],
        retained_missing_role_ids=[99],
    )

    await child_with_label(view, "Remove Missing IDs").callback(interaction)

    assert interaction.response.messages == [
        (
            "Team Register settings are no longer configured for this channel.",
            {"ephemeral": True},
        )
    ]
    assert manager.encore_role_id_updates == []
    assert interaction.response.edits == []


@pytest.mark.asyncio
async def test_missing_only_edit_view_can_preview_missing_cleanup() -> None:
    manager = RecordingTeamRegisterManager()
    interaction = FakeInteraction(roles=[])
    view = EncoreRoleEditView(
        manager,
        metadata=team_register_metadata(),
        roles=[],
        encore_role_ids=[99],
        retained_missing_role_ids=[99],
    )
    remove_button = child_with_label(view, "Remove Missing IDs")

    await remove_button.callback(interaction)

    assert manager.encore_role_id_updates == []
    content, edit_kwargs = interaction.response.edits[0]
    assert content is None
    updated_view = edit_kwargs["view"]
    assert isinstance(updated_view, EncoreRolePreviewView)
    assert updated_view.selected_roles == ()
    assert updated_view.retained_missing_role_ids == ()
    assert updated_view.removed_missing_role_ids == (99,)


@pytest.mark.asyncio
async def test_encore_role_confirm_omits_removed_missing_ids() -> None:
    manager = RecordingTeamRegisterManager()
    role = FakeRole(id=1, name="Encore", position=10)
    interaction = FakeInteraction(roles=[role])
    view = EncoreRolePreviewView(
        manager,
        selected_roles=[role],
        retained_missing_role_ids=[],
        removed_missing_role_ids=[99],
        metadata=team_register_metadata(),
    )

    await child_with_label(view, "Confirm Save").callback(interaction)

    assert manager.encore_role_id_updates == [[1]]


@pytest.mark.asyncio
async def test_back_to_settings_returns_to_clean_settings_panel() -> None:
    manager = RecordingTeamRegisterManager()
    manager.encore_role_ids = [1]
    role = FakeRole(id=1, name="Encore", position=10)
    interaction = FakeInteraction(roles=[role])
    button = BackToTeamSettingsButton(manager, metadata=team_register_metadata())

    await button.callback(interaction)

    assert len(interaction.response.edits) == 1
    content, edit_kwargs = interaction.response.edits[0]
    assert content is None
    assert edit_kwargs["embed"].title == "Team Register Settings"
    assert isinstance(edit_kwargs["view"], TeamRegisterView)


@pytest.mark.asyncio
async def test_back_to_settings_stops_old_view_and_transfers_message_to_settings() -> (
    None
):
    manager = RecordingTeamRegisterManager()
    manager.encore_role_ids = [1]
    role = FakeRole(id=1, name="Encore", position=10)
    interaction = FakeInteraction(roles=[role])
    message = FakeMessage()
    view = EncoreRoleEditView(
        manager,
        metadata=team_register_metadata(),
        roles=[role],
        encore_role_ids=[1],
        retained_missing_role_ids=[],
    )
    attach_settings_view_message(view, message)

    await child_with_label(view, "Back to Settings").callback(interaction)

    updated_view = interaction.response.edits[0][1]["view"]
    assert view.is_finished()
    assert updated_view.message is message


@pytest.mark.asyncio
async def test_back_to_settings_denies_unauthorized_user() -> None:
    manager = RecordingTeamRegisterManager()
    interaction = unauthorized_interaction()
    button = BackToTeamSettingsButton(manager, metadata=team_register_metadata())

    await button.callback(interaction)

    assert_permission_denied(interaction)
    assert interaction.response.edits == []


@pytest.mark.asyncio
async def test_back_to_settings_uses_fresh_missing_settings_guard() -> None:
    manager = RecordingTeamRegisterManager()
    manager.config_exists = False
    interaction = FakeInteraction()
    button = BackToTeamSettingsButton(manager, metadata=team_register_metadata())

    await button.callback(interaction)

    assert interaction.response.messages == [
        (
            "Team Register settings are no longer configured for this channel.",
            {"ephemeral": True},
        )
    ]
    assert interaction.response.edits == []


@pytest.mark.asyncio
async def test_shift_settings_button_denies_unauthorized_user() -> None:
    manager = RecordingShiftRegisterManager()
    interaction = unauthorized_interaction()
    button = ShiftRegisterButton("Set Up Shift Register", manager)

    await button.callback(interaction)

    assert_permission_denied(interaction)
    assert interaction.response.modals == []


@pytest.mark.asyncio
async def test_shift_settings_button_allows_authorized_user() -> None:
    manager = RecordingShiftRegisterManager()
    manager.config_exists = False
    interaction = FakeInteraction()
    button = ShiftRegisterButton("Set Up Shift Register", manager)

    await button.callback(interaction)

    assert len(interaction.response.modals) == 1
    assert isinstance(interaction.response.modals[0], ShiftRegisterSheetModal)
    assert interaction.response.messages == []


@pytest.mark.asyncio
async def test_shift_setup_button_with_existing_config_sends_current_panel() -> None:
    manager = RecordingShiftRegisterManager()
    interaction = FakeInteraction()
    button = ShiftRegisterButton("Set Up Shift Register", manager)

    await button.callback(interaction)

    assert interaction.response.deferred == [True]
    assert interaction.response.modals == []
    assert len(interaction.followup.messages) == 1
    content, kwargs = interaction.followup.messages[0]
    assert content == (
        "Shift Register is already configured for this channel. "
        "Here are the current settings."
    )
    assert kwargs["ephemeral"] is True
    assert kwargs["embed"].title == "Shift Register Settings"
    assert kwargs["embed"].description == (
        "Shift Register is configured for this channel. "
        "Use the buttons below to update sheet settings, shift timeline, "
        "or recruitment time range."
    )
    field_map = {field.name: field.value for field in kwargs["embed"].fields}
    assert field_map["Shift Timeline"] == (
        "- **Day Number** -> `2`\n"
        "- **Event Date** -> `2026-08-12`\n"
        "- **Submission Deadline** -> `2026-08-12 21:00 JST`\n"
        "- **Draft Shift Proposal** -> `2026-08-13 20:00 JST`\n"
        "- **Final Shift Notice** -> `2026-08-14 18:00 JST`"
    )
    assert field_map["Recruitment Time Range"] == "`4-28`"
    assert [child.label for child in kwargs["view"].children] == [
        "Edit Sheet Settings",
        "Edit Shift Timeline",
        "Edit Recruitment Time Range",
    ]
    assert kwargs["embed"].footer.text is None
    assert kwargs["wait"] is True
    assert kwargs["view"].message is interaction.followup.sent_message_objects[0]


@pytest.mark.asyncio
async def test_shift_setup_button_storage_error_sends_safe_message() -> None:
    manager = RecordingShiftRegisterManager()
    manager.fresh_config_error = DBConnectionError("private database host")
    interaction = FakeInteraction()
    button = ShiftRegisterButton("Set Up Shift Register", manager)

    await button.callback(interaction)

    assert interaction.response.modals == []
    assert interaction.followup.messages == []
    assert len(interaction.response.messages) == 1
    content, kwargs = interaction.response.messages[0]
    assert content is not None
    assert_safe_settings_storage_message(content)
    assert kwargs == {"ephemeral": True}


@pytest.mark.asyncio
async def test_shift_edit_settings_button_uses_fresh_missing_settings_guard() -> None:
    manager = RecordingShiftRegisterManager()
    manager.config_exists = False
    interaction = FakeInteraction()
    view = ShiftRegisterView(
        manager,
        has_existing_settings=True,
        sheet_url="https://sheet.example",
        entry_worksheet_title="Entry",
        draft_worksheet_title="Draft",
        final_schedule_worksheet_title="Final",
        final_schedule_anchor_cell="B2",
    )

    await child_with_label(view, "Edit Sheet Settings").callback(interaction)

    assert interaction.response.messages == [
        (
            "Shift Register settings are no longer configured for this channel.",
            {"ephemeral": True},
        )
    ]
    assert interaction.response.modals == []


@pytest.mark.asyncio
async def test_shift_settings_view_disables_controls_on_timeout() -> None:
    manager = RecordingShiftRegisterManager()
    view = ShiftRegisterView(
        manager,
        has_existing_settings=True,
        sheet_url="https://sheet.example",
        entry_worksheet_title="Entry",
        draft_worksheet_title="Draft",
        final_schedule_worksheet_title="Final",
        final_schedule_anchor_cell="B2",
    )
    message = FakeMessage()
    attach_settings_view_message(view, message)

    await view.on_timeout()

    assert view.timeout == SETTINGS_VIEW_TIMEOUT_SECONDS
    assert all(child.disabled for child in view.children)
    assert message.edits == [((), {"view": view})]


@pytest.mark.asyncio
async def test_shift_existing_edit_button_uses_local_modal_defaults() -> None:
    manager = RecordingShiftRegisterManager()
    manager.sheet_url = "https://fresh.sheet.example"
    manager.final_schedule_anchor_cell = "D8"
    manager.metadata_error = GoogleSheetsError(
        GoogleSheetsErrorKind.INVALID_URL,
        "Check the Google Sheet link and save the settings again.",
    )
    interaction = FakeInteraction()
    view = ShiftRegisterView(
        manager,
        has_existing_settings=True,
        sheet_url="https://stale.sheet.example",
        entry_worksheet_title="Stale Entry",
        draft_worksheet_title="Stale Draft",
        final_schedule_worksheet_title="Stale Final",
        final_schedule_anchor_cell="B2",
    )

    await child_with_label(view, "Edit Sheet Settings").callback(interaction)

    assert interaction.response.messages == []
    assert len(interaction.response.modals) == 1
    modal = interaction.response.modals[0]
    assert isinstance(modal, ShiftRegisterSheetModal)
    assert modal.sheet_url.default == "https://fresh.sheet.example"
    assert modal.entry_worksheet_title.default == "Stale Entry"
    assert modal.draft_worksheet_title.default == "Stale Draft"
    assert modal.final_schedule_worksheet_title.default == "Stale Final"
    assert modal.final_schedule_anchor_cell.default == "D8"


@pytest.mark.asyncio
async def test_shift_timeline_button_prefills_modal_from_fresh_config() -> None:
    manager = RecordingShiftRegisterManager()
    interaction = FakeInteraction()
    view = ShiftRegisterView(manager, has_existing_settings=True)

    await child_with_label(view, "Edit Shift Timeline").callback(interaction)

    assert interaction.response.messages == []
    assert len(interaction.response.modals) == 1
    modal = interaction.response.modals[0]
    assert isinstance(modal, ShiftTimelineModal)
    assert modal.day_number.default == "2"
    assert modal.event_date.default == "2026-08-12"
    assert modal.submission_deadline_at.default == "2026-08-12 21"
    assert modal.draft_shift_proposal_at.default == "2026-08-13 20"
    assert modal.final_shift_notice_at.default == "2026-08-14 18"


@pytest.mark.asyncio
async def test_shift_timeline_button_storage_error_sends_safe_message() -> None:
    manager = RecordingShiftRegisterManager()
    manager.fresh_config_error = DBConnectionError("private database host")
    interaction = FakeInteraction()
    view = ShiftRegisterView(manager, has_existing_settings=True)

    await child_with_label(view, "Edit Shift Timeline").callback(interaction)

    assert interaction.response.modals == []
    assert len(interaction.response.messages) == 1
    content, kwargs = interaction.response.messages[0]
    assert content is not None
    assert_safe_settings_storage_message(content)
    assert kwargs == {"ephemeral": True}


@pytest.mark.asyncio
async def test_shift_recruitment_range_button_prefills_modal_from_fresh_config() -> (
    None
):
    manager = RecordingShiftRegisterManager()
    manager.recruitment_time_ranges = [{"start": 4, "end": 12}]
    interaction = FakeInteraction()
    view = ShiftRegisterView(manager, has_existing_settings=True)

    await child_with_label(view, "Edit Recruitment Time Range").callback(interaction)

    assert interaction.response.messages == []
    assert len(interaction.response.modals) == 1
    modal = interaction.response.modals[0]
    assert isinstance(modal, ShiftRecruitmentRangeModal)
    assert modal.recruitment_time_range.default == "4-12"


@pytest.mark.asyncio
async def test_shift_modal_submit_denies_unauthorized_user() -> None:
    manager = RecordingShiftRegisterManager()
    interaction = unauthorized_interaction()
    modal = ShiftRegisterSheetModal(manager, sheet_url="https://sheet.example")

    await modal.on_submit(interaction)

    assert_permission_denied(interaction)
    assert interaction.response.deferred == []
    assert manager.upsert_calls == []
    assert manager.anchor_updates == []


@pytest.mark.asyncio
async def test_shift_modal_submit_allows_authorized_user() -> None:
    manager = RecordingShiftRegisterManager()
    interaction = FakeInteraction()
    modal = ShiftRegisterSheetModal(
        manager,
        sheet_url="https://sheet.example",
        entry_worksheet_title="Entry",
        draft_worksheet_title="Draft",
        final_schedule_worksheet_title="Final",
        final_schedule_anchor_cell="B2",
    )

    await modal.on_submit(interaction)

    assert interaction.response.deferred == [True]
    assert len(manager.upsert_calls) == 1
    assert manager.anchor_updates == ["B2"]
    assert len(interaction.followup.messages) == 1
    _, kwargs = interaction.followup.messages[0]
    embed = kwargs["embed"]
    assert embed.title == "Shift Register Settings Saved"
    assert embed.description == (
        "Your Shift Register settings were saved. "
        "Use the buttons below to update sheet settings, shift timeline, "
        "or recruitment time range."
    )
    assert embed.footer.text is None
    assert kwargs["wait"] is True
    assert kwargs["view"].message is interaction.followup.sent_message_objects[0]


@pytest.mark.asyncio
async def test_shift_edit_modal_submit_uses_fresh_missing_settings_guard() -> None:
    manager = RecordingShiftRegisterManager()
    manager.config_exists = False
    interaction = FakeInteraction()
    modal = ShiftRegisterSheetModal(
        manager,
        sheet_url="https://sheet.example",
        entry_worksheet_title="Entry",
        draft_worksheet_title="Draft",
        final_schedule_worksheet_title="Final",
        final_schedule_anchor_cell="B2",
        requires_existing_settings=True,
    )

    await modal.on_submit(interaction)

    assert interaction.response.messages == [
        (
            "Shift Register settings are no longer configured for this channel.",
            {"ephemeral": True},
        )
    ]
    assert interaction.response.deferred == []
    assert manager.upsert_calls == []
    assert manager.anchor_updates == []


@pytest.mark.asyncio
async def test_shift_setup_modal_reports_partial_success_for_sheet_save_error() -> None:
    manager = FailingShiftRegisterManager()
    interaction = FakeInteraction()
    modal = ShiftRegisterSheetModal(
        manager,
        sheet_url="https://private.sheet.example",
        entry_worksheet_title="Entry",
        draft_worksheet_title="Draft",
        final_schedule_worksheet_title="Final",
        final_schedule_anchor_cell="B2",
    )

    await modal.on_submit(interaction)

    assert interaction.response.deferred == [True]
    assert manager.anchor_updates == []
    assert len(interaction.followup.messages) == 1
    content, kwargs = interaction.followup.messages[0]
    assert content is not None
    assert "Some changes may have been saved" in content
    assert "Reference: `STG-" in content
    assert_no_private_storage_terms(content)
    assert kwargs == {"ephemeral": True}
    assert "private.sheet.example" not in str(interaction.followup.messages)


@pytest.mark.asyncio
async def test_shift_setup_modal_reports_partial_success_when_initial_save_fails() -> (
    None
):
    manager = RecordingShiftRegisterManager()
    manager.upsert_error = IntegrityError("private constraint")
    interaction = FakeInteraction()
    modal = ShiftRegisterSheetModal(
        manager,
        sheet_url="https://sheet.example",
        entry_worksheet_title="Entry",
        draft_worksheet_title="Draft",
        final_schedule_worksheet_title="Final",
        final_schedule_anchor_cell="B2",
    )

    await modal.on_submit(interaction)

    assert interaction.response.deferred == [True]
    assert manager.anchor_updates == []
    assert len(interaction.followup.messages) == 1
    content, kwargs = interaction.followup.messages[0]
    assert content is not None
    assert "Some changes may have been saved" in content
    assert "Reference: `STG-" in content
    assert_no_private_storage_terms(content)
    assert kwargs == {"ephemeral": True}


@pytest.mark.asyncio
async def test_shift_setup_modal_reports_partial_success_when_anchor_save_fails() -> (
    None
):
    manager = RecordingShiftRegisterManager()
    manager.save_error = DBConnectionError("private database host")
    interaction = FakeInteraction()
    modal = ShiftRegisterSheetModal(
        manager,
        sheet_url="https://sheet.example",
        entry_worksheet_title="Entry",
        draft_worksheet_title="Draft",
        final_schedule_worksheet_title="Final",
        final_schedule_anchor_cell="B2",
    )

    await modal.on_submit(interaction)

    assert interaction.response.deferred == [True]
    assert len(manager.upsert_calls) == 1
    assert manager.anchor_updates == []
    assert len(interaction.followup.messages) == 1
    content, kwargs = interaction.followup.messages[0]
    assert content is not None
    assert "Some changes may have been saved" in content
    assert "Reference: `STG-" in content
    assert_no_private_storage_terms(content)
    assert kwargs == {"ephemeral": True}


@pytest.mark.asyncio
async def test_shift_timeline_modal_submit_updates_timeline() -> None:
    manager = RecordingShiftRegisterManager()
    interaction = FakeInteraction()
    modal = ShiftTimelineModal(
        manager,
        day_number="3",
        event_date="2026-08-12",
        submission_deadline_at="8/12 21",
        draft_shift_proposal_at="2026/08/13 20",
        final_shift_notice_at="2026-08-14 18",
    )

    await modal.on_submit(interaction)

    assert interaction.response.deferred == [True]
    assert manager.timeline_updates == [
        {
            "day_number": 3,
            "event_date": dt.date(2026, 8, 12),
            "submission_deadline_at": dt.datetime(2026, 8, 12, 12, tzinfo=dt.UTC),
            "draft_shift_proposal_at": dt.datetime(2026, 8, 13, 11, tzinfo=dt.UTC),
            "final_shift_notice_at": dt.datetime(2026, 8, 14, 9, tzinfo=dt.UTC),
        }
    ]
    assert len(interaction.followup.messages) == 1
    _, kwargs = interaction.followup.messages[0]
    assert kwargs["embed"].title == "Shift Register Settings Saved"


@pytest.mark.asyncio
async def test_shift_timeline_modal_submit_normalizes_full_width_values() -> None:
    manager = RecordingShiftRegisterManager()
    interaction = FakeInteraction()
    modal = ShiftTimelineModal(
        manager,
        day_number="２",  # noqa: RUF001
        event_date="２０２６／０８／１２",  # noqa: RUF001
        submission_deadline_at="８／１２ ２１",  # noqa: RUF001
        draft_shift_proposal_at="",
        final_shift_notice_at="",
    )

    await modal.on_submit(interaction)

    assert interaction.response.deferred == [True]
    assert manager.timeline_updates == [
        {
            "day_number": 2,
            "event_date": dt.date(2026, 8, 12),
            "submission_deadline_at": dt.datetime(2026, 8, 12, 12, tzinfo=dt.UTC),
            "draft_shift_proposal_at": None,
            "final_shift_notice_at": None,
        }
    ]


@pytest.mark.asyncio
async def test_shift_timeline_modal_submit_reports_google_sheets_error_safely() -> None:
    manager = RecordingShiftRegisterManager()
    manager.metadata_error = GoogleSheetsError(
        GoogleSheetsErrorKind.PERMISSION,
        "Check the sheet sharing settings and service account access.",
    )
    interaction = FakeInteraction()
    modal = ShiftTimelineModal(
        manager,
        day_number="3",
        event_date="2026-08-12",
        submission_deadline_at="8/12 21",
        draft_shift_proposal_at="",
        final_shift_notice_at="",
    )

    await modal.on_submit(interaction)

    assert interaction.response.deferred == [True]
    assert len(manager.timeline_updates) == 1
    assert len(interaction.followup.messages) == 1
    content, kwargs = interaction.followup.messages[0]
    assert content is not None
    assert "Some changes may have been saved" in content
    assert "settings view could not be refreshed" in content
    assert "Reference: `STG-" in content
    assert_no_private_storage_terms(content)
    assert kwargs == {"ephemeral": True}


@pytest.mark.asyncio
async def test_shift_timeline_saved_panel_delivery_timeout_is_not_storage_error() -> (
    None
):
    manager = RecordingShiftRegisterManager()
    interaction = FakeInteraction()
    interaction.followup = FirstSendTimeoutFollowup()
    modal = ShiftTimelineModal(
        manager,
        day_number="3",
        event_date="2026-08-12",
        submission_deadline_at="8/12 21",
        draft_shift_proposal_at="",
        final_shift_notice_at="",
    )

    with pytest.raises(TimeoutError, match="discord delivery timeout"):
        await modal.on_submit(interaction)

    assert interaction.response.deferred == [True]
    assert len(manager.timeline_updates) == 1
    assert interaction.followup.messages == []


@pytest.mark.asyncio
async def test_shift_timeline_modal_submit_reports_storage_save_error() -> None:
    manager = RecordingShiftRegisterManager()
    manager.save_error = DBConnectionError("private database host")
    interaction = FakeInteraction()
    modal = ShiftTimelineModal(
        manager,
        day_number="3",
        event_date="2026-08-12",
        submission_deadline_at="8/12 21",
        draft_shift_proposal_at="",
        final_shift_notice_at="",
    )

    await modal.on_submit(interaction)

    assert interaction.response.deferred == [True]
    assert manager.timeline_updates == []
    assert len(interaction.followup.messages) == 1
    content, kwargs = interaction.followup.messages[0]
    assert content is not None
    assert "could not complete this action" in content
    assert "Reference: `STG-" in content
    assert_no_private_storage_terms(content)
    assert kwargs == {"ephemeral": True}


@pytest.mark.asyncio
async def test_shift_timeline_modal_invalid_submit_sends_edit_again_view() -> None:
    manager = RecordingShiftRegisterManager()
    interaction = FakeInteraction()
    modal = ShiftTimelineModal(
        manager,
        day_number="0",
        event_date="2026-08-12",
        submission_deadline_at="8/12 24",
        draft_shift_proposal_at="",
        final_shift_notice_at="",
    )

    await modal.on_submit(interaction)

    assert manager.timeline_updates == []
    assert interaction.response.deferred == []
    assert len(interaction.response.messages) == 1
    content, kwargs = interaction.response.messages[0]
    assert content == (
        "Shift timeline could not be saved:\n"
        "- Day Number must be a positive integer.\n"
        "- Submission Deadline hour must be 0-23."
    )
    edit_again_view = kwargs["view"]
    retry_interaction = FakeInteraction()
    await child_with_label(edit_again_view, "Edit Again").callback(retry_interaction)
    assert isinstance(retry_interaction.response.modals[0], ShiftTimelineModal)
    assert retry_interaction.response.modals[0].day_number.default == "0"
    assert (
        retry_interaction.response.modals[0].submission_deadline_at.default == "8/12 24"
    )


@pytest.mark.asyncio
async def test_shift_recruitment_range_modal_submit_updates_range() -> None:
    manager = RecordingShiftRegisterManager()
    interaction = FakeInteraction()
    modal = ShiftRecruitmentRangeModal(manager, recruitment_time_range="4-8, 8-12")

    await modal.on_submit(interaction)

    assert interaction.response.deferred == [True]
    assert [ranges.to_json() for ranges in manager.recruitment_range_updates] == [
        [{"start": 4, "end": 12}]
    ]
    assert len(interaction.followup.messages) == 1
    _, kwargs = interaction.followup.messages[0]
    assert kwargs["embed"].title == "Shift Register Settings Saved"


@pytest.mark.asyncio
async def test_shift_recruitment_range_modal_submit_normalizes_full_width_values() -> (
    None
):
    manager = RecordingShiftRegisterManager()
    interaction = FakeInteraction()
    modal = ShiftRecruitmentRangeModal(
        manager,
        recruitment_time_range="４－８，８－１２",  # noqa: RUF001
    )

    await modal.on_submit(interaction)

    assert interaction.response.deferred == [True]
    assert [ranges.to_json() for ranges in manager.recruitment_range_updates] == [
        [{"start": 4, "end": 12}]
    ]


@pytest.mark.asyncio
async def test_shift_recruitment_range_modal_blank_resets_to_default() -> None:
    manager = RecordingShiftRegisterManager()
    interaction = FakeInteraction()
    modal = ShiftRecruitmentRangeModal(manager, recruitment_time_range="")

    await modal.on_submit(interaction)

    assert [ranges.to_json() for ranges in manager.recruitment_range_updates] == [
        [{"start": 4, "end": 28}]
    ]


@pytest.mark.asyncio
async def test_shift_recruitment_range_modal_reports_storage_save_error() -> None:
    manager = RecordingShiftRegisterManager()
    manager.save_error = DBConnectionError("private database host")
    interaction = FakeInteraction()
    modal = ShiftRecruitmentRangeModal(manager, recruitment_time_range="4-8, 8-12")

    await modal.on_submit(interaction)

    assert interaction.response.deferred == [True]
    assert manager.recruitment_range_updates == []
    assert len(interaction.followup.messages) == 1
    content, kwargs = interaction.followup.messages[0]
    assert content is not None
    assert "could not complete this action" in content
    assert "Reference: `STG-" in content
    assert_no_private_storage_terms(content)
    assert kwargs == {"ephemeral": True}


@pytest.mark.asyncio
async def test_shift_recruitment_range_modal_invalid_sends_edit_again_view() -> None:
    manager = RecordingShiftRegisterManager()
    interaction = FakeInteraction()
    modal = ShiftRecruitmentRangeModal(manager, recruitment_time_range="28-4")

    await modal.on_submit(interaction)

    assert manager.recruitment_range_updates == []
    assert interaction.response.deferred == []
    assert len(interaction.response.messages) == 1
    content, kwargs = interaction.response.messages[0]
    assert content == (
        "Recruitment time range could not be saved:\n"
        "- Use ranges like 4-28 or 4-12, 20-28 within 0-30."
    )
    edit_again_view = kwargs["view"]
    retry_interaction = FakeInteraction()
    await child_with_label(edit_again_view, "Edit Again").callback(retry_interaction)
    assert isinstance(retry_interaction.response.modals[0], ShiftRecruitmentRangeModal)
    assert retry_interaction.response.modals[0].recruitment_time_range.default == "28-4"


@pytest.mark.asyncio
async def test_announcement_language_save_reports_storage_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail_save_announcement_languages(*_: object) -> None:
        message = "private database host"
        raise DBConnectionError(message)

    monkeypatch.setattr(
        "components.ui_language_settings.save_announcement_languages",
        fail_save_announcement_languages,
    )
    view = AnnouncementLanguageSettingsView(
        guild_id=111,
        language_codes=["ja", "en"],
    )
    interaction = FakeInteraction()

    await child_with_label(view, "Save").callback(interaction)

    assert len(interaction.response.messages) == 1
    content, kwargs = interaction.response.messages[0]
    assert content is not None
    assert "could not complete this action" in content
    assert "Reference: `STG-" in content
    assert_no_private_storage_terms(content)
    assert kwargs == {"ephemeral": True}


@pytest.mark.asyncio
async def test_disable_and_clear_confirm_denies_unauthorized_user() -> None:
    interaction = unauthorized_interaction()
    view = DisableAndClearConfirmView()

    await view.children[0].callback(interaction)

    assert_permission_denied(interaction)
    assert view.value is False
    assert view.is_finished()
    assert interaction.response.edits == []


@pytest.mark.asyncio
async def test_disable_and_clear_confirm_allows_authorized_user() -> None:
    interaction = FakeInteraction()
    view = DisableAndClearConfirmView()

    await view.children[0].callback(interaction)

    assert view.value is True
    assert interaction.response.edits == [
        ("Confirmed. Clearing settings...", {"view": None})
    ]


@pytest.mark.asyncio
async def test_disable_and_clear_cancel_allows_authorized_user() -> None:
    interaction = FakeInteraction()
    view = DisableAndClearConfirmView()

    await view.children[1].callback(interaction)

    assert view.value is False
    assert interaction.response.edits == [("Operation cancelled.", {"view": None})]
