from __future__ import annotations

import copy
import re
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pandas as pd
import pytest

from models.team_register import TeamRegisterConfig
from tests.fakes import FakeWorksheet
from utils import shift_register_manager
from utils.google_sheets_errors import GoogleSheetsError, GoogleSheetsErrorKind
from utils.shift_register_manager import ShiftRegisterManager
from utils.shift_register_structs import (
    DraftWorksheetMetadata,
    EntryWorksheetContent,
    EntryWorksheetMetadata,
    FinalScheduleWorksheetMetadata,
    ShiftParser,
    ShiftRegisterGoogleSheetsMetadata,
    build_team_summary_formula,
)
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


def make_feature_channel(feature_name: str) -> SimpleNamespace:
    return SimpleNamespace(guild_id=1, channel_id=2, feature_name=feature_name)


def make_user(username: str = "alice", display_name: str = "Alice") -> UserInfo:
    return UserInfo(username=username, display_name=display_name)


class FakeTeamConfigQuery:
    def __init__(self, configs: list[SimpleNamespace]) -> None:
        self.configs = configs
        self.selected_related: tuple[str, ...] = ()

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
    ) -> None:
        self.id = worksheet_id
        self.title = title
        self.header = header

    async def batch_get_values(self, ranges: list[str]) -> list[list[list[object]]]:
        assert ranges == ["1:1"]
        return [[self.header]] if self.header is not None else [[]]


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

    def filter_configs(**kwargs: object) -> FakeTeamConfigQuery:
        assert kwargs == {"feature_channel__guild_id": manager.feature_channel.guild_id}
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
) -> SimpleNamespace:
    return SimpleNamespace(
        sheet_url="https://team.sheet.example",
        team_worksheet_ids=team_worksheet_ids or [101, 102],
        summary_worksheet_id=201,
        feature_channel=SimpleNamespace(channel_id=22),
    )


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


def available_team_source() -> shift_register_manager.TeamSummarySourceResolution:
    return shift_register_manager.TeamSummarySourceResolution(
        shift_register_manager.TeamSummarySourceStatus.AVAILABLE,
        shift_register_manager.TeamSummaryFormulaSource(
            channel_id=22,
            sheet_url="https://team.sheet.example",
            worksheet_id=201,
            worksheet_title="Team Summary",
            username_column=1,
            roles_column=3,
            main_isv_column=4,
            encore_isv_column=6,
            import_last_column="G",
        ),
    )


def configure_row_source(
    manager: ShiftRegisterManager,
    resolution: shift_register_manager.TeamSummarySourceResolution,
) -> None:
    async def resolve() -> shift_register_manager.TeamSummarySourceResolution:
        return resolution

    manager.resolve_team_summary_source = resolve  # type: ignore[method-assign]


def current_entry_rows(*participant_rows: list[object]) -> list[list[object]]:
    return [
        EntryWorksheetContent.count_row(),
        EntryWorksheetContent.COLUMNS,
        *participant_rows,
    ]


def expected_formula(row: int) -> str:
    source = available_team_source().source
    assert source is not None
    return build_team_summary_formula(
        row=row,
        sheet_url=source.sheet_url,
        worksheet_title=source.worksheet_title,
        username_column=source.username_column,
        roles_column=source.roles_column,
        main_isv_column=source.main_isv_column,
        encore_isv_column=source.encore_isv_column,
        import_last_column=source.import_last_column,
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
async def test_shift_manager_selects_only_same_guild_team_register(
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

    resolution = await manager.resolve_team_summary_source()

    assert resolution.status is shift_register_manager.TeamSummarySourceStatus.AVAILABLE
    assert resolution.source is not None
    assert resolution.source.channel_id == 22
    assert resolution.source.worksheet_title == "Renamed Summary"
    assert resolution.source.main_isv_column == 4
    assert resolution.source.encore_isv_column == 6
    assert resolution.source.import_last_column == "G"
    assert query.selected_related == ("feature_channel",)


@pytest.mark.asyncio
async def test_shift_manager_resolves_main_only_team_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = ShiftRegisterManager(
        make_feature_channel("shift_register"), "service.json"
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

    resolution = await manager.resolve_team_summary_source()

    assert resolution.status is shift_register_manager.TeamSummarySourceStatus.AVAILABLE
    assert resolution.source is not None
    assert resolution.source.encore_isv_column is None
    assert resolution.source.import_last_column == "E"


@pytest.mark.asyncio
async def test_shift_manager_reports_missing_team_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = ShiftRegisterManager(
        make_feature_channel("shift_register"), "service.json"
    )
    configure_team_source_query(monkeypatch, manager=manager, configs=[])

    resolution = await manager.resolve_team_summary_source()

    assert resolution.status is shift_register_manager.TeamSummarySourceStatus.MISSING
    assert resolution.source is None


@pytest.mark.asyncio
async def test_shift_manager_reports_ambiguous_team_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = ShiftRegisterManager(
        make_feature_channel("shift_register"), "service.json"
    )
    configure_team_source_query(
        monkeypatch,
        manager=manager,
        configs=[make_team_source_config(), make_team_source_config()],
    )

    resolution = await manager.resolve_team_summary_source()

    assert resolution.status is shift_register_manager.TeamSummarySourceStatus.AMBIGUOUS
    assert resolution.source is None


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
    configure_team_source_query(
        monkeypatch,
        manager=manager,
        configs=[make_team_source_config()],
        source_sheet=FakeTeamSourceSheet(worksheets),
    )

    resolution = await manager.resolve_team_summary_source()

    assert resolution.status is shift_register_manager.TeamSummarySourceStatus.INVALID
    assert resolution.source is None


@pytest.mark.asyncio
async def test_shift_manager_reports_transient_team_source_as_unresolved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = ShiftRegisterManager(
        make_feature_channel("shift_register"), "service.json"
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

    resolution = await manager.resolve_team_summary_source()

    assert (
        resolution.status is shift_register_manager.TeamSummarySourceStatus.UNRESOLVED
    )
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
async def test_shift_manager_initializes_empty_entry_worksheet() -> None:
    manager = ShiftRegisterManager(
        make_feature_channel("shift_register"), "service.json"
    )
    configure_row_source(
        manager,
        shift_register_manager.TeamSummarySourceResolution(
            shift_register_manager.TeamSummarySourceStatus.MISSING
        ),
    )
    worksheet = FakeEntryWorksheet(rows=[], row_count=2, col_count=20)
    shift = ShiftParser.parse_submission(make_user(), ["4-8"]).shift
    assert shift is not None

    await manager.upsert_or_delete_user_shift(
        make_user(), shift, make_shift_metadata(worksheet)
    )

    ranges = [item["range"] for item in worksheet.batch_updates[-1]]
    assert ranges == ["A1:AJ1", "A2:AJ2", "A3:B3", "F3:AJ3"]
    assert worksheet.rows[0][:36] == EntryWorksheetContent.count_row()
    assert worksheet.rows[1][:36] == EntryWorksheetContent.COLUMNS
    assert worksheet.rows[2][0:2] == ["alice", "Alice"]
    assert worksheet.rows[2][9] == 1
    assert worksheet.rows[2][13] == 0
    assert worksheet.ensure_calls == [(3, 36)]


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
async def test_missing_team_source_saves_shift_and_clears_stale_anchor() -> None:
    manager = ShiftRegisterManager(
        make_feature_channel("shift_register"), "service.json"
    )
    configure_row_source(
        manager,
        shift_register_manager.TeamSummarySourceResolution(
            shift_register_manager.TeamSummarySourceStatus.MISSING
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
        shift_register_manager.TeamSummarySourceResolution(
            shift_register_manager.TeamSummarySourceStatus.UNRESOLVED
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
        shift_register_manager.TeamSummarySourceResolution(
            shift_register_manager.TeamSummarySourceStatus.MISSING
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

    ranges = [item["range"] for item in worksheet.batch_updates[-1]]
    assert ranges[:2] == ["A1:AJ1", "A2:AJ2"]
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
