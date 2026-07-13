from __future__ import annotations

import datetime as dt
import logging
from types import SimpleNamespace

import pytest
from discord import ButtonStyle
from tortoise.exceptions import DBConnectionError, IntegrityError

from cogs.shift_register import ShiftRegister
from cogs.team_register import TeamRegister
from components import ui_shift_register, ui_team_register
from components.ui_auto_guide import (
    AUTO_GUIDE_BUTTON_VIEW_TIMEOUT_SECONDS,
    AUTO_GUIDE_DELETE_CUSTOM_ID_PREFIX,
    LATEST_GUIDE_FIELD_NAME,
    LATEST_GUIDE_SETTINGS_REFRESH_FAILED_WARNING,
    AutoGuideButtonsView,
    LatestGuideButton,
    auto_guide_button_language,
    auto_guide_delete_custom_id,
    discord_message_url,
)
from components.ui_feature_channel import (
    ConfirmDeleteUserDataView,
    DisableAndClearConfirmView,
)
from components.ui_language_settings import AnnouncementLanguageSettingsView
from components.ui_permissions import MISSING_SETTINGS_PERMISSION_MESSAGE
from components.ui_settings_flow import (
    SETTINGS_STORAGE_EXCEPTIONS,
    SETTINGS_VIEW_TIMEOUT_SECONDS,
    attach_settings_view_message,
)
from components.ui_shift_register import (
    ApplyTeamSourceButton,
    GenerateDraftConfirmView,
    ManageTeamSourceButton,
    ShiftRecruitmentRangeModal,
    ShiftRegisterButton,
    ShiftRegisterSheetModal,
    ShiftRegisterView,
    ShiftTimelineModal,
    TeamSourceSelect,
    TeamSourceView,
    build_current_settings_embed as build_shift_current_settings_embed,
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
    build_current_settings_embed as build_team_current_settings_embed,
)
from tests.fakes import FakeInteraction, FakeRole
from utils import team_register_manager as team_register_manager_module
from utils.google_sheets_errors import GoogleSheetsError, GoogleSheetsErrorKind
from utils.shift_register_manager import (
    TeamSource,
    TeamSourceResolution,
    TeamSourceStatus,
    TeamSummaryColumns,
)
from utils.storage_errors import partial_success_storage_error
from utils.structs_base import WorksheetContractError
from utils.team_register_structs import (
    SummaryWorksheetMetadata,
    TeamRegisterGoogleSheetsMetadata,
    TeamWorksheetMetadata,
)

TEAM_SETTINGS_SHEET_URL = "https://docs.google.com/spreadsheets/d/team-settings/edit"


def team_source_resolution(
    *,
    landing_worksheet_id: int = 201,
) -> TeamSourceResolution:
    config = SimpleNamespace(
        sheet_url="https://team.sheet.example",
        landing_worksheet_id=landing_worksheet_id,
        feature_channel=SimpleNamespace(channel_id=22),
    )
    metadata = TeamRegisterGoogleSheetsMetadata.from_subtyped_worksheets(
        config.sheet_url,
        [
            TeamWorksheetMetadata(101, "Main Team", None),
            TeamWorksheetMetadata(102, "Encore Team", None),
            SummaryWorksheetMetadata(201, "Renamed Summary", None),
        ],
    )
    return TeamSourceResolution(
        TeamSourceStatus.AVAILABLE,
        TeamSource(
            config=config,
            metadata=metadata,
            summary_columns=TeamSummaryColumns(
                username=1,
                roles=3,
                main_isv=4,
                main_power=5,
                encore_isv=6,
                encore_power=7,
                import_last_column="G",
            ),
        ),
    )


class RecordingAsyncLock:
    def __init__(self) -> None:
        self.keys: list[int] = []
        self.entered = 0
        self.exited = 0

    def __call__(self, key: int) -> RecordingAsyncLock:
        self.keys.append(key)
        return self

    async def __aenter__(self) -> None:
        self.entered += 1

    async def __aexit__(self, *_args: object) -> None:
        self.exited += 1


class RecordingTeamRegisterManager:
    def __init__(self) -> None:
        self.upsert_calls: list[dict[str, object]] = []
        self.encore_role_updates: list[list[object]] = []
        self.encore_role_id_updates: list[list[int]] = []
        self.encore_reconciliation_calls: list[dict[str, object]] = []
        self.encore_role_ids: list[int] = []
        self.config_exists = True
        self.feature_channel = SimpleNamespace(
            id=222,
            guild_id=111,
            channel_id=222,
            feature_name="team_register",
        )
        self.sheet_url = "https://docs.google.com/spreadsheets/d/team-encore/edit"
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
        member_by_names: dict[str, object],
    ) -> SimpleNamespace:
        assert isinstance(member_by_names, dict)
        if self.upsert_error is not None:
            raise self.upsert_error
        self.config_exists = True
        self.sheet_url = sheet_url
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

    async def update_encore_role_ids_and_summary(
        self,
        role_ids: list[int],
        member_by_names: dict[str, object],
    ) -> SimpleNamespace:
        if self.save_error is not None:
            raise self.save_error
        self.encore_reconciliation_calls.append(
            {
                "role_ids": role_ids,
                "member_by_names": member_by_names,
            }
        )
        self.encore_role_id_updates.append(role_ids)
        self.encore_role_ids = role_ids
        if self.refresh_error is not None:
            error = partial_success_storage_error(self.refresh_error)
            assert error is not None
            raise error
        return self.metadata


class RecordingShiftRegisterManager:
    def __init__(self) -> None:
        self.upsert_calls: list[dict[str, object]] = []
        self.anchor_updates: list[str] = []
        self.timeline_updates: list[dict[str, object]] = []
        self.recruitment_range_updates: list[object] = []
        self.config_exists = True
        self.feature_channel = SimpleNamespace(id=222, channel_id=222)
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
        self.team_source = TeamSourceResolution(TeamSourceStatus.MISSING)
        self.team_source_apply_calls: list[int] = []
        self.team_source_candidate_channel_ids: tuple[int, ...] = ()

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
        self.config_exists = True
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

    async def resolve_team_source(self) -> TeamSourceResolution:
        return self.team_source

    async def select_team_source_and_repair(
        self,
        team_channel_id: int,
    ) -> TeamSourceResolution:
        self.team_source_apply_calls.append(team_channel_id)
        return self.team_source

    async def get_team_source_candidate_channel_ids(self) -> tuple[int, ...]:
        return self.team_source_candidate_channel_ids


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


def assert_latest_guide_enabled_panel(
    kwargs: dict[str, object],
    *,
    toggle_callback: object,
) -> None:
    embed = kwargs["embed"]
    view = kwargs["view"]
    field_map = {field.name: field.value for field in embed.fields}
    assert field_map[LATEST_GUIDE_FIELD_NAME] == (
        r"- \🟢 `Enabled` : A short guide is automatically kept near the newest "
        "messages. When a full guide announcement exists, the short guide replies "
        "to it."
    )
    assert view.children[0].label == "Disable Latest Guide"
    assert view.children[0].style is ButtonStyle.secondary
    assert view.children[0].toggle_callback is toggle_callback


def assert_latest_guide_disabled_panel(kwargs: dict[str, object]) -> None:
    embed = kwargs["embed"]
    view = kwargs["view"]
    field_map = {field.name: field.value for field in embed.fields}
    assert field_map[LATEST_GUIDE_FIELD_NAME] == (
        r"- \⚫ `Disabled` : No short guide is maintained near new messages. Enable "
        "this to keep registration rules visible as the channel moves."
    )
    assert view.children[0].label == "Enable Latest Guide"
    assert view.children[0].style is ButtonStyle.primary
    assert view.children[0].toggle_callback is not None


async def noop_latest_guide_toggle(
    _interaction: object,
    *,
    enabled: bool,
    current_view: object,
) -> None:
    del enabled, current_view


async def latest_guide_is_enabled() -> bool:
    return True


async def latest_guide_disabled_for_feature(_feature_channel: object) -> bool:
    return False


async def noop_latest_guide_refresh(*_: object, **__: object) -> bool:
    return True


class RecordingLatestGuideRefreshCallback:
    def __init__(self, *, result: bool = True) -> None:
        self.result = result
        self.calls: list[tuple[object, object, int]] = []

    async def __call__(self, interaction: object, feature_config: object) -> bool:
        self.calls.append(
            (
                interaction,
                feature_config,
                len(interaction.followup.messages),
            )
        )
        return self.result


def fake_bot() -> SimpleNamespace:
    return SimpleNamespace(
        tree=SimpleNamespace(add_command=lambda _command: None),
        user=None,
    )


def test_team_register_setup_view_uses_set_up_button_label() -> None:
    manager = RecordingTeamRegisterManager()

    view = TeamRegisterView(manager, has_existing_settings=False)

    assert [child.label for child in view.children] == ["Set Up Team Register"]


def test_shift_register_setup_view_uses_set_up_button_label() -> None:
    manager = RecordingShiftRegisterManager()

    view = ShiftRegisterView(manager, has_existing_settings=False)

    assert [child.label for child in view.children] == ["Set Up Shift Register"]


def test_setup_views_do_not_show_latest_guide_button() -> None:
    team_view = TeamRegisterView(
        RecordingTeamRegisterManager(),
        has_existing_settings=False,
        latest_guide_enabled=True,
        latest_guide_toggle_callback=noop_latest_guide_toggle,
    )
    shift_view = ShiftRegisterView(
        RecordingShiftRegisterManager(),
        has_existing_settings=False,
        latest_guide_enabled=True,
        latest_guide_toggle_callback=noop_latest_guide_toggle,
    )

    assert "Disable Latest Guide" not in [child.label for child in team_view.children]
    assert "Disable Latest Guide" not in [child.label for child in shift_view.children]


@pytest.mark.asyncio
async def test_cog_setup_views_pass_latest_guide_refresh_callback_to_sheet_modals() -> (
    None
):
    team_manager = RecordingTeamRegisterManager()
    team_manager.config_exists = False
    team_view = TeamRegister(fake_bot())._build_initial_setup_view(  # noqa: SLF001
        team_manager
    )
    team_interaction = FakeInteraction()

    await child_with_label(team_view, "Set Up Team Register").callback(team_interaction)

    team_modal = team_interaction.response.modals[0]
    assert isinstance(team_modal, TeamRegisterSheetModal)
    assert team_modal.latest_guide_refresh_callback is not None

    shift_manager = RecordingShiftRegisterManager()
    shift_manager.config_exists = False
    shift_view = ShiftRegister(fake_bot())._build_initial_setup_view(  # noqa: SLF001
        shift_manager
    )
    shift_interaction = FakeInteraction()

    await child_with_label(shift_view, "Set Up Shift Register").callback(
        shift_interaction
    )

    shift_modal = shift_interaction.response.modals[0]
    assert isinstance(shift_modal, ShiftRegisterSheetModal)
    assert shift_modal.latest_guide_refresh_callback is not None


def test_configured_team_view_starts_with_enable_latest_guide_button() -> None:
    view = TeamRegisterView(
        RecordingTeamRegisterManager(),
        has_existing_settings=True,
        metadata=team_register_metadata(),
        latest_guide_enabled=False,
        latest_guide_toggle_callback=noop_latest_guide_toggle,
    )

    first_child = view.children[0]
    assert first_child.label == "Enable Latest Guide"
    assert first_child.style is ButtonStyle.primary
    assert "Edit Team Register Settings" in [child.label for child in view.children]
    assert "Edit Encore Roles" in [child.label for child in view.children]


def test_configured_shift_view_starts_with_disable_latest_guide_button() -> None:
    view = ShiftRegisterView(
        RecordingShiftRegisterManager(),
        has_existing_settings=True,
        latest_guide_enabled=True,
        latest_guide_toggle_callback=noop_latest_guide_toggle,
    )

    first_child = view.children[0]
    assert first_child.label == "Disable Latest Guide"
    assert first_child.style is ButtonStyle.secondary


@pytest.mark.asyncio
async def test_team_latest_guide_button_callback_has_view_manager() -> None:
    manager = RecordingTeamRegisterManager()
    calls: list[tuple[bool, object]] = []

    async def toggle_callback(
        _interaction: object,
        *,
        enabled: bool,
        current_view: object,
    ) -> None:
        calls.append((enabled, current_view.team_register_manager))

    view = TeamRegisterView(
        manager,
        has_existing_settings=True,
        metadata=team_register_metadata(),
        latest_guide_toggle_callback=toggle_callback,
    )

    await child_with_label(view, "Enable Latest Guide").callback(FakeInteraction())

    assert calls == [(True, manager)]


@pytest.mark.asyncio
async def test_shift_latest_guide_button_callback_has_view_manager() -> None:
    manager = RecordingShiftRegisterManager()
    calls: list[tuple[bool, object]] = []

    async def toggle_callback(
        _interaction: object,
        *,
        enabled: bool,
        current_view: object,
    ) -> None:
        calls.append((enabled, current_view.shift_register_manager))

    view = ShiftRegisterView(
        manager,
        has_existing_settings=True,
        latest_guide_toggle_callback=toggle_callback,
    )

    await child_with_label(view, "Enable Latest Guide").callback(FakeInteraction())

    assert calls == [(True, manager)]


def test_team_settings_embed_includes_latest_guide_status_after_encore_roles() -> None:
    embed = build_team_current_settings_embed(
        sheet_url="https://sheet.example",
        metadata=team_register_metadata(),
        encore_role_ids=[],
        color=0,
        latest_guide_enabled=True,
    )
    field_names = [field.name for field in embed.fields]

    assert field_names.index(LATEST_GUIDE_FIELD_NAME) == (
        field_names.index("Encore Roles") + 1
    )
    latest_guide_field = embed.fields[field_names.index(LATEST_GUIDE_FIELD_NAME)]
    assert latest_guide_field.value == (
        r"- \🟢 `Enabled` : A short guide is automatically kept near the newest "
        "messages. When a full guide announcement exists, the short guide replies "
        "to it."
    )


def test_shift_settings_embed_includes_latest_guide_status_before_timeline() -> None:
    manager = RecordingShiftRegisterManager()
    embed = build_shift_current_settings_embed(
        sheet_url=manager.sheet_url,
        metadata=manager.metadata,
        final_schedule_anchor_cell=manager.final_schedule_anchor_cell,
        shift_register=manager,
        color=0,
        latest_guide_enabled=False,
        team_source=manager.team_source,
    )
    field_names = [field.name for field in embed.fields]

    assert field_names.index(LATEST_GUIDE_FIELD_NAME) == (
        field_names.index("Final Schedule Anchor Cell") + 1
    )
    assert field_names.index("Shift Timeline") == (
        field_names.index(LATEST_GUIDE_FIELD_NAME) + 1
    )
    latest_guide_field = embed.fields[field_names.index(LATEST_GUIDE_FIELD_NAME)]
    assert latest_guide_field.value == (
        r"- \⚫ `Disabled` : No short guide is maintained near new messages. Enable "
        "this to keep registration rules visible as the channel moves."
    )


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (
            TeamSourceStatus.UNSET,
            "- No Team source is selected. Shift registrations will continue without "
            "Team references.",
        ),
        (
            TeamSourceStatus.MISSING,
            "- No configured Team Register exists in this server.",
        ),
        (
            TeamSourceStatus.AMBIGUOUS,
            "- Multiple Team Registers are configured. Use Edit Team Source "
            "to select one.",
        ),
        (
            TeamSourceStatus.INVALID,
            "- The configured Team source is invalid. Repair its worksheet "
            "settings or header.",
        ),
        (
            TeamSourceStatus.UNRESOLVED,
            "- The Team source could not be read at this time.",
        ),
    ],
)
def test_shift_settings_embed_formats_unavailable_team_source(
    status: TeamSourceStatus,
    expected: str,
) -> None:
    manager = RecordingShiftRegisterManager()

    embed = build_shift_current_settings_embed(
        sheet_url=manager.sheet_url,
        metadata=manager.metadata,
        final_schedule_anchor_cell=manager.final_schedule_anchor_cell,
        shift_register=manager,
        color=0,
        team_source=TeamSourceResolution(status),
    )

    field_map = {field.name: field.value for field in embed.fields}
    assert field_map["Team Source"] == expected


@pytest.mark.asyncio
async def test_shift_settings_panel_lists_unique_team_source() -> None:
    manager = RecordingShiftRegisterManager()
    manager.team_source = team_source_resolution()
    interaction = FakeInteraction()
    button = ShiftRegisterButton("Set Up Shift Register", manager)

    await button.callback(interaction)

    embed = interaction.followup.messages[0][1]["embed"]
    field_map = {field.name: field.value for field in embed.fields}
    field_names = [field.name for field in embed.fields]
    assert field_map["Team Source"] == (
        "- **Channel** = <#22>\n"
        "- **Google Sheet** = [Open Team Register Sheet]"
        "(https://team.sheet.example?gid=201#gid=201)"
    )
    assert field_names.index("Team Source") == (
        field_names.index("Worksheets & IDs") + 1
    )
    view = interaction.followup.messages[0][1]["view"]
    assert [getattr(child, "label", None) for child in view.children] == [
        "Edit Sheet Settings",
        "Edit Team Source",
        "Edit Shift Timeline",
        "Edit Recruitment Time Range",
    ]


@pytest.mark.asyncio
async def test_manage_team_source_button_rechecks_permissions() -> None:
    manager = RecordingShiftRegisterManager()
    button = ManageTeamSourceButton(manager)
    interaction = unauthorized_interaction()

    await button.callback(interaction)

    assert_permission_denied(interaction)


@pytest.mark.asyncio
async def test_edit_team_source_can_return_to_settings_without_saving() -> None:
    manager = RecordingShiftRegisterManager()
    view = ShiftRegisterView(manager, has_existing_settings=True)
    edit_button = child_with_label(view, "Edit Team Source")
    edit_interaction = FakeInteraction()

    await edit_button.callback(edit_interaction)

    assert edit_interaction.response.deferred == [False]
    _, edit_kwargs = edit_interaction.original_response_edits[-1]
    team_source_view = edit_kwargs["view"]
    assert isinstance(team_source_view, TeamSourceView)
    assert [child.label for child in team_source_view.children] == ["Back to Settings"]

    back_interaction = FakeInteraction()
    await child_with_label(team_source_view, "Back to Settings").callback(
        back_interaction
    )

    assert back_interaction.response.deferred == [False]
    _, back_kwargs = back_interaction.original_response_edits[-1]
    labels = [getattr(child, "label", None) for child in back_kwargs["view"].children]
    assert labels == [
        "Edit Sheet Settings",
        "Edit Team Source",
        "Edit Shift Timeline",
        "Edit Recruitment Time Range",
    ]
    assert manager.team_source_apply_calls == []


@pytest.mark.asyncio
async def test_apply_team_source_repairs_selected_channel() -> None:
    manager = RecordingShiftRegisterManager()
    manager.team_source = team_source_resolution()
    view = TeamSourceView(manager, selected_channel_id=22)
    button = next(
        child for child in view.children if isinstance(child, ApplyTeamSourceButton)
    )
    interaction = FakeInteraction()

    await button.callback(interaction)

    assert manager.team_source_apply_calls == [22]
    assert interaction.followup.messages[-1][0] == (
        "✅ Team source saved and references repaired."
    )


def test_shift_settings_embed_uses_team_landing_worksheet() -> None:
    manager = RecordingShiftRegisterManager()

    embed = build_shift_current_settings_embed(
        sheet_url=manager.sheet_url,
        metadata=manager.metadata,
        final_schedule_anchor_cell=manager.final_schedule_anchor_cell,
        shift_register=manager,
        color=0,
        team_source=team_source_resolution(landing_worksheet_id=101),
    )

    field_map = {field.name: field.value for field in embed.fields}
    assert field_map["Team Source"] == (
        "- **Channel** = <#22>\n"
        "- **Google Sheet** = [Open Team Register Sheet]"
        "(https://team.sheet.example?gid=101#gid=101)"
    )


@pytest.mark.asyncio
async def test_latest_guide_button_denies_unauthorized_user() -> None:
    calls: list[tuple[object, bool, object]] = []

    async def toggle_callback(
        interaction: object,
        *,
        enabled: bool,
        current_view: object,
    ) -> None:
        calls.append((interaction, enabled, current_view))

    interaction = unauthorized_interaction()
    button = LatestGuideButton(enabled=False, toggle_callback=toggle_callback)

    await button.callback(interaction)

    assert_permission_denied(interaction)
    assert calls == []


@pytest.mark.asyncio
async def test_latest_guide_button_defers_before_toggle_work() -> None:
    async def toggle_callback(
        interaction: object,
        *,
        enabled: bool,
        current_view: object,
    ) -> None:
        assert interaction.response.is_done()
        assert enabled is True
        assert current_view is button.view

    interaction = FakeInteraction()
    button = LatestGuideButton(enabled=False, toggle_callback=toggle_callback)

    await button.callback(interaction)

    assert interaction.response.deferred == [False]


def test_auto_guide_button_language_uses_first_supported_language() -> None:
    assert auto_guide_button_language(["zh_tw", "ja", "en"]) == "zh_tw"
    assert auto_guide_button_language(["ja", "en"]) == "ja"
    assert auto_guide_button_language(["unsupported", "en"]) == "en"
    assert auto_guide_button_language([]) == "en"


def test_auto_guide_delete_custom_id_includes_feature_name() -> None:
    assert (
        auto_guide_delete_custom_id("team_register")
        == f"{AUTO_GUIDE_DELETE_CUSTOM_ID_PREFIX}team_register"
    )


def test_discord_message_url_uses_guild_channel_and_message_id() -> None:
    assert (
        discord_message_url(guild_id=111, channel_id=222, message_id=333)
        == "https://discord.com/channels/111/222/333"
    )


@pytest.mark.asyncio
async def test_auto_guide_buttons_view_builds_team_reply_buttons() -> None:
    calls: list[object] = []

    async def delete_callback(interaction: object) -> None:
        calls.append(interaction)

    view = AutoGuideButtonsView(
        feature_name="team_register",
        language="zh_tw",
        delete_callback=delete_callback,
        sheet_url="https://sheet.example/#gid=1",
        full_guide_url="https://discord.com/channels/111/222/333",
    )

    assert [child.label for child in view.children] == [
        "刪除我的編成",
        "完整說明",
        "Google Sheets",
    ]
    assert [str(child.emoji) for child in view.children] == ["🗑️", "⤴️", "👀"]
    assert view.children[0].style is ButtonStyle.danger
    assert view.children[0].custom_id == ("rhoboto:auto_guide:delete:team_register")
    assert view.children[1].style is ButtonStyle.link
    assert view.children[1].url == "https://discord.com/channels/111/222/333"
    assert view.children[2].style is ButtonStyle.link
    assert view.children[2].url == "https://sheet.example/#gid=1"
    assert view.timeout == AUTO_GUIDE_BUTTON_VIEW_TIMEOUT_SECONDS
    assert not view.is_persistent()

    interaction = FakeInteraction()
    await view.children[0].callback(interaction)
    assert calls == [interaction]


def test_auto_guide_buttons_view_omits_full_guide_without_url() -> None:
    async def delete_callback(_interaction: object) -> None:
        return None

    view = AutoGuideButtonsView(
        feature_name="shift_register",
        language="ja",
        delete_callback=delete_callback,
        sheet_url="https://sheet.example/#gid=2",
    )

    assert [child.label for child in view.children] == [
        "自分のシフトを削除",
        "Google Sheets",
    ]
    assert [str(child.emoji) for child in view.children] == ["🗑️", "👀"]
    assert view.children[0].style is ButtonStyle.danger
    assert view.children[1].style is ButtonStyle.link


def test_auto_guide_buttons_view_delete_only_is_persistent() -> None:
    async def delete_callback(_interaction: object) -> None:
        return None

    view = AutoGuideButtonsView(
        feature_name="team_register",
        language="en",
        delete_callback=delete_callback,
        sheet_url=None,
        delete_only=True,
        timeout=None,
    )

    assert len(view.children) == 1
    assert view.children[0].label == "Delete Your Teams"
    assert view.children[0].custom_id == ("rhoboto:auto_guide:delete:team_register")
    assert view.is_persistent()


@pytest.mark.asyncio
async def test_team_initial_setup_save_shows_disabled_latest_guide_controls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    subject = TeamRegister(fake_bot())
    monkeypatch.setattr(
        subject,
        "_auto_guide_is_enabled",
        latest_guide_disabled_for_feature,
    )
    monkeypatch.setattr(
        subject,
        "_refresh_auto_guide_if_enabled",
        noop_latest_guide_refresh,
    )
    manager = RecordingTeamRegisterManager()
    manager.config_exists = False
    view = subject._build_initial_setup_view(manager)  # noqa: SLF001

    assert [child.label for child in view.children] == ["Set Up Team Register"]
    setup_interaction = FakeInteraction()
    await child_with_label(view, "Set Up Team Register").callback(setup_interaction)
    modal = setup_interaction.response.modals[0]
    modal.sheet_url._value = TEAM_SETTINGS_SHEET_URL  # noqa: SLF001

    save_interaction = FakeInteraction()
    await modal.on_submit(save_interaction)

    assert len(save_interaction.followup.messages) == 1
    assert_latest_guide_disabled_panel(save_interaction.followup.messages[0][1])


@pytest.mark.asyncio
async def test_shift_initial_setup_save_shows_disabled_latest_guide_controls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    subject = ShiftRegister(fake_bot())
    monkeypatch.setattr(
        subject,
        "_auto_guide_is_enabled",
        latest_guide_disabled_for_feature,
    )
    monkeypatch.setattr(
        subject,
        "_refresh_auto_guide_if_enabled",
        noop_latest_guide_refresh,
    )
    manager = RecordingShiftRegisterManager()
    manager.config_exists = False
    view = subject._build_initial_setup_view(manager)  # noqa: SLF001

    assert [child.label for child in view.children] == ["Set Up Shift Register"]
    setup_interaction = FakeInteraction()
    await child_with_label(view, "Set Up Shift Register").callback(setup_interaction)
    modal = setup_interaction.response.modals[0]

    save_interaction = FakeInteraction()
    await modal.on_submit(save_interaction)

    assert len(save_interaction.followup.messages) == 1
    _, kwargs = save_interaction.followup.messages[0]
    team_source_view = kwargs["view"]
    assert kwargs["embed"] is None
    assert isinstance(team_source_view, TeamSourceView)
    assert team_source_view.latest_guide_toggle_callback is not None
    assert [child.label for child in team_source_view.children] == ["Set Later"]

    return_interaction = FakeInteraction()
    await child_with_label(team_source_view, "Set Later").callback(return_interaction)

    assert return_interaction.response.deferred == [False]
    assert_latest_guide_disabled_panel(
        return_interaction.original_response_edits[-1][1]
    )


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
    modal = TeamRegisterSheetModal(manager, sheet_url=TEAM_SETTINGS_SHEET_URL)

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
        sheet_url=TEAM_SETTINGS_SHEET_URL,
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
async def test_team_modal_holds_shared_channel_lock_during_integrated_save(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = RecordingTeamRegisterManager()
    interaction = FakeInteraction()
    lock = RecordingAsyncLock()
    monkeypatch.setattr(
        ui_team_register,
        "TEAM_REGISTER_SHEET_WRITE_LOCK",
        lock,
    )
    modal = TeamRegisterSheetModal(
        manager,
        sheet_url=TEAM_SETTINGS_SHEET_URL,
        team_worksheet_titles=["Team 1"],
        summary_worksheet_title="Summary",
    )

    await modal.on_submit(interaction)

    assert lock.keys == [222]
    assert (lock.entered, lock.exited) == (1, 1)


@pytest.mark.asyncio
async def test_team_edit_modal_save_preserves_latest_guide_controls() -> None:
    manager = RecordingTeamRegisterManager()
    interaction = FakeInteraction()
    modal = TeamRegisterSheetModal(
        manager,
        sheet_url=TEAM_SETTINGS_SHEET_URL,
        team_worksheet_titles=["Team 1"],
        summary_worksheet_title="Summary",
        requires_existing_settings=True,
        latest_guide_enabled=True,
        latest_guide_toggle_callback=noop_latest_guide_toggle,
    )

    await modal.on_submit(interaction)

    assert len(interaction.followup.messages) == 1
    assert_latest_guide_enabled_panel(
        interaction.followup.messages[0][1],
        toggle_callback=noop_latest_guide_toggle,
    )


@pytest.mark.asyncio
async def test_team_edit_modal_save_refreshes_latest_guide_state() -> None:
    manager = RecordingTeamRegisterManager()
    interaction = FakeInteraction()
    modal = TeamRegisterSheetModal(
        manager,
        sheet_url=TEAM_SETTINGS_SHEET_URL,
        team_worksheet_titles=["Team 1"],
        summary_worksheet_title="Summary",
        requires_existing_settings=True,
        latest_guide_enabled=False,
        latest_guide_toggle_callback=noop_latest_guide_toggle,
        latest_guide_state_resolver=latest_guide_is_enabled,
    )

    await modal.on_submit(interaction)

    assert len(interaction.followup.messages) == 1
    assert_latest_guide_enabled_panel(
        interaction.followup.messages[0][1],
        toggle_callback=noop_latest_guide_toggle,
    )


@pytest.mark.asyncio
async def test_team_modal_save_calls_latest_guide_refresh_after_panel_refresh() -> None:
    manager = RecordingTeamRegisterManager()
    interaction = FakeInteraction()
    refresh_callback = RecordingLatestGuideRefreshCallback()
    modal = TeamRegisterSheetModal(
        manager,
        sheet_url=TEAM_SETTINGS_SHEET_URL,
        team_worksheet_titles=["Team 1"],
        summary_worksheet_title="Summary",
        latest_guide_refresh_callback=refresh_callback,
    )

    await modal.on_submit(interaction)

    assert len(interaction.followup.messages) == 1
    assert len(refresh_callback.calls) == 1
    call_interaction, feature_config, followup_count = refresh_callback.calls[0]
    assert call_interaction is interaction
    assert feature_config.sheet_url == TEAM_SETTINGS_SHEET_URL
    assert followup_count == 1


@pytest.mark.asyncio
async def test_team_modal_save_warns_when_latest_guide_refresh_fails() -> None:
    manager = RecordingTeamRegisterManager()
    interaction = FakeInteraction()
    refresh_callback = RecordingLatestGuideRefreshCallback(result=False)
    modal = TeamRegisterSheetModal(
        manager,
        sheet_url=TEAM_SETTINGS_SHEET_URL,
        team_worksheet_titles=["Team 1"],
        summary_worksheet_title="Summary",
        latest_guide_refresh_callback=refresh_callback,
    )

    await modal.on_submit(interaction)

    assert len(refresh_callback.calls) == 1
    assert len(interaction.followup.messages) == 2
    _, panel_kwargs = interaction.followup.messages[0]
    assert panel_kwargs["embed"].title == "Team Register Settings Saved"
    assert interaction.followup.messages[1] == (
        LATEST_GUIDE_SETTINGS_REFRESH_FAILED_WARNING,
        {"ephemeral": True},
    )


@pytest.mark.asyncio
async def test_team_setup_modal_submit_can_create_missing_settings() -> None:
    manager = RecordingTeamRegisterManager()
    manager.config_exists = False
    interaction = FakeInteraction()
    modal = TeamRegisterSheetModal(
        manager,
        sheet_url=TEAM_SETTINGS_SHEET_URL,
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
        sheet_url=TEAM_SETTINGS_SHEET_URL,
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
        sheet_url=TEAM_SETTINGS_SHEET_URL,
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
async def test_team_setup_modal_reports_storage_error_before_sheet_save() -> None:
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
    assert "Some changes may have been saved" not in content
    assert "Reference: `STG-" in content
    assert_no_private_storage_terms(content)
    assert kwargs == {"ephemeral": True}
    assert "private.sheet.example" not in str(interaction.followup.messages)


@pytest.mark.asyncio
async def test_team_setup_modal_reports_contract_error_without_partial_success() -> (
    None
):
    manager = RecordingTeamRegisterManager()
    contract_error = WorksheetContractError(log_hint="required_header_duplicate")
    manager.upsert_error = contract_error
    interaction = FakeInteraction()
    modal = TeamRegisterSheetModal(
        manager,
        sheet_url=TEAM_SETTINGS_SHEET_URL,
        team_worksheet_titles=["Team 1"],
        summary_worksheet_title="Summary",
    )

    assert not isinstance(contract_error, SETTINGS_STORAGE_EXCEPTIONS)

    await modal.on_submit(interaction)

    assert interaction.response.deferred == [True]
    assert len(interaction.followup.messages) == 1
    content, kwargs = interaction.followup.messages[0]
    assert content is not None
    assert "The configured Google Sheet could not be processed" in content
    assert "Some changes may have been saved" not in content
    assert "Reference: `WSC-" in content
    assert kwargs == {"ephemeral": True}


@pytest.mark.asyncio
async def test_team_setup_modal_reports_storage_error_when_initial_save_fails() -> None:
    manager = RecordingTeamRegisterManager()
    manager.upsert_error = DBConnectionError("private database host")
    interaction = FakeInteraction()
    modal = TeamRegisterSheetModal(
        manager,
        sheet_url=TEAM_SETTINGS_SHEET_URL,
        team_worksheet_titles=["Team 1"],
        summary_worksheet_title="Summary",
    )

    await modal.on_submit(interaction)

    assert interaction.response.deferred == [True]
    assert len(interaction.followup.messages) == 1
    content, kwargs = interaction.followup.messages[0]
    assert content is not None
    assert "Some changes may have been saved" not in content
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
        sheet_url=TEAM_SETTINGS_SHEET_URL,
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
async def test_edit_encore_roles_too_many_refreshes_latest_guide_state() -> None:
    manager = RecordingTeamRegisterManager()
    roles = [FakeRole(id=i, name=f"Role {i}", position=i) for i in range(1, 27)]
    manager.encore_role_ids = [role.id for role in roles]
    interaction = FakeInteraction(roles=roles)
    view = TeamRegisterView(
        manager,
        has_existing_settings=True,
        metadata=team_register_metadata(),
        roles=roles,
        encore_role_ids=manager.encore_role_ids,
        latest_guide_enabled=False,
        latest_guide_toggle_callback=noop_latest_guide_toggle,
        latest_guide_state_resolver=latest_guide_is_enabled,
    )

    await child_with_label(view, "Edit Encore Roles").callback(interaction)

    updated_view = interaction.response.edits[0][1]["view"]
    assert updated_view.latest_guide_enabled is True
    assert updated_view.children[0].label == "Disable Latest Guide"
    assert updated_view.children[0].style is ButtonStyle.secondary
    assert updated_view.children[0].toggle_callback is noop_latest_guide_toggle
    field_map = {
        field.name: field.value
        for field in interaction.response.edits[0][1]["embed"].fields
    }
    assert field_map[LATEST_GUIDE_FIELD_NAME] == (
        r"- \🟢 `Enabled` : A short guide is automatically kept near the newest "
        "messages. When a full guide announcement exists, the short guide replies "
        "to it."
    )


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
    assert interaction.response.deferred == [True]
    assert len(interaction.original_response_edits) == 1


@pytest.mark.asyncio
async def test_encore_role_confirm_holds_fresh_team_transaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = RecordingTeamRegisterManager()
    role = FakeRole(id=1, name="Encore", position=10)
    member = SimpleNamespace(name="alice", display_name="Alice", roles=[role])
    interaction = FakeInteraction(
        guild=SimpleNamespace(id=111, roles=[role], members=[member])
    )

    class DeferredRecordingAsyncLock(RecordingAsyncLock):
        async def __aenter__(self) -> None:
            assert interaction.response.deferred == [True]
            await super().__aenter__()

    channel_lock = DeferredRecordingAsyncLock()
    spreadsheet_lock = RecordingAsyncLock()
    monkeypatch.setattr(
        ui_team_register,
        "TEAM_REGISTER_SHEET_WRITE_LOCK",
        channel_lock,
    )
    monkeypatch.setattr(
        team_register_manager_module,
        "SPREADSHEET_TRANSACTION_LOCK",
        spreadsheet_lock,
        raising=False,
    )
    view = EncoreRolePreviewView(
        manager,
        selected_roles=[role],
        retained_missing_role_ids=[],
        metadata=team_register_metadata(),
    )

    await child_with_label(view, "Confirm Save").callback(interaction)

    assert channel_lock.keys == [222]
    assert spreadsheet_lock.keys == ["team-encore"]
    assert manager.encore_reconciliation_calls == [
        {
            "role_ids": [1],
            "member_by_names": {"alice": member},
        }
    ]
    assert interaction.response.deferred == [True]
    assert interaction.response.edits == []
    assert len(interaction.original_response_edits) == 1


@pytest.mark.asyncio
async def test_encore_role_confirm_reports_contract_error_before_save() -> None:
    manager = RecordingTeamRegisterManager()
    manager.save_error = WorksheetContractError(log_hint="invalid_worksheet_contract")
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
    assert interaction.response.deferred == [True]
    assert len(interaction.followup.messages) == 1
    content, kwargs = interaction.followup.messages[0]
    assert content is not None
    assert "The configured Google Sheet could not be processed" in content
    assert "Some changes may have been saved" not in content
    assert "Reference: `WSC-" in content
    assert kwargs == {"ephemeral": True}


@pytest.mark.asyncio
async def test_encore_role_confirm_does_not_call_latest_guide_refresh() -> None:
    manager = RecordingTeamRegisterManager()
    role = FakeRole(id=1, name="Encore", position=10)
    interaction = FakeInteraction(roles=[role])
    refresh_callback = RecordingLatestGuideRefreshCallback()
    view = EncoreRolePreviewView(
        manager,
        selected_roles=[role],
        retained_missing_role_ids=[],
        metadata=team_register_metadata(),
        latest_guide_refresh_callback=refresh_callback,
    )

    await child_with_label(view, "Confirm Save").callback(interaction)

    assert manager.encore_role_id_updates == [[1]]
    assert refresh_callback.calls == []


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

    updated_view = interaction.original_response_edits[0][1]["view"]
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

    content, edit_kwargs = interaction.original_response_edits[0]
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
    assert len(interaction.original_response_edits) == 1
    content, edit_kwargs = interaction.original_response_edits[0]
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

    content, _edit_kwargs = interaction.original_response_edits[0]
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
    assert interaction.response.deferred == [True]
    assert len(interaction.followup.messages) == 1
    content, kwargs = interaction.followup.messages[0]
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
    assert interaction.response.deferred == [True]
    assert interaction.followup.messages == [
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
        "- **Day Number** = `2`\n"
        "- **Event Date** = `2026-08-12`\n"
        "- **Submission Deadline** = `2026-08-12 21:00 JST`\n"
        "- **Draft Shift Proposal** = `2026-08-13 20:00 JST`\n"
        "- **Final Shift Notice** = `2026-08-14 18:00 JST`"
    )
    assert field_map["Recruitment Time Range"] == "- `4-28`"
    assert [child.label for child in kwargs["view"].children] == [
        "Edit Sheet Settings",
        "Edit Team Source",
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
async def test_shift_modal_submit_allows_authorized_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = RecordingShiftRegisterManager()
    sheet_lock = RecordingAsyncLock()
    monkeypatch.setattr(
        ui_shift_register,
        "SHIFT_REGISTER_SHEET_WRITE_LOCK",
        sheet_lock,
    )
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
    assert sheet_lock.keys == [222]
    assert (sheet_lock.entered, sheet_lock.exited) == (1, 1)
    assert len(manager.upsert_calls) == 1
    assert manager.anchor_updates == ["B2"]
    assert len(interaction.followup.messages) == 1
    content, kwargs = interaction.followup.messages[0]
    assert content == (
        "⚠️ No Team Register is configured in this server. "
        "Shift registrations will continue without Team references."
    )
    assert kwargs["embed"] is None
    assert [child.label for child in kwargs["view"].children] == ["Set Later"]
    assert kwargs["wait"] is True
    assert kwargs["view"].message is interaction.followup.sent_message_objects[0]


@pytest.mark.asyncio
async def test_shift_initial_setup_without_team_source_offers_set_later() -> None:
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
    )

    await modal.on_submit(interaction)

    content, kwargs = interaction.followup.messages[-1]
    assert content == (
        "⚠️ No Team Register is configured in this server. "
        "Shift registrations will continue without Team references."
    )
    assert kwargs["embed"] is None
    assert [child.label for child in kwargs["view"].children] == ["Set Later"]


@pytest.mark.asyncio
async def test_shift_initial_setup_multiple_sources_keeps_channel_select() -> None:
    manager = RecordingShiftRegisterManager()
    manager.config_exists = False
    manager.team_source_candidate_channel_ids = (22, 33)
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

    _, kwargs = interaction.followup.messages[-1]
    view = kwargs["view"]
    assert isinstance(view, TeamSourceView)
    assert view.selected_channel_id is None
    assert any(isinstance(child, TeamSourceSelect) for child in view.children)
    assert [getattr(child, "label", None) for child in view.children] == [
        None,
        "Apply & Repair",
        "Set Later",
    ]


@pytest.mark.asyncio
async def test_shift_initial_setup_with_one_team_source_preselects_channel() -> None:
    manager = RecordingShiftRegisterManager()
    manager.config_exists = False
    manager.team_source_candidate_channel_ids = (22,)
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

    _, kwargs = interaction.followup.messages[-1]
    view = kwargs["view"]
    assert isinstance(view, TeamSourceView)
    assert view.selected_channel_id == 22
    select = next(
        child for child in view.children if isinstance(child, TeamSourceSelect)
    )
    assert select.default_values[0].id == 22
    assert manager.team_source_apply_calls == []


@pytest.mark.asyncio
async def test_apply_team_source_preserves_latest_guide_control() -> None:
    manager = RecordingShiftRegisterManager()
    manager.team_source = team_source_resolution()
    view = TeamSourceView(
        manager,
        selected_channel_id=22,
        latest_guide_enabled=True,
        latest_guide_toggle_callback=noop_latest_guide_toggle,
    )
    button = next(
        child for child in view.children if isinstance(child, ApplyTeamSourceButton)
    )
    interaction = FakeInteraction()

    await button.callback(interaction)

    panel_view = interaction.followup.messages[-1][1]["view"]
    assert [getattr(child, "label", None) for child in panel_view.children] == [
        "Disable Latest Guide",
        "Edit Sheet Settings",
        "Edit Team Source",
        "Edit Shift Timeline",
        "Edit Recruitment Time Range",
    ]


@pytest.mark.asyncio
async def test_shift_edit_modal_save_preserves_latest_guide_controls() -> None:
    manager = RecordingShiftRegisterManager()
    interaction = FakeInteraction()
    modal = ShiftRegisterSheetModal(
        manager,
        sheet_url="https://sheet.example",
        entry_worksheet_title="Entry",
        draft_worksheet_title="Draft",
        final_schedule_worksheet_title="Final",
        final_schedule_anchor_cell="B2",
        requires_existing_settings=True,
        latest_guide_enabled=True,
        latest_guide_toggle_callback=noop_latest_guide_toggle,
    )

    await modal.on_submit(interaction)

    assert len(interaction.followup.messages) == 1
    assert_latest_guide_enabled_panel(
        interaction.followup.messages[0][1],
        toggle_callback=noop_latest_guide_toggle,
    )


@pytest.mark.asyncio
async def test_shift_edit_modal_save_refreshes_latest_guide_state() -> None:
    manager = RecordingShiftRegisterManager()
    interaction = FakeInteraction()
    modal = ShiftRegisterSheetModal(
        manager,
        sheet_url="https://sheet.example",
        entry_worksheet_title="Entry",
        draft_worksheet_title="Draft",
        final_schedule_worksheet_title="Final",
        final_schedule_anchor_cell="B2",
        requires_existing_settings=True,
        latest_guide_enabled=False,
        latest_guide_toggle_callback=noop_latest_guide_toggle,
        latest_guide_state_resolver=latest_guide_is_enabled,
    )

    await modal.on_submit(interaction)

    assert len(interaction.followup.messages) == 1
    assert_latest_guide_enabled_panel(
        interaction.followup.messages[0][1],
        toggle_callback=noop_latest_guide_toggle,
    )


@pytest.mark.asyncio
async def test_shift_modal_save_calls_latest_guide_refresh_after_panel_refresh() -> (
    None
):
    manager = RecordingShiftRegisterManager()
    interaction = FakeInteraction()
    refresh_callback = RecordingLatestGuideRefreshCallback()
    modal = ShiftRegisterSheetModal(
        manager,
        sheet_url="https://sheet.example",
        entry_worksheet_title="Entry",
        draft_worksheet_title="Draft",
        final_schedule_worksheet_title="Final",
        final_schedule_anchor_cell="C3",
        requires_existing_settings=True,
        latest_guide_refresh_callback=refresh_callback,
    )

    await modal.on_submit(interaction)

    assert manager.anchor_updates == ["C3"]
    assert len(interaction.followup.messages) == 1
    assert len(refresh_callback.calls) == 1
    call_interaction, feature_config, followup_count = refresh_callback.calls[0]
    assert call_interaction is interaction
    assert feature_config.final_schedule_anchor_cell == "C3"
    assert followup_count == 1


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
async def test_shift_setup_modal_reports_contract_error_without_partial_success() -> (
    None
):
    manager = RecordingShiftRegisterManager()
    contract_error = WorksheetContractError(log_hint="required_header_missing")
    manager.upsert_error = contract_error
    interaction = FakeInteraction()
    modal = ShiftRegisterSheetModal(
        manager,
        sheet_url="https://sheet.example",
        entry_worksheet_title="Entry",
        draft_worksheet_title="Draft",
        final_schedule_worksheet_title="Final",
        final_schedule_anchor_cell="B2",
    )

    assert not isinstance(contract_error, SETTINGS_STORAGE_EXCEPTIONS)

    await modal.on_submit(interaction)

    assert interaction.response.deferred == [True]
    assert manager.anchor_updates == []
    assert len(interaction.followup.messages) == 1
    content, kwargs = interaction.followup.messages[0]
    assert content is not None
    assert "The configured Google Sheet could not be processed" in content
    assert "Some changes may have been saved" not in content
    assert "Reference: `WSC-" in content
    assert kwargs == {"ephemeral": True}


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
async def test_shift_timeline_modal_save_preserves_latest_guide_controls() -> None:
    manager = RecordingShiftRegisterManager()
    interaction = FakeInteraction()
    modal = ShiftTimelineModal(
        manager,
        day_number="3",
        event_date="2026-08-12",
        submission_deadline_at="8/12 21",
        draft_shift_proposal_at="2026/08/13 20",
        final_shift_notice_at="2026-08-14 18",
        latest_guide_enabled=True,
        latest_guide_toggle_callback=noop_latest_guide_toggle,
    )

    await modal.on_submit(interaction)

    assert len(interaction.followup.messages) == 1
    assert_latest_guide_enabled_panel(
        interaction.followup.messages[0][1],
        toggle_callback=noop_latest_guide_toggle,
    )


@pytest.mark.asyncio
async def test_shift_timeline_modal_save_refreshes_latest_guide_state() -> None:
    manager = RecordingShiftRegisterManager()
    interaction = FakeInteraction()
    modal = ShiftTimelineModal(
        manager,
        day_number="3",
        event_date="2026-08-12",
        submission_deadline_at="8/12 21",
        draft_shift_proposal_at="2026/08/13 20",
        final_shift_notice_at="2026-08-14 18",
        latest_guide_enabled=False,
        latest_guide_toggle_callback=noop_latest_guide_toggle,
        latest_guide_state_resolver=latest_guide_is_enabled,
    )

    await modal.on_submit(interaction)

    assert len(interaction.followup.messages) == 1
    assert_latest_guide_enabled_panel(
        interaction.followup.messages[0][1],
        toggle_callback=noop_latest_guide_toggle,
    )


@pytest.mark.asyncio
async def test_shift_timeline_save_calls_latest_guide_refresh_after_panel_refresh() -> (
    None
):
    manager = RecordingShiftRegisterManager()
    interaction = FakeInteraction()
    refresh_callback = RecordingLatestGuideRefreshCallback()
    modal = ShiftTimelineModal(
        manager,
        day_number="3",
        event_date="2026-08-12",
        submission_deadline_at="8/12 21",
        draft_shift_proposal_at="2026/08/13 20",
        final_shift_notice_at="2026-08-14 18",
        latest_guide_refresh_callback=refresh_callback,
    )

    await modal.on_submit(interaction)

    assert len(interaction.followup.messages) == 1
    assert len(refresh_callback.calls) == 1
    call_interaction, feature_config, followup_count = refresh_callback.calls[0]
    assert call_interaction is interaction
    assert feature_config.day_number == 3
    assert followup_count == 1


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
async def test_shift_timeline_panel_refresh_failure_skips_latest_guide_refresh() -> (
    None
):
    manager = RecordingShiftRegisterManager()
    manager.metadata_error = GoogleSheetsError(
        GoogleSheetsErrorKind.PERMISSION,
        "Check the sheet sharing settings and service account access.",
    )
    interaction = FakeInteraction()
    refresh_callback = RecordingLatestGuideRefreshCallback()
    modal = ShiftTimelineModal(
        manager,
        day_number="3",
        event_date="2026-08-12",
        submission_deadline_at="8/12 21",
        draft_shift_proposal_at="",
        final_shift_notice_at="",
        latest_guide_refresh_callback=refresh_callback,
    )

    await modal.on_submit(interaction)

    assert refresh_callback.calls == []
    assert len(interaction.followup.messages) == 1
    content, kwargs = interaction.followup.messages[0]
    assert content is not None
    assert "settings view could not be refreshed" in content
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
async def test_shift_recruitment_range_modal_submit_updates_range(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = RecordingShiftRegisterManager()
    sheet_lock = RecordingAsyncLock()
    monkeypatch.setattr(
        ui_shift_register,
        "SHIFT_REGISTER_SHEET_WRITE_LOCK",
        sheet_lock,
    )
    interaction = FakeInteraction()
    modal = ShiftRecruitmentRangeModal(manager, recruitment_time_range="4-8, 8-12")

    await modal.on_submit(interaction)

    assert interaction.response.deferred == [True]
    assert sheet_lock.keys == [222]
    assert (sheet_lock.entered, sheet_lock.exited) == (1, 1)
    assert [ranges.to_json() for ranges in manager.recruitment_range_updates] == [
        [{"start": 4, "end": 12}]
    ]
    assert len(interaction.followup.messages) == 1
    _, kwargs = interaction.followup.messages[0]
    assert kwargs["embed"].title == "Shift Register Settings Saved"


@pytest.mark.asyncio
async def test_shift_recruitment_range_save_preserves_latest_guide_controls() -> None:
    manager = RecordingShiftRegisterManager()
    interaction = FakeInteraction()
    modal = ShiftRecruitmentRangeModal(
        manager,
        recruitment_time_range="4-8, 8-12",
        latest_guide_enabled=True,
        latest_guide_toggle_callback=noop_latest_guide_toggle,
    )

    await modal.on_submit(interaction)

    assert len(interaction.followup.messages) == 1
    assert_latest_guide_enabled_panel(
        interaction.followup.messages[0][1],
        toggle_callback=noop_latest_guide_toggle,
    )


@pytest.mark.asyncio
async def test_shift_recruitment_range_save_refreshes_latest_guide_state() -> None:
    manager = RecordingShiftRegisterManager()
    interaction = FakeInteraction()
    modal = ShiftRecruitmentRangeModal(
        manager,
        recruitment_time_range="4-8, 8-12",
        latest_guide_enabled=False,
        latest_guide_toggle_callback=noop_latest_guide_toggle,
        latest_guide_state_resolver=latest_guide_is_enabled,
    )

    await modal.on_submit(interaction)

    assert len(interaction.followup.messages) == 1
    assert_latest_guide_enabled_panel(
        interaction.followup.messages[0][1],
        toggle_callback=noop_latest_guide_toggle,
    )


@pytest.mark.asyncio
async def test_shift_range_save_calls_latest_guide_refresh_after_panel_refresh() -> (
    None
):
    manager = RecordingShiftRegisterManager()
    interaction = FakeInteraction()
    refresh_callback = RecordingLatestGuideRefreshCallback()
    modal = ShiftRecruitmentRangeModal(
        manager,
        recruitment_time_range="4-8, 8-12",
        latest_guide_refresh_callback=refresh_callback,
    )

    await modal.on_submit(interaction)

    assert len(interaction.followup.messages) == 1
    assert len(refresh_callback.calls) == 1
    call_interaction, feature_config, followup_count = refresh_callback.calls[0]
    assert call_interaction is interaction
    assert feature_config.recruitment_time_ranges == [{"start": 4, "end": 12}]
    assert followup_count == 1


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
@pytest.mark.parametrize("label", ["確認生成", "取消"])
async def test_generate_draft_confirm_rejects_other_user(label: str) -> None:
    view = GenerateDraftConfirmView(
        requesting_user_id=333,
        draft_sheet_url="https://sheet.example#gid=222",
    )
    interaction = FakeInteraction(user_id=444)

    await child_with_label(view, label).callback(interaction)

    assert view.value is None
    assert not view.is_finished()
    assert interaction.response.messages == [
        ("⚠️ 只有執行此 command 的管理員可以操作。", {"ephemeral": True})
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("label", ["確認生成", "取消"])
async def test_generate_draft_confirm_stops_after_permission_loss(label: str) -> None:
    view = GenerateDraftConfirmView(
        requesting_user_id=333,
        draft_sheet_url="https://sheet.example#gid=222",
    )
    interaction = FakeInteraction(user_id=333, manage_channels=False)

    await child_with_label(view, label).callback(interaction)

    assert view.value is False
    assert view.is_finished()
    assert interaction.response.messages == [
        (MISSING_SETTINGS_PERMISSION_MESSAGE, {"ephemeral": True})
    ]


@pytest.mark.asyncio
async def test_generate_draft_confirm_allows_requester() -> None:
    view = GenerateDraftConfirmView(
        requesting_user_id=333,
        draft_sheet_url="https://sheet.example#gid=222",
    )
    interaction = FakeInteraction(user_id=333)

    await child_with_label(view, "確認生成").callback(interaction)

    assert view.value is True
    assert view.is_finished()
    assert interaction.response.edits == [
        (
            "已確認生成，正在處理 "  # noqa: RUF001
            "[Shift Draft](https://sheet.example#gid=222)。",
            {"view": None},
        )
    ]


@pytest.mark.asyncio
async def test_generate_draft_cancel_allows_requester() -> None:
    view = GenerateDraftConfirmView(
        requesting_user_id=333,
        draft_sheet_url="https://sheet.example#gid=222",
    )
    interaction = FakeInteraction(user_id=333)

    await child_with_label(view, "取消").callback(interaction)

    assert view.value is False
    assert view.is_finished()
    assert interaction.response.edits == [
        ("✖️ 已取消生成，未變更 Shift Draft。", {"view": None})  # noqa: RUF001
    ]


@pytest.mark.asyncio
async def test_delete_confirm_allows_requesting_user() -> None:
    interaction = FakeInteraction(user_id=333)
    view = ConfirmDeleteUserDataView(
        requesting_user_id=333,
        confirm_label="Confirm",
        cancel_label="Cancel",
        in_progress_message="processing",
        cancelled_message="cancelled",
        unauthorized_message="unauthorized",
    )

    await child_with_label(view, "Confirm").callback(interaction)

    assert view.value is True
    assert view.is_finished()
    assert interaction.response.edits == [("processing", {"view": None})]


@pytest.mark.asyncio
async def test_delete_confirm_rejects_other_user_without_finishing() -> None:
    interaction = FakeInteraction(user_id=444)
    view = ConfirmDeleteUserDataView(
        requesting_user_id=333,
        confirm_label="Confirm",
        cancel_label="Cancel",
        in_progress_message="processing",
        cancelled_message="cancelled",
        unauthorized_message="unauthorized",
    )

    await child_with_label(view, "Confirm").callback(interaction)

    assert view.value is None
    assert not view.is_finished()
    assert interaction.response.messages == [("unauthorized", {"ephemeral": True})]
    assert interaction.response.edits == []


@pytest.mark.asyncio
async def test_delete_cancel_finishes_without_confirming() -> None:
    interaction = FakeInteraction(user_id=333)
    view = ConfirmDeleteUserDataView(
        requesting_user_id=333,
        confirm_label="Confirm",
        cancel_label="Cancel",
        in_progress_message="processing",
        cancelled_message="cancelled",
        unauthorized_message="unauthorized",
    )

    await child_with_label(view, "Cancel").callback(interaction)

    assert view.value is False
    assert view.is_finished()
    assert interaction.response.edits == [("cancelled", {"view": None})]


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
