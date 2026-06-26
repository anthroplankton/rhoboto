from __future__ import annotations

from types import SimpleNamespace

import pytest

from components.ui_feature_channel import DisableAndClearConfirmView
from components.ui_permissions import MISSING_SETTINGS_PERMISSION_MESSAGE
from components.ui_shift_register import ShiftRegisterButton, ShiftRegisterSheetModal
from components.ui_team_register import (
    EncoreRoleMultiSelect,
    TeamRegisterButton,
    TeamRegisterSheetModal,
)
from tests.fakes import FakeInteraction, FakeRole
from utils.google_sheets_errors import GoogleSheetsError, GoogleSheetsErrorKind


class RecordingTeamRegisterManager:
    def __init__(self) -> None:
        self.upsert_calls: list[dict[str, object]] = []
        self.encore_role_updates: list[list[object]] = []

    async def upsert_sheet_config_and_worksheets(
        self,
        *,
        sheet_url: str,
        team_worksheet_titles: list[str],
        summary_worksheet_title: str,
    ) -> SimpleNamespace:
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
        return SimpleNamespace(encore_role_ids=[])

    async def update_encore_roles_record(self, roles: list[object]) -> None:
        self.encore_role_updates.append(roles)


class RecordingShiftRegisterManager:
    def __init__(self) -> None:
        self.upsert_calls: list[dict[str, object]] = []
        self.anchor_updates: list[str] = []

    async def upsert_sheet_config_and_worksheets(
        self,
        *,
        sheet_url: str,
        entry_worksheet_title: str,
        draft_worksheet_title: str,
        final_schedule_worksheet_title: str,
    ) -> SimpleNamespace:
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
        self.anchor_updates.append(anchor_cell)


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


@pytest.mark.asyncio
async def test_team_settings_button_denies_unauthorized_user() -> None:
    manager = RecordingTeamRegisterManager()
    interaction = unauthorized_interaction()
    button = TeamRegisterButton("Setup Team Register", manager)

    await button.callback(interaction)

    assert_permission_denied(interaction)
    assert interaction.response.modals == []


@pytest.mark.asyncio
async def test_team_settings_button_allows_authorized_user() -> None:
    manager = RecordingTeamRegisterManager()
    interaction = FakeInteraction()
    button = TeamRegisterButton("Setup Team Register", manager)

    await button.callback(interaction)

    assert len(interaction.response.modals) == 1
    assert isinstance(interaction.response.modals[0], TeamRegisterSheetModal)
    assert interaction.response.messages == []


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


@pytest.mark.asyncio
async def test_team_modal_submit_reports_google_sheets_error_safely() -> None:
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
    assert interaction.followup.messages == [
        (
            "Google Sheets could not complete this action. "
            "Check the sheet sharing settings and service account access.",
            {"ephemeral": True},
        )
    ]
    assert "private.sheet.example" not in str(interaction.followup.messages)


@pytest.mark.asyncio
async def test_encore_role_select_denies_unauthorized_user() -> None:
    manager = RecordingTeamRegisterManager()
    role = FakeRole(id=1, name="Encore", position=10)
    interaction = unauthorized_interaction()
    select = EncoreRoleMultiSelect(manager, roles=[role])
    select._values = ["1"]  # noqa: SLF001

    await select.callback(interaction)

    assert_permission_denied(interaction)
    assert manager.encore_role_updates == []


@pytest.mark.asyncio
async def test_encore_role_select_allows_authorized_user() -> None:
    manager = RecordingTeamRegisterManager()
    role = FakeRole(id=1, name="Encore", position=10)
    interaction = FakeInteraction(roles=[role])
    select = EncoreRoleMultiSelect(manager, roles=[role])
    select._values = ["1"]  # noqa: SLF001

    await select.callback(interaction)

    assert manager.encore_role_updates == [[role]]
    assert interaction.response.messages == [
        ("Encore roles updated: <@&1>", {"ephemeral": True})
    ]


@pytest.mark.asyncio
async def test_shift_settings_button_denies_unauthorized_user() -> None:
    manager = RecordingShiftRegisterManager()
    interaction = unauthorized_interaction()
    button = ShiftRegisterButton("Setup Shift Register", manager)

    await button.callback(interaction)

    assert_permission_denied(interaction)
    assert interaction.response.modals == []


@pytest.mark.asyncio
async def test_shift_settings_button_allows_authorized_user() -> None:
    manager = RecordingShiftRegisterManager()
    interaction = FakeInteraction()
    button = ShiftRegisterButton("Setup Shift Register", manager)

    await button.callback(interaction)

    assert len(interaction.response.modals) == 1
    assert isinstance(interaction.response.modals[0], ShiftRegisterSheetModal)
    assert interaction.response.messages == []


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


@pytest.mark.asyncio
async def test_shift_modal_submit_reports_google_sheets_error_safely() -> None:
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
    assert interaction.followup.messages == [
        (
            "Google Sheets could not complete this action. "
            "Check the Google Sheet link and save the settings again.",
            {"ephemeral": True},
        )
    ]
    assert "private.sheet.example" not in str(interaction.followup.messages)


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
