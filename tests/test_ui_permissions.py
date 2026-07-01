from __future__ import annotations

from types import SimpleNamespace

import pytest
from discord import ButtonStyle

from components.ui_feature_channel import DisableAndClearConfirmView
from components.ui_permissions import MISSING_SETTINGS_PERMISSION_MESSAGE
from components.ui_shift_register import (
    ShiftRegisterButton,
    ShiftRegisterSheetModal,
    ShiftRegisterView,
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
        self.metadata_error: GoogleSheetsError | None = None

    async def upsert_sheet_config_and_worksheets(
        self,
        *,
        sheet_url: str,
        team_worksheet_titles: list[str],
        summary_worksheet_title: str,
    ) -> SimpleNamespace:
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
        if not self.config_exists:
            msg = "Sheet configuration not found."
            raise RuntimeError(msg)
        return SimpleNamespace(
            sheet_url=self.sheet_url,
            encore_role_ids=self.encore_role_ids,
        )

    async def get_fresh_sheet_config(self) -> SimpleNamespace | None:
        if not self.config_exists:
            return None
        return await self.get_sheet_config()

    async def fetch_google_sheets_metadata(self) -> SimpleNamespace:
        if self.metadata_error is not None:
            raise self.metadata_error
        return self.metadata

    async def update_encore_roles_record(self, roles: list[object]) -> None:
        self.encore_role_updates.append(roles)

    async def update_encore_role_ids_record(self, role_ids: list[int]) -> None:
        self.encore_role_id_updates.append(role_ids)
        self.encore_role_ids = role_ids


class RecordingShiftRegisterManager:
    def __init__(self) -> None:
        self.upsert_calls: list[dict[str, object]] = []
        self.anchor_updates: list[str] = []
        self.config_exists = True
        self.sheet_url = "https://sheet.example"
        self.final_schedule_anchor_cell = "B2"
        self.metadata = SimpleNamespace(
            sheet_url="https://sheet.example",
            entry_worksheets=SimpleNamespace(title="Entry", id=101),
            draft_worksheet=SimpleNamespace(title="Draft", id=102),
            final_schedule_worksheet=SimpleNamespace(title="Final", id=103),
        )
        self.metadata_error: GoogleSheetsError | None = None

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

    async def get_sheet_config(self) -> SimpleNamespace:
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
        )

    async def get_fresh_sheet_config(self) -> SimpleNamespace | None:
        if not self.config_exists:
            return None
        return await self.get_sheet_config()

    async def fetch_google_sheets_metadata(self) -> SimpleNamespace:
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
    manager.config_exists = False
    interaction = FakeInteraction()
    button = TeamRegisterButton("Setup Team Register", manager)

    await button.callback(interaction)

    assert len(interaction.response.modals) == 1
    assert isinstance(interaction.response.modals[0], TeamRegisterSheetModal)
    assert interaction.response.messages == []


@pytest.mark.asyncio
async def test_team_setup_button_with_existing_config_sends_current_panel() -> None:
    manager = RecordingTeamRegisterManager()
    interaction = FakeInteraction()
    button = TeamRegisterButton("Setup Team Register", manager)

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
    select = EncoreRoleSelect(
        manager,
        roles=[role],
        encore_role_ids=[],
        retained_missing_role_ids=[99],
        metadata=team_register_metadata(),
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
async def test_encore_role_select_uses_fresh_missing_settings_guard() -> None:
    manager = RecordingTeamRegisterManager()
    manager.config_exists = False
    role = FakeRole(id=1, name="Encore", position=10)
    interaction = FakeInteraction(roles=[role])
    select = EncoreRoleSelect(
        manager,
        roles=[role],
        encore_role_ids=[],
        retained_missing_role_ids=[99],
        metadata=team_register_metadata(),
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
    manager.metadata_error = GoogleSheetsError(
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
    assert interaction.response.edits == [
        (
            "Encore roles saved, but the settings view could not be refreshed. "
            "Google Sheets could not complete this action. "
            "Check the sheet sharing settings and service account access.",
            {"embed": None, "view": None},
        )
    ]


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
    button = ShiftRegisterButton("Setup Shift Register", manager)

    await button.callback(interaction)

    assert_permission_denied(interaction)
    assert interaction.response.modals == []


@pytest.mark.asyncio
async def test_shift_settings_button_allows_authorized_user() -> None:
    manager = RecordingShiftRegisterManager()
    manager.config_exists = False
    interaction = FakeInteraction()
    button = ShiftRegisterButton("Setup Shift Register", manager)

    await button.callback(interaction)

    assert len(interaction.response.modals) == 1
    assert isinstance(interaction.response.modals[0], ShiftRegisterSheetModal)
    assert interaction.response.messages == []


@pytest.mark.asyncio
async def test_shift_setup_button_with_existing_config_sends_current_panel() -> None:
    manager = RecordingShiftRegisterManager()
    interaction = FakeInteraction()
    button = ShiftRegisterButton("Setup Shift Register", manager)

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
        "Use the button below to update sheet settings."
    )
    assert kwargs["embed"].footer.text is None


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

    await child_with_label(view, "Edit Shift Register Settings").callback(interaction)

    assert interaction.response.messages == [
        (
            "Shift Register settings are no longer configured for this channel.",
            {"ephemeral": True},
        )
    ]
    assert interaction.response.modals == []


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

    await child_with_label(view, "Edit Shift Register Settings").callback(interaction)

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
        "Use the button below to edit sheet settings."
    )
    assert embed.footer.text is None


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


@pytest.mark.asyncio
async def test_disable_and_clear_cancel_allows_authorized_user() -> None:
    interaction = FakeInteraction()
    view = DisableAndClearConfirmView()

    await view.children[1].callback(interaction)

    assert view.value is False
    assert interaction.response.edits == [("Operation cancelled.", {"view": None})]
