from __future__ import annotations

import copy
import re
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pandas as pd
import pytest

from models.team_register import TeamRegisterConfig
from tests.fakes import FakeWorksheet
from utils import shift_register_manager
from utils.google_sheets_errors import GoogleSheetsError, GoogleSheetsErrorKind
from utils.manager_base import ManagerBase
from utils.shift_register_manager import ShiftRegisterManager
from utils.shift_register_structs import (
    DraftWorksheetMetadata,
    EntryWorksheetContent,
    EntryWorksheetMetadata,
    FinalScheduleWorksheetMetadata,
    RecruitmentTimeRanges,
    ShiftParser,
    ShiftRegisterGoogleSheetsMetadata,
    build_team_summary_formula,
)
from utils.shift_scheduler import DraftTeamProfile
from utils.storage_errors import StorageError, StorageErrorKind
from utils.structs_base import UserInfo
from utils.team_register_manager import TeamRegisterManager
from utils.team_register_structs import (
    Summary,
    SummaryWorksheetMetadata,
    TeamParser,
    TeamRegisterGoogleSheetsMetadata,
    TeamWorksheetContent,
    TeamWorksheetMetadata,
)

if TYPE_CHECKING:
    from collections.abc import Generator
    from typing import Self


def make_feature_channel(
    feature_name: str,
    *,
    feature_channel_id: int = 1,
    guild_id: int = 1,
    channel_id: int = 2,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=feature_channel_id,
        guild_id=guild_id,
        channel_id=channel_id,
        feature_name=feature_name,
    )


def make_user(username: str = "alice", display_name: str = "Alice") -> UserInfo:
    return UserInfo(username=username, display_name=display_name)


class FakeTeamConfigQuery:
    def __init__(self, configs: list[SimpleNamespace]) -> None:
        self.configs = configs
        self.selected_related: tuple[str, ...] = ()
        self.filter_kwargs: dict[str, object] = {}

    def select_related(self, *fields: str) -> Self:
        self.selected_related = fields
        return self

    def __await__(self) -> Generator[object, None, list[SimpleNamespace]]:
        async def resolve() -> list[SimpleNamespace]:
            return self.configs

        return resolve().__await__()


class FakeTeamSourceWorksheet:
    def __init__(
        self,
        worksheet_id: int,
        title: str,
        header: list[object] | None = None,
        rows: list[list[object]] | None = None,
    ) -> None:
        self.id = worksheet_id
        self.title = title
        self.header = header
        self.rows = rows or []
        self.batch_gets: list[list[str]] = []

    async def batch_get_values(self, ranges: list[str]) -> list[list[list[object]]]:
        self.batch_gets.append(ranges)
        if ranges == ["1:1"]:
            return [[self.header]] if self.header is not None else [[]]
        return [[[*(self.header or [])], *self.rows]]


class FakeTeamSourceSheet:
    def __init__(
        self,
        worksheets: list[FakeTeamSourceWorksheet],
        *,
        error: GoogleSheetsError | None = None,
    ) -> None:
        self.worksheets = {worksheet.id: worksheet for worksheet in worksheets}
        self.error = error

    async def get_worksheets(
        self, worksheet_ids: list[int]
    ) -> dict[int, FakeTeamSourceWorksheet | None]:
        if self.error is not None:
            raise self.error
        return {
            worksheet_id: self.worksheets.get(worksheet_id)
            for worksheet_id in worksheet_ids
        }


def configure_team_source_query(
    monkeypatch: pytest.MonkeyPatch,
    *,
    manager: ShiftRegisterManager,
    configs: list[SimpleNamespace],
    source_sheet: FakeTeamSourceSheet | None = None,
) -> FakeTeamConfigQuery:
    query = FakeTeamConfigQuery(configs)
    if manager._sheet_config is None:  # noqa: SLF001
        manager._sheet_config = SimpleNamespace(  # noqa: SLF001
            team_source_feature_channel_id=None
        )

    def filter_configs(**kwargs: object) -> FakeTeamConfigQuery:
        query.filter_kwargs = kwargs
        return query

    monkeypatch.setattr(
        TeamRegisterConfig,
        "filter",
        filter_configs,
    )
    if source_sheet is not None:
        monkeypatch.setattr(
            shift_register_manager,
            "GoogleSheet",
            lambda _url, _path: source_sheet,
            raising=False,
        )
    return query


def make_team_source_config(
    *,
    team_worksheet_ids: list[int] | None = None,
    landing_worksheet_id: int = 201,
    feature_channel_id: int = 22,
    channel_id: int = 22,
) -> SimpleNamespace:
    worksheet_ids = [*(team_worksheet_ids or [101, 102]), 201]
    return SimpleNamespace(
        sheet_url="https://team.sheet.example",
        team_worksheet_ids=team_worksheet_ids or [101, 102],
        summary_worksheet_id=201,
        landing_worksheet_id=landing_worksheet_id,
        feature_channel_id=feature_channel_id,
        feature_channel=SimpleNamespace(
            id=feature_channel_id,
            guild_id=1,
            channel_id=channel_id,
            feature_name="team_register",
        ),
        get_worksheet_ids=lambda: worksheet_ids,
    )


TEAM_SUMMARY_HEADER = [
    "username",
    "display_name",
    "encore_roles",
    "Main Team ISV",
    "Main Team Power",
    "Encore Team ISV",
    "Encore Team Power",
]


class FakeEntryWorksheet:
    RANGE_PATTERN = re.compile(
        r"(?P<start_col>[A-Z]+)(?P<start_row>\d+)"
        r"(?::(?P<end_col>[A-Z]+)(?P<end_row>\d+))?"
    )

    def __init__(
        self,
        rows: list[list[object]] | None = None,
        *,
        title: str = "Shift Entry",
        worksheet_id: int = 1,
        row_count: int = 100,
        col_count: int = 40,
    ) -> None:
        self.title = title
        self.id = worksheet_id
        self.rows = copy.deepcopy(rows or [])
        self.row_count = max(row_count, len(self.rows))
        self.col_count = max(col_count, max(map(len, self.rows), default=0))
        self.batch_gets: list[list[str]] = []
        self.batch_updates: list[list[dict[str, object]]] = []
        self.typed_batch_updates: list[list[dict[str, object]]] = []
        self.typed_formula_ranges: list[set[str]] = []
        self.conditional_format_rules: list[dict[str, object]] = []
        self.presentation_updates: list[dict[str, object]] = []
        self.ensure_calls: list[tuple[int, int]] = []
        self.deleted_rows: list[int] = []

    async def batch_get_values(self, ranges: list[str]) -> list[list[list[object]]]:
        self.batch_gets.append(ranges)
        assert ranges == ["1:2", "A3:C"]
        header_rows = self._trim_range_rows(self.rows[:2])
        participant_rows = self._trim_range_rows([row[:3] for row in self.rows[2:]])
        return [header_rows, participant_rows]

    async def batch_update_values(self, data: list[dict[str, object]]) -> None:
        self.batch_updates.append(copy.deepcopy(data))
        for item in data:
            self._apply_range(str(item["range"]), item["values"])

    async def batch_update_typed_values(  # noqa: PLR0913
        self,
        data: list[dict[str, object]],
        *,
        formula_ranges: set[str],
        background_updates: object = (),
        border_updates: object = (),
        format_updates: object = (),
        column_width_updates: object = (),
        hidden_column_updates: object = (),
        conditional_format_rule_deletes: object = (),
        conditional_format_rule_adds: object = (),
        frozen_column_count: int | None = None,
    ) -> None:
        copied = copy.deepcopy(data)
        self.batch_updates.append(copied)
        self.typed_batch_updates.append(copied)
        self.typed_formula_ranges.append(formula_ranges)
        self.presentation_updates.append(
            {
                "background_updates": list(background_updates),
                "border_updates": list(border_updates),
                "format_updates": list(format_updates),
                "column_width_updates": list(column_width_updates),
                "hidden_column_updates": list(hidden_column_updates),
                "conditional_format_rule_deletes": list(
                    conditional_format_rule_deletes
                ),
                "conditional_format_rule_adds": list(conditional_format_rule_adds),
                "frozen_column_count": frozen_column_count,
            }
        )
        for item in data:
            self._apply_range(str(item["range"]), item["values"])

    async def get_conditional_format_rules(self) -> list[dict[str, object]]:
        return copy.deepcopy(self.conditional_format_rules)

    async def ensure_size(self, *, min_rows: int, min_cols: int) -> None:
        self.ensure_calls.append((min_rows, min_cols))
        self.row_count = max(self.row_count, min_rows)
        self.col_count = max(self.col_count, min_cols)

    async def delete_row(self, index: int) -> None:
        self.deleted_rows.append(index)
        if index <= len(self.rows):
            self.rows.pop(index - 1)
        self.row_count -= 1

    @staticmethod
    def _trim_range_rows(rows: list[list[object]]) -> list[list[object]]:
        trimmed = [FakeEntryWorksheet._trim_row(row) for row in rows]
        while trimmed and not trimmed[-1]:
            trimmed.pop()
        return trimmed

    @staticmethod
    def _trim_row(row: list[object]) -> list[object]:
        trimmed = list(row)
        while trimmed and trimmed[-1] in ("", None):
            trimmed.pop()
        return trimmed

    @staticmethod
    def _column_index(letters: str) -> int:
        index = 0
        for letter in letters:
            index = index * 26 + ord(letter) - ord("A") + 1
        return index

    def _apply_range(self, range_name: str, values: object) -> None:
        match = self.RANGE_PATTERN.fullmatch(range_name)
        assert match is not None
        start_row = int(match.group("start_row"))
        start_col = self._column_index(match.group("start_col"))
        value_rows = values
        assert isinstance(value_rows, list)
        for row_offset, value_row in enumerate(value_rows):
            assert isinstance(value_row, list)
            row_index = start_row + row_offset - 1
            while len(self.rows) <= row_index:
                self.rows.append([])
            required_cols = start_col - 1 + len(value_row)
            self.rows[row_index].extend(
                [""] * max(0, required_cols - len(self.rows[row_index]))
            )
            self.rows[row_index][start_col - 1 : required_cols] = value_row


def make_shift_metadata(
    worksheet: FakeEntryWorksheet | None,
) -> ShiftRegisterGoogleSheetsMetadata:
    return ShiftRegisterGoogleSheetsMetadata(
        "https://sheet.example",
        [
            EntryWorksheetMetadata(1, "Shift Entry", worksheet),
            DraftWorksheetMetadata(2, "Shift Draft", None),
            FinalScheduleWorksheetMetadata(3, "Shift Final Schedule", None),
        ],
    )


def available_team_source() -> shift_register_manager.TeamSourceResolution:
    config = make_team_source_config()
    metadata = TeamRegisterGoogleSheetsMetadata.from_subtyped_worksheets(
        config.sheet_url,
        [
            TeamWorksheetMetadata(101, "Main Team", None),
            TeamWorksheetMetadata(102, "Encore Team", None),
            SummaryWorksheetMetadata(201, "Team Summary", None),
        ],
    )
    return shift_register_manager.TeamSourceResolution(
        shift_register_manager.TeamSourceStatus.AVAILABLE,
        shift_register_manager.TeamSource(
            config=config,
            metadata=metadata,
            summary_columns=shift_register_manager.TeamSummaryColumns(
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


def renamed_team_source() -> shift_register_manager.TeamSourceResolution:
    resolution = available_team_source()
    source = resolution.source
    assert source is not None
    source.metadata.summary_worksheet.title = "Renamed Summary"
    return resolution


def configure_row_source(
    manager: ShiftRegisterManager,
    resolution: shift_register_manager.TeamSourceResolution,
) -> None:
    async def resolve() -> shift_register_manager.TeamSourceResolution:
        return resolution

    manager.resolve_team_source = resolve  # type: ignore[method-assign]


def current_entry_rows(*participant_rows: list[object]) -> list[list[object]]:
    return [
        EntryWorksheetContent.count_row(),
        EntryWorksheetContent.COLUMNS,
        *participant_rows,
    ]


def expected_formula(row: int) -> str:
    source = available_team_source().source
    assert source is not None
    summary = source.metadata.summary_worksheet
    columns = source.summary_columns
    assert summary.title is not None
    return build_team_summary_formula(
        row=row,
        sheet_url=source.config.sheet_url,
        worksheet_title=summary.title,
        username_column=columns.username,
        roles_column=columns.roles,
        main_isv_column=columns.main_isv,
        encore_isv_column=columns.encore_isv,
        import_last_column=columns.import_last_column,
    )


def entry_participant_row(  # noqa: PLR0913
    username: str,
    display_name: str,
    formula: str = "",
    *,
    slots: set[int] | None = None,
    original_message: str = "",
    manual_value: object = "",
) -> list[object]:
    row: list[object] = [""] * 37
    row[0:3] = [username, display_name, formula]
    for slot in slots or set():
        row[5 + slot] = 1
    row[35] = original_message
    row[36] = manual_value
    return row


@pytest.mark.asyncio
async def test_team_manager_fresh_config_invalidates_cached_google_sheet() -> None:
    manager = TeamRegisterManager(make_feature_channel("team_register"), "service.json")
    old_config = SimpleNamespace(sheet_url="https://old.sheet.example")
    new_config = SimpleNamespace(sheet_url="https://new.sheet.example")
    cached_sheet = SimpleNamespace(sheet_url=old_config.sheet_url)

    class FakeSheetConfig:
        @classmethod
        async def get_or_none(cls, *, feature_channel: object) -> SimpleNamespace:
            assert feature_channel is manager.feature_channel
            return new_config

    manager.SheetConfigType = FakeSheetConfig
    manager._sheet_config = old_config  # noqa: SLF001
    manager._google_sheet = cached_sheet  # noqa: SLF001

    refreshed_config = await manager.get_fresh_sheet_config()

    assert refreshed_config is new_config
    assert manager._sheet_config is new_config  # noqa: SLF001
    assert manager._google_sheet is None  # noqa: SLF001


@pytest.mark.asyncio
async def test_shift_manager_fresh_config_invalidates_cached_google_sheet() -> None:
    manager = ShiftRegisterManager(
        make_feature_channel("shift_register"), "service.json"
    )
    old_config = SimpleNamespace(sheet_url="https://old.sheet.example")
    new_config = SimpleNamespace(sheet_url="https://new.sheet.example")
    cached_sheet = SimpleNamespace(sheet_url=old_config.sheet_url)

    class FakeSheetConfig:
        @classmethod
        async def get_or_none(cls, *, feature_channel: object) -> SimpleNamespace:
            assert feature_channel is manager.feature_channel
            return new_config

    manager.SheetConfigType = FakeSheetConfig
    manager._sheet_config = old_config  # noqa: SLF001
    manager._google_sheet = cached_sheet  # noqa: SLF001

    refreshed_config = await manager.get_fresh_sheet_config()

    assert refreshed_config is new_config
    assert manager._sheet_config is new_config  # noqa: SLF001
    assert manager._google_sheet is None  # noqa: SLF001


@pytest.mark.asyncio
async def test_shift_manager_requires_explicit_team_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = ShiftRegisterManager(
        make_feature_channel("shift_register"), "service.json"
    )
    config = make_team_source_config()
    source_sheet = FakeTeamSourceSheet(
        [
            FakeTeamSourceWorksheet(101, "Renamed Main"),
            FakeTeamSourceWorksheet(102, "Renamed Encore"),
            FakeTeamSourceWorksheet(
                201,
                "Renamed Summary",
                [
                    "username",
                    "display_name",
                    "encore_roles",
                    "Renamed Main ISV",
                    "Renamed Main Power",
                    "Renamed Encore ISV",
                    "Renamed Encore Power",
                ],
            ),
        ]
    )
    query = configure_team_source_query(
        monkeypatch,
        manager=manager,
        configs=[config],
        source_sheet=source_sheet,
    )

    resolution = await manager.resolve_team_source()

    assert resolution.status is shift_register_manager.TeamSourceStatus.UNSET
    assert resolution.source is None
    assert query.selected_related == ()
    assert query.filter_kwargs == {}


@pytest.mark.asyncio
async def test_shift_manager_prefers_saved_team_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = ShiftRegisterManager(
        make_feature_channel("shift_register"), "service.json"
    )
    manager._sheet_config = SimpleNamespace(  # noqa: SLF001
        team_source_feature_channel_id=23
    )
    query = configure_team_source_query(
        monkeypatch,
        manager=manager,
        configs=[make_team_source_config(feature_channel_id=23, channel_id=33)],
        source_sheet=FakeTeamSourceSheet(
            [
                FakeTeamSourceWorksheet(101, "Main Team"),
                FakeTeamSourceWorksheet(102, "Encore Team"),
                FakeTeamSourceWorksheet(
                    201,
                    "Team Summary",
                    [
                        "username",
                        "display_name",
                        "encore_roles",
                        "Main Team ISV",
                        "Main Team Power",
                        "Encore Team ISV",
                        "Encore Team Power",
                    ],
                ),
            ]
        ),
    )

    resolution = await manager.resolve_team_source()

    assert resolution.status is shift_register_manager.TeamSourceStatus.AVAILABLE
    assert resolution.source is not None
    assert resolution.source.config.feature_channel.channel_id == 33
    assert query.filter_kwargs == {"feature_channel_id": 23}


@pytest.mark.asyncio
async def test_shift_manager_reads_draft_profiles_from_selected_team_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = ShiftRegisterManager(
        make_feature_channel("shift_register"), "service.json"
    )
    manager._sheet_config = SimpleNamespace(  # noqa: SLF001
        team_source_feature_channel_id=22
    )
    summary = FakeTeamSourceWorksheet(
        201,
        "Team Summary",
        TEAM_SUMMARY_HEADER,
        [
            ["alice", "Alice", "Encore", 200, 40, 250, 50],
            ["bob", "Bob", "Encore", 190, 45, "", ""],
            ["carol", "Carol", "", 210, 55, 260, 60],
        ],
    )
    configure_team_source_query(
        monkeypatch,
        manager=manager,
        configs=[make_team_source_config()],
        source_sheet=FakeTeamSourceSheet(
            [
                FakeTeamSourceWorksheet(101, "Main Team"),
                FakeTeamSourceWorksheet(102, "Encore Team"),
                summary,
            ]
        ),
    )

    resolution = await manager.resolve_draft_team_profiles()

    assert resolution.status is shift_register_manager.TeamSourceStatus.AVAILABLE
    assert resolution.profiles["alice"] == DraftTeamProfile(
        main_isv=200,
        main_power=40,
        encore_isv=250,
        encore_power=50,
        has_encore_role=True,
    )
    assert resolution.profiles["bob"].has_encore_team is False
    assert resolution.profiles["carol"].has_encore_role is False
    assert resolution.notes_team_source == shift_register_manager.DraftNotesTeamSource(
        sheet_url="https://team.sheet.example",
        worksheet_title="Team Summary",
        import_last_column="G",
        username_header="username",
        roles_header="encore_roles",
        main_isv_header="Main Team ISV",
        main_power_header="Main Team Power",
        encore_isv_header="Encore Team ISV",
        encore_power_header="Encore Team Power",
    )
    assert summary.batch_gets == [["1:1"], ["A:G"]]


@pytest.mark.asyncio
async def test_shift_manager_rejects_duplicate_draft_profile_usernames(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = ShiftRegisterManager(
        make_feature_channel("shift_register"), "service.json"
    )
    manager._sheet_config = SimpleNamespace(  # noqa: SLF001
        team_source_feature_channel_id=22
    )
    summary = FakeTeamSourceWorksheet(
        201,
        "Team Summary",
        TEAM_SUMMARY_HEADER,
        [
            ["alice", "Alice", "Encore", 200, 40, 250, 50],
            ["alice", "Alice 2", "Encore", 190, 45, "", ""],
        ],
    )
    configure_team_source_query(
        monkeypatch,
        manager=manager,
        configs=[make_team_source_config()],
        source_sheet=FakeTeamSourceSheet(
            [
                FakeTeamSourceWorksheet(101, "Main Team"),
                FakeTeamSourceWorksheet(102, "Encore Team"),
                summary,
            ]
        ),
    )

    resolution = await manager.resolve_draft_team_profiles()

    assert resolution == shift_register_manager.DraftTeamProfileResolution(
        shift_register_manager.TeamSourceStatus.INVALID,
        {},
    )


@pytest.mark.asyncio
async def test_shift_manager_keeps_entry_source_without_power_but_rejects_profiles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = ShiftRegisterManager(
        make_feature_channel("shift_register"), "service.json"
    )
    manager._sheet_config = SimpleNamespace(  # noqa: SLF001
        team_source_feature_channel_id=22
    )
    configure_team_source_query(
        monkeypatch,
        manager=manager,
        configs=[make_team_source_config(team_worksheet_ids=[101])],
        source_sheet=FakeTeamSourceSheet(
            [
                FakeTeamSourceWorksheet(101, "Only Main"),
                FakeTeamSourceWorksheet(
                    201,
                    "Team Summary",
                    [
                        "username",
                        "display_name",
                        "encore_roles",
                        "Only Main ISV",
                    ],
                ),
            ]
        ),
    )

    source = await manager.resolve_team_source()
    profiles = await manager.resolve_draft_team_profiles()

    assert source.status is shift_register_manager.TeamSourceStatus.AVAILABLE
    assert profiles == shift_register_manager.DraftTeamProfileResolution(
        shift_register_manager.TeamSourceStatus.INVALID,
        {},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status",
    [
        shift_register_manager.TeamSourceStatus.UNSET,
        shift_register_manager.TeamSourceStatus.MISSING,
        shift_register_manager.TeamSourceStatus.AMBIGUOUS,
        shift_register_manager.TeamSourceStatus.INVALID,
        shift_register_manager.TeamSourceStatus.UNRESOLVED,
    ],
)
async def test_draft_profiles_preserve_unavailable_team_source_status(
    monkeypatch: pytest.MonkeyPatch,
    status: shift_register_manager.TeamSourceStatus,
) -> None:
    manager = ShiftRegisterManager(
        make_feature_channel("shift_register"), "service.json"
    )
    monkeypatch.setattr(
        manager,
        "resolve_team_source",
        AsyncMock(return_value=shift_register_manager.TeamSourceResolution(status)),
    )

    resolution = await manager.resolve_draft_team_profiles()

    assert resolution == shift_register_manager.DraftTeamProfileResolution(
        status,
        {},
    )


@pytest.mark.asyncio
async def test_shift_manager_lists_configured_team_source_channels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = ShiftRegisterManager(
        make_feature_channel("shift_register"), "service.json"
    )
    query = configure_team_source_query(
        monkeypatch,
        manager=manager,
        configs=[
            make_team_source_config(feature_channel_id=22, channel_id=32),
            make_team_source_config(feature_channel_id=23, channel_id=33),
        ],
    )

    channel_ids = await manager.get_team_source_candidate_channel_ids()

    assert channel_ids == (32, 33)
    assert query.selected_related == ("feature_channel",)
    assert query.filter_kwargs == {
        "feature_channel__guild_id": 1,
        "feature_channel__feature_name": "team_register",
    }


@pytest.mark.asyncio
async def test_shift_manager_validates_explicit_team_channel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = ShiftRegisterManager(
        make_feature_channel("shift_register"), "service.json"
    )
    query = configure_team_source_query(monkeypatch, manager=manager, configs=[])

    resolution = await manager.resolve_team_source(team_channel_id=33)

    assert resolution.status is shift_register_manager.TeamSourceStatus.INVALID
    assert query.filter_kwargs == {
        "feature_channel__guild_id": 1,
        "feature_channel__channel_id": 33,
        "feature_channel__feature_name": "team_register",
    }


@pytest.mark.asyncio
async def test_shift_manager_resolves_main_only_team_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = ShiftRegisterManager(
        make_feature_channel("shift_register"), "service.json"
    )
    manager._sheet_config = SimpleNamespace(  # noqa: SLF001
        team_source_feature_channel_id=22
    )
    config = make_team_source_config(team_worksheet_ids=[101])
    source_sheet = FakeTeamSourceSheet(
        [
            FakeTeamSourceWorksheet(101, "Only Main"),
            FakeTeamSourceWorksheet(
                201,
                "Team Summary",
                [
                    "username",
                    "display_name",
                    "encore_roles",
                    "Only Main ISV",
                    "Only Main Power",
                ],
            ),
        ]
    )
    configure_team_source_query(
        monkeypatch,
        manager=manager,
        configs=[config],
        source_sheet=source_sheet,
    )

    resolution = await manager.resolve_team_source()

    assert resolution.status is shift_register_manager.TeamSourceStatus.AVAILABLE
    assert resolution.source is not None
    assert resolution.source.summary_columns.encore_isv is None
    assert resolution.source.summary_columns.import_last_column == "E"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "worksheets",
    [
        [FakeTeamSourceWorksheet(101, "Main")],
        [
            FakeTeamSourceWorksheet(101, "Main"),
            FakeTeamSourceWorksheet(102, "Encore"),
            FakeTeamSourceWorksheet(
                201,
                "Summary",
                ["username", "display_name", "encore_roles", "wrong"],
            ),
        ],
    ],
    ids=["missing-summary", "malformed-header"],
)
async def test_shift_manager_reports_invalid_team_source(
    monkeypatch: pytest.MonkeyPatch,
    worksheets: list[FakeTeamSourceWorksheet],
) -> None:
    manager = ShiftRegisterManager(
        make_feature_channel("shift_register"), "service.json"
    )
    manager._sheet_config = SimpleNamespace(  # noqa: SLF001
        team_source_feature_channel_id=22
    )
    configure_team_source_query(
        monkeypatch,
        manager=manager,
        configs=[make_team_source_config()],
        source_sheet=FakeTeamSourceSheet(worksheets),
    )

    resolution = await manager.resolve_team_source()

    assert resolution.status is shift_register_manager.TeamSourceStatus.INVALID
    assert resolution.source is None


@pytest.mark.asyncio
async def test_shift_manager_rejects_missing_team_landing_worksheet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = ShiftRegisterManager(
        make_feature_channel("shift_register"), "service.json"
    )
    manager._sheet_config = SimpleNamespace(  # noqa: SLF001
        team_source_feature_channel_id=22
    )
    config = make_team_source_config(landing_worksheet_id=999)
    source_sheet = FakeTeamSourceSheet(
        [
            FakeTeamSourceWorksheet(101, "Main"),
            FakeTeamSourceWorksheet(102, "Encore"),
            FakeTeamSourceWorksheet(
                201,
                "Summary",
                [
                    "username",
                    "display_name",
                    "encore_roles",
                    "Main ISV",
                    "Main Power",
                    "Encore ISV",
                    "Encore Power",
                ],
            ),
        ]
    )
    configure_team_source_query(
        monkeypatch,
        manager=manager,
        configs=[config],
        source_sheet=source_sheet,
    )

    resolution = await manager.resolve_team_source()

    assert resolution.status is shift_register_manager.TeamSourceStatus.INVALID
    assert resolution.source is None


@pytest.mark.asyncio
async def test_shift_manager_reports_transient_team_source_as_unresolved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = ShiftRegisterManager(
        make_feature_channel("shift_register"), "service.json"
    )
    manager._sheet_config = SimpleNamespace(  # noqa: SLF001
        team_source_feature_channel_id=22
    )
    error = GoogleSheetsError(
        GoogleSheetsErrorKind.TRANSIENT,
        "temporarily unavailable",
    )
    configure_team_source_query(
        monkeypatch,
        manager=manager,
        configs=[make_team_source_config()],
        source_sheet=FakeTeamSourceSheet([], error=error),
    )

    resolution = await manager.resolve_team_source()

    assert resolution.status is shift_register_manager.TeamSourceStatus.UNRESOLVED
    assert resolution.source is None


@pytest.mark.asyncio
async def test_team_manager_upserts_and_deletes_user_team_with_fake_worksheet() -> None:
    manager = TeamRegisterManager(make_feature_channel("team_register"), "service.json")
    worksheet = FakeWorksheet(title="Main Team")
    user = make_user()
    team = TeamParser.parse_line(user, "150/740/33.4 main")

    await manager.upsert_or_delete_user_team(user, team, worksheet)

    inserted = worksheet.updated_frames[-1]
    assert inserted.loc[0, "username"] == "alice"
    assert inserted.loc[0, "leader_skill_value"] == 150

    await manager.upsert_or_delete_user_team(user, None, worksheet)

    deleted = worksheet.updated_frames[-1]
    assert "alice" not in set(deleted["username"].astype(str))


@pytest.mark.asyncio
async def test_shift_sheet_setup_initializes_entry_presentation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = ShiftRegisterManager(
        make_feature_channel("shift_register"), "service.json"
    )
    metadata = make_shift_metadata(FakeEntryWorksheet(rows=[]))
    parent_upsert = AsyncMock(return_value=metadata)
    monkeypatch.setattr(
        ManagerBase,
        "upsert_sheet_config_and_worksheets",
        parent_upsert,
    )
    manager.get_sheet_config = AsyncMock(
        return_value=SimpleNamespace(recruitment_time_ranges=[{"start": 4, "end": 28}])
    )
    manager.sync_entry_presentation = AsyncMock()

    result = await manager.upsert_sheet_config_and_worksheets(
        "https://sheet.example",
        entry_worksheet_title="Entry",
        draft_worksheet_title="Draft",
        final_schedule_worksheet_title="Final",
    )

    assert result is metadata
    manager.sync_entry_presentation.assert_awaited_once()
    sync_metadata, sync_ranges = manager.sync_entry_presentation.await_args.args
    assert sync_metadata is metadata
    assert sync_ranges.to_json() == [{"start": 4, "end": 28}]
    assert manager.sync_entry_presentation.await_args.kwargs == {"force": True}


@pytest.mark.asyncio
async def test_shift_manager_initializes_empty_entry_worksheet() -> None:
    manager = ShiftRegisterManager(
        make_feature_channel("shift_register"), "service.json"
    )
    configure_row_source(
        manager,
        shift_register_manager.TeamSourceResolution(
            shift_register_manager.TeamSourceStatus.MISSING
        ),
    )
    worksheet = FakeEntryWorksheet(rows=[], row_count=2, col_count=20)
    shift = ShiftParser.parse_submission(make_user(), ["4-8"]).shift
    assert shift is not None

    await manager.upsert_or_delete_user_shift(
        make_user(), shift, make_shift_metadata(worksheet)
    )

    assert len(worksheet.typed_batch_updates) == 1
    assert [item["range"] for item in worksheet.typed_batch_updates[0]] == [
        "A1:AJ1",
        "A2:AJ2",
        "A3:B3",
        "F3:AJ3",
    ]
    assert worksheet.rows[0][:36] == EntryWorksheetContent.count_row()
    assert worksheet.rows[1][:36] == EntryWorksheetContent.COLUMNS
    assert worksheet.rows[2][0:2] == ["alice", "Alice"]
    assert worksheet.rows[2][9] == 1
    assert worksheet.rows[2][13] == 0
    assert worksheet.ensure_calls == [(3, 36)]


def test_entry_presentation_plan_matches_approved_sheet_contract() -> None:
    ranges = RecruitmentTimeRanges.from_modal_input("4-12, 20-28")

    plan = shift_register_manager._entry_presentation_plan(  # noqa: SLF001
        ranges,
        worksheet_id=1,
    )

    assert plan.frozen_column_count == 5
    assert plan.column_width_updates == (
        ("A:B", 100),
        ("C:D", 60),
        ("E:E", 60),
        ("F:AI", 40),
    )
    assert plan.hidden_column_updates == (
        ("F:AI", False),
        ("F:I", True),
        ("AH:AI", True),
    )
    assert ("A2:AJ2", "#000000", "SOLID", ("top", "bottom")) in (plan.border_updates)
    assert ("B:B", "#000000", "SOLID", ("right",)) in plan.border_updates
    assert ("E:E", "#000000", "SOLID", ("right",)) in plan.border_updates
    assert ("AI:AI", "#000000", "SOLID", ("right",)) in plan.border_updates
    formulas = [
        rule["booleanRule"]["condition"]["values"][0]["userEnteredValue"]
        for rule in plan.conditional_format_rules
    ]
    assert all("rhoboto:shift-entry:" in formula for formula in formulas)
    assert any("ISODD(SUBTOTAL(103,$A$3:$A3))" in formula for formula in formulas)
    assert any("ISEVEN(SUBTOTAL(103,$A$3:$A3))" in formula for formula in formulas)
    gap_rule = next(
        rule for rule in plan.conditional_format_rules if "gap:v1" in str(rule)
    )
    assert gap_rule["ranges"] == [
        {
            "sheetId": 1,
            "startRowIndex": 2,
            "startColumnIndex": 17,
            "endColumnIndex": 25,
        }
    ]
    assert gap_rule["booleanRule"]["format"]["backgroundColorStyle"] == {
        "rgbColor": {"red": 0.8, "green": 0.8, "blue": 0.8}
    }


def test_entry_rule_updates_replace_only_marked_rules_in_descending_order() -> None:
    desired = shift_register_manager._entry_presentation_plan(  # noqa: SLF001
        RecruitmentTimeRanges.default(),
        worksheet_id=1,
    ).conditional_format_rules
    stale = copy.deepcopy(desired[0])
    stale["booleanRule"]["condition"]["values"][0]["userEnteredValue"] = (
        '=N("rhoboto:shift-entry:stale:v0")=0'
    )
    manual = {"booleanRule": {"condition": {"type": "TEXT_EQ"}}}

    deletes, adds, current = shift_register_manager._entry_rule_updates(  # noqa: SLF001
        [stale, manual, copy.deepcopy(stale)],
        desired,
    )

    assert deletes == (2, 0)
    assert adds == tuple(reversed(desired))
    assert current is False


@pytest.mark.asyncio
async def test_shift_entry_style_repair_shares_participant_atomic_batch() -> None:
    manager = ShiftRegisterManager(
        make_feature_channel("shift_register"), "service.json"
    )
    configure_row_source(
        manager,
        shift_register_manager.TeamSourceResolution(
            shift_register_manager.TeamSourceStatus.MISSING
        ),
    )
    worksheet = FakeEntryWorksheet(current_entry_rows())
    shift = ShiftParser.parse_submission(make_user(), ["4-8"]).shift
    assert shift is not None

    await manager.upsert_or_delete_user_shift(
        make_user(),
        shift,
        make_shift_metadata(worksheet),
        recruitment_ranges=RecruitmentTimeRanges.from_modal_input("4-12, 20-28"),
    )

    presentation = worksheet.presentation_updates[-1]
    assert presentation["frozen_column_count"] == 5
    assert presentation["conditional_format_rule_adds"]
    assert presentation["hidden_column_updates"] == [
        ("F:AI", False),
        ("F:I", True),
        ("AH:AI", True),
    ]
    assert len(worksheet.typed_batch_updates) == 1


@pytest.mark.asyncio
async def test_empty_shift_entry_presentation_initializes_without_participant() -> None:
    manager = ShiftRegisterManager(
        make_feature_channel("shift_register"), "service.json"
    )
    worksheet = FakeEntryWorksheet(rows=[], row_count=2, col_count=20)

    await manager.sync_entry_presentation(
        make_shift_metadata(worksheet),
        RecruitmentTimeRanges.from_modal_input("4-12, 20-28"),
        force=True,
    )

    assert [item["range"] for item in worksheet.typed_batch_updates[-1]] == [
        "A1:AJ1",
        "A2:AJ2",
    ]
    assert worksheet.rows[0][:36] == EntryWorksheetContent.count_row()
    assert worksheet.rows[1][:36] == EntryWorksheetContent.COLUMNS
    assert len(worksheet.rows) == 2
    assert worksheet.ensure_calls == [(3, 36)]
    assert worksheet.presentation_updates[-1]["frozen_column_count"] == 5


@pytest.mark.asyncio
async def test_entry_presentation_sync_ignores_duplicate_usernames() -> None:
    manager = ShiftRegisterManager(
        make_feature_channel("shift_register"), "service.json"
    )
    worksheet = FakeEntryWorksheet(
        current_entry_rows(
            entry_participant_row("alice", "Alice"),
            entry_participant_row("alice", "Duplicate"),
        )
    )

    await manager.sync_entry_presentation(
        make_shift_metadata(worksheet),
        RecruitmentTimeRanges.default(),
        force=True,
    )

    assert len(worksheet.typed_batch_updates) == 1
    assert worksheet.presentation_updates[-1]["frozen_column_count"] == 5


@pytest.mark.asyncio
async def test_current_shift_entry_rules_do_not_repeat_presentation_updates() -> None:
    manager = ShiftRegisterManager(
        make_feature_channel("shift_register"), "service.json"
    )
    configure_row_source(manager, available_team_source())
    worksheet = FakeEntryWorksheet(current_entry_rows())
    ranges = RecruitmentTimeRanges.from_modal_input("4-12, 20-28")
    worksheet.conditional_format_rules = list(
        shift_register_manager._entry_presentation_plan(  # noqa: SLF001
            ranges,
            worksheet_id=worksheet.id,
        ).conditional_format_rules
    )
    shift = ShiftParser.parse_submission(make_user(), ["4-8"]).shift
    assert shift is not None

    await manager.upsert_or_delete_user_shift(
        make_user(),
        shift,
        make_shift_metadata(worksheet),
        recruitment_ranges=ranges,
    )

    assert worksheet.presentation_updates[-1] == {
        "background_updates": [],
        "border_updates": [],
        "format_updates": [],
        "column_width_updates": [],
        "hidden_column_updates": [],
        "conditional_format_rule_deletes": [],
        "conditional_format_rule_adds": [],
        "frozen_column_count": None,
    }


@pytest.mark.asyncio
async def test_existing_shift_updates_owned_ranges_on_same_row() -> None:
    manager = ShiftRegisterManager(
        make_feature_channel("shift_register"), "service.json"
    )
    configure_row_source(manager, available_team_source())
    worksheet = FakeEntryWorksheet(
        current_entry_rows(
            entry_participant_row("bob", "Bob", expected_formula(3)),
            entry_participant_row(
                "alice",
                "Old Alice",
                expected_formula(4),
                manual_value="manual note",
            ),
        )
    )
    shift = ShiftParser.parse_submission(make_user(), ["15-17"]).shift
    assert shift is not None

    await manager.upsert_or_delete_user_shift(
        make_user(), shift, make_shift_metadata(worksheet)
    )

    assert [item["range"] for item in worksheet.batch_updates[-1]] == [
        "A4:B4",
        "F4:AJ4",
    ]
    assert worksheet.rows[3][36] == "manual note"
    assert all(
        not str(item["range"]).startswith("AK") for item in worksheet.batch_updates[-1]
    )


@pytest.mark.asyncio
async def test_new_shift_writes_formula_anchor_but_not_spill_cells() -> None:
    manager = ShiftRegisterManager(
        make_feature_channel("shift_register"), "service.json"
    )
    configure_row_source(manager, available_team_source())
    worksheet = FakeEntryWorksheet(current_entry_rows())
    shift = ShiftParser.parse_submission(make_user(), ["0-2"]).shift
    assert shift is not None

    await manager.upsert_or_delete_user_shift(
        make_user(), shift, make_shift_metadata(worksheet)
    )

    updates = worksheet.batch_updates[-1]
    assert [item["range"] for item in updates] == ["A3:B3", "C3", "F3:AJ3"]
    assert updates[1]["values"] == [[expected_formula(3)]]
    assert all(item["range"] not in {"D3", "E3"} for item in updates)


@pytest.mark.asyncio
async def test_unset_team_source_saves_shift_and_clears_stale_anchor() -> None:
    manager = ShiftRegisterManager(
        make_feature_channel("shift_register"), "service.json"
    )
    configure_row_source(
        manager,
        shift_register_manager.TeamSourceResolution(
            shift_register_manager.TeamSourceStatus.UNSET
        ),
    )
    worksheet = FakeEntryWorksheet(
        current_entry_rows(entry_participant_row("alice", "Alice", "=STALE"))
    )
    shift = ShiftParser.parse_submission(make_user(), ["4-8"]).shift
    assert shift is not None

    await manager.upsert_or_delete_user_shift(
        make_user(), shift, make_shift_metadata(worksheet)
    )

    assert {"range": "C3", "values": [[""]]} in worksheet.batch_updates[-1]
    assert any(item["range"] == "F3:AJ3" for item in worksheet.batch_updates[-1])


@pytest.mark.asyncio
async def test_transient_team_source_preserves_existing_formula() -> None:
    manager = ShiftRegisterManager(
        make_feature_channel("shift_register"), "service.json"
    )
    configure_row_source(
        manager,
        shift_register_manager.TeamSourceResolution(
            shift_register_manager.TeamSourceStatus.UNRESOLVED
        ),
    )
    worksheet = FakeEntryWorksheet(
        current_entry_rows(entry_participant_row("alice", "Alice", "=KEEP"))
    )
    shift = ShiftParser.parse_submission(make_user(), ["4-8"]).shift
    assert shift is not None

    await manager.upsert_or_delete_user_shift(
        make_user(), shift, make_shift_metadata(worksheet)
    )

    assert "C3" not in [item["range"] for item in worksheet.batch_updates[-1]]
    assert worksheet.rows[2][2] == "=KEEP"


@pytest.mark.asyncio
async def test_source_change_repairs_only_changed_formula_anchors() -> None:
    manager = ShiftRegisterManager(
        make_feature_channel("shift_register"), "service.json"
    )
    configure_row_source(manager, available_team_source())
    worksheet = FakeEntryWorksheet(
        current_entry_rows(
            entry_participant_row("bob", "Bob", "=OLD"),
            entry_participant_row("alice", "Alice", expected_formula(4)),
        )
    )
    shift = ShiftParser.parse_submission(make_user(), ["4-8"]).shift
    assert shift is not None

    await manager.upsert_or_delete_user_shift(
        make_user(), shift, make_shift_metadata(worksheet)
    )

    updates = worksheet.batch_updates[-1]
    assert {"range": "C3", "values": [[expected_formula(3)]]} in updates
    assert sum(item["range"] == "C4" for item in updates) == 0


@pytest.mark.asyncio
async def test_repair_team_references_updates_only_changed_populated_c_cells() -> None:
    manager = ShiftRegisterManager(
        make_feature_channel("shift_register"), "service.json"
    )
    resolution = renamed_team_source()
    source = resolution.source
    assert source is not None
    worksheet = FakeEntryWorksheet(
        current_entry_rows(
            entry_participant_row("alice", "Alice", "=STALE"),
            entry_participant_row("", "", "=KEEP-EMPTY"),
        )
    )
    expected = build_team_summary_formula(
        row=3,
        sheet_url=source.config.sheet_url,
        worksheet_title="Renamed Summary",
        username_column=source.summary_columns.username,
        roles_column=source.summary_columns.roles,
        main_isv_column=source.summary_columns.main_isv,
        encore_isv_column=source.summary_columns.encore_isv,
        import_last_column=source.summary_columns.import_last_column,
    )

    changed = await manager.repair_team_references(
        make_shift_metadata(worksheet), resolution
    )

    assert changed == 1
    assert worksheet.batch_updates == [[{"range": "C3", "values": [[expected]]}]]
    assert worksheet.rows[3][2] == "=KEEP-EMPTY"


@pytest.mark.asyncio
async def test_repair_team_references_skips_write_when_current() -> None:
    manager = ShiftRegisterManager(
        make_feature_channel("shift_register"), "service.json"
    )
    worksheet = FakeEntryWorksheet(
        current_entry_rows(
            entry_participant_row("alice", "Alice", expected_formula(3)),
        )
    )

    changed = await manager.repair_team_references(
        make_shift_metadata(worksheet), available_team_source()
    )

    assert changed == 0
    assert worksheet.batch_updates == []


@pytest.mark.asyncio
async def test_select_team_source_persists_before_repair() -> None:
    manager = ShiftRegisterManager(
        make_feature_channel("shift_register"), "service.json"
    )
    resolution = available_team_source()
    events: list[object] = []

    class Config:
        team_source_feature_channel_id: int | None = None

        async def save(self, *, update_fields: list[str]) -> None:
            events.append(("save", update_fields))

    config = Config()

    async def resolve(*, team_channel_id: int | None = None) -> object:
        assert team_channel_id == 22
        return resolution

    async def fetch() -> ShiftRegisterGoogleSheetsMetadata:
        events.append("fetch")
        return make_shift_metadata(FakeEntryWorksheet(current_entry_rows()))

    async def repair(*_args: object) -> int:
        events.append("repair")
        return 0

    manager.resolve_team_source = resolve  # type: ignore[method-assign]
    manager.fetch_google_sheets_metadata = fetch  # type: ignore[method-assign]
    manager.repair_team_references = repair  # type: ignore[method-assign]
    manager._sheet_config = config  # type: ignore[assignment]  # noqa: SLF001

    result = await manager.select_team_source_and_repair(22)

    assert result is resolution
    assert config.team_source_feature_channel_id == 22
    assert events == [
        ("save", ["team_source_feature_channel_id", "updated_at"]),
        "fetch",
        "repair",
    ]


@pytest.mark.asyncio
async def test_shift_delete_removes_physical_username_row() -> None:
    manager = ShiftRegisterManager(
        make_feature_channel("shift_register"), "service.json"
    )
    worksheet = FakeEntryWorksheet(
        current_entry_rows(
            entry_participant_row("alice", "Alice", manual_value="alice note"),
            entry_participant_row("bob", "Bob", manual_value="bob note"),
        )
    )

    await manager.upsert_or_delete_user_shift(
        make_user(), None, make_shift_metadata(worksheet)
    )

    assert worksheet.deleted_rows == [3]
    assert worksheet.rows[2][0] == "bob"
    assert worksheet.rows[2][36] == "bob note"
    assert worksheet.batch_updates == []


@pytest.mark.asyncio
async def test_shift_manager_reuses_first_blank_username_row() -> None:
    manager = ShiftRegisterManager(
        make_feature_channel("shift_register"), "service.json"
    )
    configure_row_source(manager, available_team_source())
    worksheet = FakeEntryWorksheet(
        current_entry_rows(
            entry_participant_row("bob", "Bob", expected_formula(3)),
            entry_participant_row("", "", manual_value="prepared"),
            entry_participant_row("carol", "Carol", expected_formula(5)),
        )
    )
    shift = ShiftParser.parse_submission(make_user(), ["4-8"]).shift
    assert shift is not None

    await manager.upsert_or_delete_user_shift(
        make_user(), shift, make_shift_metadata(worksheet)
    )

    assert worksheet.rows[3][0] == "alice"
    assert worksheet.rows[3][36] == "prepared"


@pytest.mark.asyncio
async def test_shift_manager_repairs_migration_ready_header_and_count() -> None:
    manager = ShiftRegisterManager(
        make_feature_channel("shift_register"), "service.json"
    )
    configure_row_source(
        manager,
        shift_register_manager.TeamSourceResolution(
            shift_register_manager.TeamSourceStatus.MISSING
        ),
    )
    count_row = EntryWorksheetContent.count_row()
    count_row[5] = "=WRONG"
    migration_header = [
        "username",
        "display_name",
        "",
        "",
        "",
        *EntryWorksheetContent.HOUR_COLUMNS,
        "original_message",
    ]
    worksheet = FakeEntryWorksheet([count_row, migration_header])
    shift = ShiftParser.parse_submission(make_user(), ["4-8"]).shift
    assert shift is not None

    await manager.upsert_or_delete_user_shift(
        make_user(), shift, make_shift_metadata(worksheet)
    )

    assert len(worksheet.typed_batch_updates) == 1
    assert [item["range"] for item in worksheet.typed_batch_updates[0]][:2] == [
        "A1:AJ1",
        "A2:AJ2",
    ]
    assert worksheet.rows[0][:36] == EntryWorksheetContent.count_row()
    assert worksheet.rows[1][:36] == EntryWorksheetContent.COLUMNS


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "rows",
    [
        [["username", "display_name"]],
        current_entry_rows(
            entry_participant_row("alice", "Alice"),
            entry_participant_row("alice", "Duplicate"),
        ),
        [["count", "unexpected"], EntryWorksheetContent.COLUMNS],
    ],
    ids=["legacy-row-one-header", "duplicate-username", "invalid-count-band"],
)
async def test_shift_manager_rejects_malformed_entry_before_update(
    rows: list[list[object]],
) -> None:
    manager = ShiftRegisterManager(
        make_feature_channel("shift_register"), "service.json"
    )
    worksheet = FakeEntryWorksheet(rows)
    shift = ShiftParser.parse_submission(make_user(), ["4-8"]).shift
    assert shift is not None

    with pytest.raises(StorageError) as exc_info:
        await manager.upsert_or_delete_user_shift(
            make_user(), shift, make_shift_metadata(worksheet)
        )

    assert exc_info.value.kind is StorageErrorKind.MALFORMED_SHEET
    assert isinstance(exc_info.value.__cause__, ValueError)
    assert worksheet.batch_updates == []
    assert worksheet.deleted_rows == []


@pytest.mark.asyncio
async def test_manager_skips_missing_worksheets_without_updates() -> None:
    team_manager = TeamRegisterManager(
        make_feature_channel("team_register"), "service.json"
    )
    shift_manager = ShiftRegisterManager(
        make_feature_channel("shift_register"), "service.json"
    )
    metadata = ShiftRegisterGoogleSheetsMetadata(
        "https://sheet.example",
        [
            EntryWorksheetMetadata(1, "Shift Entry", None),
            DraftWorksheetMetadata(2, "Shift Draft", None),
            FinalScheduleWorksheetMetadata(3, "Shift Final Schedule", None),
        ],
    )

    await team_manager.upsert_or_delete_user_team(make_user(), None, None)
    await shift_manager.upsert_or_delete_user_shift(make_user(), None, metadata)

    assert isinstance(metadata.sheet_url, str)


@pytest.mark.asyncio
async def test_team_summary_refresh_uses_title_for_each_existing_sheet() -> None:
    manager = TeamRegisterManager(make_feature_channel("team_register"), "service.json")
    manager._sheet_config = SimpleNamespace(encore_role_ids=[])  # noqa: SLF001
    user = make_user()

    main_content = TeamWorksheetContent()
    backup_content = TeamWorksheetContent()
    main_content.upsert(TeamParser.parse_line(user, "150/740/33.4 main"))
    backup_content.upsert(TeamParser.parse_line(user, "130/600/30.0 backup"))

    summary_worksheet = FakeWorksheet(title="Team Summary")
    metadata = TeamRegisterGoogleSheetsMetadata.from_subtyped_worksheets(
        "https://sheet.example",
        [
            TeamWorksheetMetadata(
                1, "Main Team", FakeWorksheet(frame=main_content.main)
            ),
            TeamWorksheetMetadata(2, "Encore Team", None),
            TeamWorksheetMetadata(
                3,
                "Backup Team",
                FakeWorksheet(frame=backup_content.main),
            ),
            SummaryWorksheetMetadata(4, "Team Summary", summary_worksheet),
        ],
    )

    await manager.refresh_summary_worksheet(metadata, {})

    refreshed = summary_worksheet.updated_frames[-1]
    assert Summary.isv_title("Backup Team") in refreshed.columns
    assert Summary.isv_title("Encore Team") not in refreshed.columns
    assert refreshed.loc[0, Summary.isv_title("Backup Team")] == 224


def test_fake_worksheet_returns_copies() -> None:
    original = pd.DataFrame({"username": ["alice"]})
    worksheet = FakeWorksheet(frame=original)

    original.loc[0, "username"] = "changed"

    assert worksheet.frame.loc[0, "username"] == "alice"
