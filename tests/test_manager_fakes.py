from __future__ import annotations

import asyncio
import copy
import re
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock

import pytest

from components.ui_team_register import build_summary_embed
from models.team_register import TeamRegisterConfig
from utils import (
    manager_base as manager_base_module,
    shift_register_manager,
    team_register_manager as team_register_manager_module,
)
from utils.google_sheets import DimensionMutation, GridValueUpdate
from utils.google_sheets_errors import GoogleSheetsError, GoogleSheetsErrorKind
from utils.key_async_lock import KeyAsyncLock
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
from utils.structs_base import UserInfo, WorksheetContractError
from utils.team_register_manager import TeamRegisterManager
from utils.team_register_structs import (
    SummaryWorksheetContent,
    SummaryWorksheetMetadata,
    TeamParser,
    TeamRegisterGoogleSheetsMetadata,
    TeamWorksheetContent,
    TeamWorksheetMetadata,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Generator
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


def _column_index(letters: str) -> int:
    index = 0
    for letter in letters:
        index = index * 26 + ord(letter) - ord("A") + 1
    return index


class FakeTeamGridWorksheet:
    def __init__(
        self,
        worksheet_id: int,
        title: str,
        values: list[list[object]],
        *,
        row_count: int = 100,
        col_count: int = 20,
    ) -> None:
        self.id = worksheet_id
        self.title = title
        self.values = copy.deepcopy(values)
        self.row_count = row_count
        self.col_count = col_count


class FakeTeamGridSheet:
    def __init__(
        self,
        worksheets: list[FakeTeamGridWorksheet],
        *,
        batch_error: Exception | None = None,
        create_error_title: str | None = None,
    ) -> None:
        self.sheet_url = "https://docs.google.com/spreadsheets/d/team-transaction/edit"
        self.worksheet_by_id = {worksheet.id: worksheet for worksheet in worksheets}
        self.batch_reads: list[list[int]] = []
        self.batch_updates: list[list[object]] = []
        self.created_titles: list[str] = []
        self.create_calls: list[str] = []
        self.batch_error = batch_error
        self.create_error_title = create_error_title

    @property
    async def sheet(self) -> FakeTeamGridSheet:
        return self

    async def worksheets(self) -> list[FakeTeamGridWorksheet]:
        return list(self.worksheet_by_id.values())

    async def get_worksheets(
        self,
        worksheet_ids: list[int],
    ) -> dict[int, FakeTeamGridWorksheet | None]:
        return {
            worksheet_id: self.worksheet_by_id.get(worksheet_id)
            for worksheet_id in worksheet_ids
        }

    async def batch_get_worksheet_values(
        self,
        worksheets: list[FakeTeamGridWorksheet],
    ) -> dict[int, list[list[object]]]:
        self.batch_reads.append([worksheet.id for worksheet in worksheets])
        return {
            worksheet.id: copy.deepcopy(worksheet.values) for worksheet in worksheets
        }

    async def get_or_create_worksheets(
        self,
        worksheet_titles: list[str],
    ) -> dict[str, FakeTeamGridWorksheet]:
        return {
            title: await self.get_or_create_worksheet(title)
            for title in worksheet_titles
        }

    async def get_or_create_worksheet(
        self,
        title: str,
    ) -> FakeTeamGridWorksheet:
        self.create_calls.append(title)
        if title == self.create_error_title:
            raise GoogleSheetsError(
                GoogleSheetsErrorKind.TRANSIENT,
                "private create detail",
            )
        by_title = {
            worksheet.title: worksheet for worksheet in self.worksheet_by_id.values()
        }
        if title not in by_title:
            worksheet_id = max(self.worksheet_by_id, default=300) + 1
            worksheet = FakeTeamGridWorksheet(worksheet_id, title, [])
            self.worksheet_by_id[worksheet_id] = worksheet
            by_title[title] = worksheet
            self.created_titles.append(title)
        return by_title[title]

    async def batch_update_grid(self, mutations: list[object]) -> None:
        self.batch_updates.append(list(mutations))
        if self.batch_error is not None:
            raise self.batch_error


def configure_team_transaction_manager(
    manager: TeamRegisterManager,
    sheet: FakeTeamGridSheet,
    *,
    team_worksheet_ids: list[int],
    summary_worksheet_id: int,
) -> None:
    worksheet_ids = [*team_worksheet_ids, summary_worksheet_id]
    manager._sheet_config = SimpleNamespace(  # noqa: SLF001
        sheet_url=sheet.sheet_url,
        team_worksheet_ids=team_worksheet_ids,
        summary_worksheet_id=summary_worksheet_id,
        encore_role_ids=[],
        get_worksheet_ids=lambda: worksheet_ids,
    )
    manager._google_sheet = sheet  # type: ignore[assignment]  # noqa: SLF001


def configure_team_settings_config(
    manager: TeamRegisterManager,
    *,
    encore_role_ids: list[int] | None = None,
) -> None:
    config = SimpleNamespace(encore_role_ids=encore_role_ids or [])
    manager._sheet_config = config  # type: ignore[assignment]  # noqa: SLF001
    manager.get_fresh_sheet_config = AsyncMock(  # type: ignore[method-assign]
        return_value=config
    )


def make_encore_reconciliation_manager(
    *,
    malformed_later_row: bool = False,
    batch_error: Exception | None = None,
) -> tuple[TeamRegisterManager, FakeTeamGridSheet, SimpleNamespace]:
    manager = TeamRegisterManager(make_feature_channel("team_register"), "service.json")
    team_rows: list[list[object]] = [["alice", "Alice", 150, 740, 33.4, "main"]]
    if malformed_later_row:
        team_rows.append(["bob", "Bob", "not-int", 700, 31.2, "bad"])
    main_worksheet = FakeTeamGridWorksheet(
        101,
        "Main Team",
        [TeamWorksheetContent.COLUMNS, *team_rows],
    )
    summary_worksheet = FakeTeamGridWorksheet(
        201,
        "Team Summary",
        [
            [
                *SummaryWorksheetContent.COLUMNS,
                "Main Team ISV",
                "Main Team Power",
                "original_message",
            ],
            ["alice", "Alice", "", 183, 33.4, "main"],
        ],
    )
    sheet = FakeTeamGridSheet(
        [main_worksheet, summary_worksheet],
        batch_error=batch_error,
    )
    configure_team_transaction_manager(
        manager,
        sheet,
        team_worksheet_ids=[101],
        summary_worksheet_id=201,
    )
    config = manager._sheet_config  # noqa: SLF001
    config.save = AsyncMock()
    return manager, sheet, config


class RecordingKeyLock:
    def __init__(self, label: str, events: list[tuple[str, object]]) -> None:
        self.label = label
        self.events = events

    @asynccontextmanager
    async def __call__(self, key: object) -> AsyncIterator[None]:
        self.events.append((f"enter_{self.label}", key))
        try:
            yield
        finally:
            self.events.append((f"exit_{self.label}", key))


@pytest.mark.asyncio
async def test_worksheet_transactions_dedupe_and_sort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[tuple[str, object]] = []
    lock = RecordingKeyLock("worksheet", events)
    monkeypatch.setattr(
        manager_base_module,
        "WORKSHEET_TRANSACTION_LOCK",
        lock,
        raising=False,
    )

    async with manager_base_module.worksheet_transactions(
        [("sheet-b", 2), ("sheet-a", 9), ("sheet-a", 1), ("sheet-a", 1)]
    ):
        events.append(("inside", None))

    assert [event for event in events if event[0] == "enter_worksheet"] == [
        ("enter_worksheet", ("sheet-a", 1)),
        ("enter_worksheet", ("sheet-a", 9)),
        ("enter_worksheet", ("sheet-b", 2)),
    ]


async def worksheet_transactions_overlap(
    monkeypatch: pytest.MonkeyPatch,
    first_resources: list[tuple[str, int]],
    second_resources: list[tuple[str, int]],
) -> bool:
    monkeypatch.setattr(
        manager_base_module,
        "WORKSHEET_TRANSACTION_LOCK",
        KeyAsyncLock(),
        raising=False,
    )
    first_entered = asyncio.Event()
    second_attempted = asyncio.Event()
    second_entered = asyncio.Event()
    release_first = asyncio.Event()

    async def first() -> None:
        async with manager_base_module.worksheet_transactions(first_resources):
            first_entered.set()
            await release_first.wait()

    async def second() -> None:
        second_attempted.set()
        async with manager_base_module.worksheet_transactions(second_resources):
            second_entered.set()

    first_task = asyncio.create_task(first())
    await first_entered.wait()
    second_task = asyncio.create_task(second())
    await second_attempted.wait()
    await asyncio.sleep(0)
    overlap = second_entered.is_set()
    release_first.set()
    await asyncio.gather(first_task, second_task)
    return overlap


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scenario",
    [
        ([("sheet", 1)], [("sheet", 1)], False),
        ([("sheet", 1)], [("sheet", 2)], True),
        ([("sheet-a", 1)], [("sheet-b", 1)], True),
    ],
)
async def test_worksheet_transaction_keyed_concurrency(
    monkeypatch: pytest.MonkeyPatch,
    scenario: tuple[list[tuple[str, int]], list[tuple[str, int]], bool],
) -> None:
    assert (
        await worksheet_transactions_overlap(
            monkeypatch,
            scenario[0],
            scenario[1],
        )
        is scenario[2]
    )


@pytest.mark.asyncio
async def test_worksheet_transaction_cancellation_releases_acquired_resources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    second_attempted = asyncio.Event()
    released: list[tuple[str, int]] = []

    class BlockingSecondResourceLock:
        @asynccontextmanager
        async def __call__(
            self,
            key: tuple[str, int],
        ) -> AsyncIterator[None]:
            if key == ("sheet", 2):
                second_attempted.set()
                await asyncio.Event().wait()
            try:
                yield
            finally:
                released.append(key)

    monkeypatch.setattr(
        manager_base_module,
        "WORKSHEET_TRANSACTION_LOCK",
        BlockingSecondResourceLock(),
        raising=False,
    )

    async def transaction() -> None:
        async with manager_base_module.worksheet_transactions(
            [("sheet", 1), ("sheet", 2)]
        ):
            raise AssertionError

    task = asyncio.create_task(transaction())
    await second_attempted.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert released == [("sheet", 1)]


@pytest.mark.asyncio
async def test_fresh_shift_transaction_only_locks_channel() -> None:
    events: list[tuple[str, object]] = []
    manager = ShiftRegisterManager(
        make_feature_channel("shift_register", channel_id=22), "service.json"
    )
    config = SimpleNamespace(
        sheet_url="https://docs.google.com/spreadsheets/d/current-shift/edit"
    )

    async def get_fresh_sheet_config() -> SimpleNamespace:
        events.append(("fresh_config", None))
        return config

    manager.get_fresh_sheet_config = get_fresh_sheet_config  # type: ignore[method-assign]
    channel_lock = RecordingKeyLock("channel", events)

    async with shift_register_manager.fresh_shift_channel_transaction(
        manager,
        channel_lock,
        channel_id=22,
    ) as current_config:
        assert current_config is config
        events.append(("inside", None))

    assert events == [
        ("enter_channel", 22),
        ("fresh_config", None),
        ("inside", None),
        ("exit_channel", 22),
    ]


@pytest.mark.asyncio
async def test_fresh_team_transaction_only_locks_channel() -> None:
    events: list[tuple[str, object]] = []
    manager = TeamRegisterManager(
        make_feature_channel("team_register", channel_id=22),
        "service.json",
    )
    config = SimpleNamespace(sheet_url="invalid saved URL")

    async def get_fresh_sheet_config() -> SimpleNamespace:
        events.append(("fresh_config", None))
        return config

    manager.get_fresh_sheet_config = get_fresh_sheet_config  # type: ignore[method-assign]
    channel_lock = RecordingKeyLock("channel", events)

    async with team_register_manager_module.fresh_team_channel_transaction(
        manager,
        channel_lock,
        channel_id=22,
    ) as current_config:
        assert current_config is config
        events.append(("inside", None))

    assert events == [
        ("enter_channel", 22),
        ("fresh_config", None),
        ("inside", None),
        ("exit_channel", 22),
    ]


@pytest.mark.asyncio
async def test_cross_feature_disjoint_overlap_and_shared_summary_waits() -> None:
    team_entered = asyncio.Event()
    shift_entered = asyncio.Event()
    shared_attempted = asyncio.Event()
    shared_entered = asyncio.Event()
    release_team = asyncio.Event()
    release_shift = asyncio.Event()

    async def team_registration() -> None:
        async with manager_base_module.worksheet_transactions(
            [("shared-sheet", 101), ("shared-sheet", 201)]
        ):
            team_entered.set()
            await release_team.wait()

    async def shift_registration_without_team_source() -> None:
        async with manager_base_module.worksheet_transactions([("shared-sheet", 301)]):
            shift_entered.set()
            await release_shift.wait()

    async def summary_consumer() -> None:
        shared_attempted.set()
        async with manager_base_module.worksheet_transactions([("shared-sheet", 201)]):
            shared_entered.set()

    team_task = asyncio.create_task(team_registration())
    await team_entered.wait()
    shift_task = asyncio.create_task(shift_registration_without_team_source())
    await shift_entered.wait()
    shared_task = asyncio.create_task(summary_consumer())
    await shared_attempted.wait()

    assert not shared_entered.is_set()
    release_team.set()
    await shared_entered.wait()
    release_shift.set()
    await asyncio.gather(team_task, shift_task, shared_task)


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
        self.values = [list(header), *(rows or [])] if header is not None else []


class FakeTeamSourceSheet:
    def __init__(
        self,
        worksheets: list[FakeTeamSourceWorksheet],
        *,
        error: GoogleSheetsError | None = None,
        read_error: GoogleSheetsError | None = None,
    ) -> None:
        self.worksheets = {worksheet.id: worksheet for worksheet in worksheets}
        self.error = error
        self.read_error = read_error
        self.batch_reads: list[list[int]] = []

    async def get_worksheets(
        self, worksheet_ids: list[int]
    ) -> dict[int, FakeTeamSourceWorksheet | None]:
        if self.error is not None:
            raise self.error
        return {
            worksheet_id: self.worksheets.get(worksheet_id)
            for worksheet_id in worksheet_ids
        }

    async def batch_get_worksheet_values(
        self,
        worksheets: list[FakeTeamSourceWorksheet],
    ) -> dict[int, list[list[object]]]:
        self.batch_reads.append([worksheet.id for worksheet in worksheets])
        if self.read_error is not None:
            raise self.read_error
        return {
            worksheet.id: copy.deepcopy(worksheet.values) for worksheet in worksheets
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
        sheet_url="https://docs.google.com/spreadsheets/d/team-source/edit",
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
    "original_message",
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
        self.batch_updates: list[list[dict[str, object]]] = []
        self.typed_batch_updates: list[list[dict[str, object]]] = []
        self.typed_formula_ranges: list[set[str]] = []
        self.conditional_format_rules: list[dict[str, object]] = []
        self.presentation_updates: list[dict[str, object]] = []
        self.typed_minimums: list[tuple[int | None, int | None]] = []
        self.deleted_rows: list[int] = []

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
        min_rows: int | None = None,
        min_cols: int | None = None,
    ) -> None:
        copied = copy.deepcopy(data)
        self.batch_updates.append(copied)
        self.typed_batch_updates.append(copied)
        self.typed_formula_ranges.append(formula_ranges)
        self.typed_minimums.append((min_rows, min_cols))
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

    async def delete_row(self, index: int) -> None:
        self.deleted_rows.append(index)
        if index <= len(self.rows):
            self.rows.pop(index - 1)
        self.row_count -= 1

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


class FakeShiftValueSheet:
    sheet_url = "https://docs.google.com/spreadsheets/d/shift-transaction/edit"

    def __init__(self) -> None:
        self.batch_reads: list[list[int]] = []

    async def batch_get_worksheet_values(
        self,
        worksheets: list[object],
    ) -> dict[int, list[list[object]]]:
        self.batch_reads.append([worksheet.id for worksheet in worksheets])  # type: ignore[attr-defined]
        return {
            worksheet.id: copy.deepcopy(  # type: ignore[attr-defined]
                getattr(worksheet, "rows", getattr(worksheet, "values", []))
            )
            for worksheet in worksheets
        }


class FakeShiftSetupGoogleSheet(FakeShiftValueSheet):
    sheet_url = "https://docs.google.com/spreadsheets/d/shift-settings/edit"

    def __init__(self, worksheets: list[FakeEntryWorksheet]) -> None:
        super().__init__()
        self.worksheet_by_title = {
            worksheet.title: worksheet for worksheet in worksheets
        }
        self.calls: list[list[str]] = []
        self.created_titles: list[str] = []

    async def get_or_create_worksheets(
        self,
        worksheet_titles: list[str],
        *,
        creation_status: object | None = None,
    ) -> dict[str, FakeEntryWorksheet]:
        self.calls.append(list(worksheet_titles))
        result = {}
        for title in worksheet_titles:
            worksheet = self.worksheet_by_title.get(title)
            if worksheet is None:
                worksheet = FakeEntryWorksheet(
                    title=title,
                    worksheet_id=max(
                        (item.id for item in self.worksheet_by_title.values()),
                        default=0,
                    )
                    + 1,
                )
                self.worksheet_by_title[title] = worksheet
                self.created_titles.append(title)
                if creation_status is not None:
                    creation_status.created = True
            result[title] = worksheet
        return result


def make_shift_metadata(
    worksheet: FakeEntryWorksheet | None,
) -> ShiftRegisterGoogleSheetsMetadata:
    return ShiftRegisterGoogleSheetsMetadata(
        "https://docs.google.com/spreadsheets/d/shift-transaction/edit",
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
                import_last_column="H",
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
) -> FakeShiftValueSheet:
    async def resolve() -> shift_register_manager.TeamSourceResolution:
        return resolution

    async def resolve_metadata() -> tuple[object, object, object]:
        source = resolution.source
        return (
            resolution.status,
            source.config if source is not None else None,
            source.metadata if source is not None else None,
        )

    sheet = FakeShiftValueSheet()

    async def read_locked(
        _shift_sheet_url: str,
        shift_worksheets: list[FakeEntryWorksheet],
        _source_status: object,
        _source_config: object,
        _source_metadata: object,
    ) -> tuple[
        dict[int, list[list[object]]],
        shift_register_manager.TeamSourceResolution,
        None,
    ]:
        grids = await sheet.batch_get_worksheet_values(shift_worksheets)
        return grids, resolution, None

    manager.resolve_team_source = resolve  # type: ignore[method-assign]
    manager._resolve_team_source_metadata = resolve_metadata  # type: ignore[method-assign]  # noqa: SLF001
    manager._read_shift_and_team_source_locked = read_locked  # type: ignore[method-assign]  # noqa: SLF001
    manager._google_sheet = sheet  # type: ignore[assignment]  # noqa: SLF001
    return sheet


def configure_shift_value_sheet(
    manager: ShiftRegisterManager,
) -> FakeShiftValueSheet:
    sheet = FakeShiftValueSheet()
    if manager._sheet_config is None:  # noqa: SLF001
        manager._sheet_config = SimpleNamespace(  # noqa: SLF001
            team_source_feature_channel_id=None
        )
    manager._google_sheet = sheet  # type: ignore[assignment]  # noqa: SLF001
    return sheet


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
                        "original_message",
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
    source_sheet = FakeTeamSourceSheet(
        [
            FakeTeamSourceWorksheet(101, "Main Team"),
            FakeTeamSourceWorksheet(102, "Encore Team"),
            summary,
        ]
    )
    configure_team_source_query(
        monkeypatch,
        manager=manager,
        configs=[make_team_source_config()],
        source_sheet=source_sheet,
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
        sheet_url="https://docs.google.com/spreadsheets/d/team-source/edit",
        worksheet_title="Team Summary",
        import_last_column="H",
        username_header="username",
        roles_header="encore_roles",
        main_isv_header="Main Team ISV",
        main_power_header="Main Team Power",
        encore_isv_header="Encore Team ISV",
        encore_power_header="Encore Team Power",
    )
    assert source_sheet.batch_reads == [[201]]


@pytest.mark.asyncio
async def test_shift_manager_coalesces_same_spreadsheet_source_grid() -> None:
    manager = ShiftRegisterManager(
        make_feature_channel("shift_register"), "service.json"
    )
    sheet = FakeShiftValueSheet()
    manager._google_sheet = sheet  # type: ignore[assignment]  # noqa: SLF001
    entry = FakeEntryWorksheet(current_entry_rows())
    main = FakeTeamSourceWorksheet(101, "Main Team")
    encore = FakeTeamSourceWorksheet(102, "Encore Team")
    summary = FakeTeamSourceWorksheet(201, "Team Summary", TEAM_SUMMARY_HEADER)
    source_config = make_team_source_config()
    source_config.sheet_url = make_shift_metadata(entry).sheet_url
    source_metadata = TeamRegisterGoogleSheetsMetadata.from_subtyped_worksheets(
        source_config.sheet_url,
        [
            TeamWorksheetMetadata(101, "Main Team", main),
            TeamWorksheetMetadata(102, "Encore Team", encore),
            SummaryWorksheetMetadata(201, "Team Summary", summary),
        ],
    )

    grids, resolution, summary_grid = await manager._read_shift_and_team_source_locked(  # noqa: SLF001
        source_config.sheet_url,
        [entry],
        shift_register_manager.TeamSourceStatus.AVAILABLE,
        source_config,
        source_metadata,
    )

    assert sheet.batch_reads == [[1, 201]]
    assert grids[1] == current_entry_rows()
    assert summary_grid == [TEAM_SUMMARY_HEADER]
    assert resolution.status is shift_register_manager.TeamSourceStatus.AVAILABLE


@pytest.mark.asyncio
async def test_shift_manager_batches_external_source_grid_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = ShiftRegisterManager(
        make_feature_channel("shift_register"), "service.json"
    )
    shift_sheet = FakeShiftValueSheet()
    manager._google_sheet = shift_sheet  # type: ignore[assignment]  # noqa: SLF001
    entry = FakeEntryWorksheet(current_entry_rows())
    draft = FakeEntryWorksheet([], title="Shift Draft", worksheet_id=2)
    main = FakeTeamSourceWorksheet(101, "Main Team")
    encore = FakeTeamSourceWorksheet(102, "Encore Team")
    summary = FakeTeamSourceWorksheet(201, "Team Summary", TEAM_SUMMARY_HEADER)
    source_config = make_team_source_config()
    source_metadata = TeamRegisterGoogleSheetsMetadata.from_subtyped_worksheets(
        source_config.sheet_url,
        [
            TeamWorksheetMetadata(101, "Main Team", main),
            TeamWorksheetMetadata(102, "Encore Team", encore),
            SummaryWorksheetMetadata(201, "Team Summary", summary),
        ],
    )
    source_sheet = FakeTeamSourceSheet([main, encore, summary])
    monkeypatch.setattr(
        shift_register_manager,
        "GoogleSheet",
        lambda _url, _path: source_sheet,
    )

    grids, resolution, summary_grid = await manager._read_shift_and_team_source_locked(  # noqa: SLF001
        make_shift_metadata(entry).sheet_url,
        [entry, draft],
        shift_register_manager.TeamSourceStatus.AVAILABLE,
        source_config,
        source_metadata,
    )

    assert shift_sheet.batch_reads == [[1, 2]]
    assert source_sheet.batch_reads == [[201]]
    assert grids[1] == current_entry_rows()
    assert summary_grid == [TEAM_SUMMARY_HEADER]
    assert resolution.status is shift_register_manager.TeamSourceStatus.AVAILABLE


@pytest.mark.asyncio
async def test_shift_manager_keeps_shift_grid_when_external_source_read_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = ShiftRegisterManager(
        make_feature_channel("shift_register"), "service.json"
    )
    shift_sheet = FakeShiftValueSheet()
    manager._google_sheet = shift_sheet  # type: ignore[assignment]  # noqa: SLF001
    entry = FakeEntryWorksheet(current_entry_rows())
    main = FakeTeamSourceWorksheet(101, "Main Team")
    encore = FakeTeamSourceWorksheet(102, "Encore Team")
    summary = FakeTeamSourceWorksheet(201, "Team Summary", TEAM_SUMMARY_HEADER)
    source_config = make_team_source_config()
    source_metadata = TeamRegisterGoogleSheetsMetadata.from_subtyped_worksheets(
        source_config.sheet_url,
        [
            TeamWorksheetMetadata(101, "Main Team", main),
            TeamWorksheetMetadata(102, "Encore Team", encore),
            SummaryWorksheetMetadata(201, "Team Summary", summary),
        ],
    )
    source_sheet = FakeTeamSourceSheet(
        [main, encore, summary],
        read_error=GoogleSheetsError(
            GoogleSheetsErrorKind.TRANSIENT,
            "private detail",
        ),
    )
    monkeypatch.setattr(
        shift_register_manager,
        "GoogleSheet",
        lambda _url, _path: source_sheet,
    )

    grids, resolution, summary_grid = await manager._read_shift_and_team_source_locked(  # noqa: SLF001
        make_shift_metadata(entry).sheet_url,
        [entry],
        shift_register_manager.TeamSourceStatus.AVAILABLE,
        source_config,
        source_metadata,
    )

    assert shift_sheet.batch_reads == [[1]]
    assert source_sheet.batch_reads == [[201]]
    assert grids[1] == current_entry_rows()
    assert resolution.status is shift_register_manager.TeamSourceStatus.UNRESOLVED
    assert summary_grid is None


@pytest.mark.asyncio
async def test_shift_manager_ignores_admin_values_after_summary_terminal(
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
        [*TEAM_SUMMARY_HEADER, "private_admin_header"],
        [
            [
                "alice",
                "Alice",
                "Encore",
                200,
                40,
                250,
                50,
                "same",
                "ADMIN_SENTINEL_MUST_NOT_BE_READ",
            ]
        ],
    )
    source_sheet = FakeTeamSourceSheet(
        [
            FakeTeamSourceWorksheet(101, "Main Team"),
            FakeTeamSourceWorksheet(102, "Encore Team"),
            summary,
        ]
    )
    configure_team_source_query(
        monkeypatch,
        manager=manager,
        configs=[make_team_source_config()],
        source_sheet=source_sheet,
    )

    resolution = await manager.resolve_draft_team_profiles()

    assert resolution.status is shift_register_manager.TeamSourceStatus.AVAILABLE
    assert resolution.notes_team_source is not None
    assert resolution.notes_team_source.import_last_column == "H"
    assert resolution.profiles["alice"].main_isv == 200
    assert source_sheet.batch_reads == [[201]]


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
                        "original_message",
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
        "_resolve_team_source_metadata",
        AsyncMock(return_value=(status, None, None)),
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
                    "original_message",
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
    assert resolution.source.summary_columns.import_last_column == "F"


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
async def test_shift_manager_rejects_team_source_headers_after_terminal(
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
        [
            "username",
            "display_name",
            "encore_roles",
            "original_message",
            "Main Team ISV",
            "Main Team Power",
            "Encore Team ISV",
            "Encore Team Power",
        ],
        [["alice", "Alice", "Encore", "same", 200, 40, 250, 50]],
    )
    source_sheet = FakeTeamSourceSheet(
        [
            FakeTeamSourceWorksheet(101, "Main Team"),
            FakeTeamSourceWorksheet(102, "Encore Team"),
            summary,
        ]
    )
    configure_team_source_query(
        monkeypatch,
        manager=manager,
        configs=[make_team_source_config()],
        source_sheet=source_sheet,
    )

    resolution = await manager.resolve_team_source()

    assert resolution.status is shift_register_manager.TeamSourceStatus.INVALID
    assert source_sheet.batch_reads == [[201]]


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
async def test_team_manager_upserts_team_and_summary_in_one_batch_same_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[tuple[str, object]] = []
    worksheet_lock = RecordingKeyLock("worksheet", events)
    structure_lock = RecordingKeyLock("structure", events)
    monkeypatch.setattr(
        manager_base_module,
        "WORKSHEET_TRANSACTION_LOCK",
        worksheet_lock,
    )
    monkeypatch.setattr(
        manager_base_module,
        "SPREADSHEET_STRUCTURE_LOCK",
        structure_lock,
    )
    manager = TeamRegisterManager(make_feature_channel("team_register"), "service.json")
    team_worksheet = FakeTeamGridWorksheet(
        101,
        "Main Team",
        [
            TeamWorksheetContent.COLUMNS,
            ["alice", "Old Alice", 1, 2, 3.0, "old team"],
        ],
    )
    encore_worksheet = FakeTeamGridWorksheet(
        102,
        "Encore Team",
        [TeamWorksheetContent.COLUMNS],
    )
    summary_headers, _ = SummaryWorksheetContent.extended_columns_dtypes_from_titles(
        ["Main Team", "Encore Team"]
    )
    summary_worksheet = FakeTeamGridWorksheet(
        201,
        "Team Summary",
        [
            [*SummaryWorksheetContent.COLUMNS, *summary_headers],
            ["alice", "Old Alice", "", 1, 3.0, "", "", "old summary"],
        ],
    )
    sheet = FakeTeamGridSheet([team_worksheet, encore_worksheet, summary_worksheet])
    configure_team_transaction_manager(
        manager,
        sheet,
        team_worksheet_ids=[101, 102],
        summary_worksheet_id=201,
    )
    user = make_user()
    team = TeamParser.parse_line(user, "150/740/33.4 main")

    await manager.upsert_user_registration(user, [], team, None)

    assert len(sheet.batch_updates) == 1
    value_updates = [
        mutation
        for mutation in sheet.batch_updates[0]
        if isinstance(mutation, GridValueUpdate)
    ]
    assert [
        (update.worksheet_id, update.start_row_index) for update in value_updates
    ] == [(101, 1), (201, 1)]
    assert [event for event in events if event[0] == "enter_worksheet"] == [
        ("enter_worksheet", ("team-transaction", 101)),
        ("enter_worksheet", ("team-transaction", 102)),
        ("enter_worksheet", ("team-transaction", 201)),
    ]
    assert all(event[0] != "enter_structure" for event in events)


@pytest.mark.asyncio
async def test_team_manager_batches_complete_grids_and_ignores_admin_values() -> None:
    manager = TeamRegisterManager(make_feature_channel("team_register"), "service.json")
    sentinel = "ADMIN_SENTINEL_MUST_BE_IGNORED"
    team_worksheet = FakeTeamGridWorksheet(
        101,
        "Main Team",
        [
            [*TeamWorksheetContent.COLUMNS, "manager_note"],
            ["alice", "Old Alice", 1, 2, 3.0, "old", sentinel],
        ],
    )
    summary_worksheet = FakeTeamGridWorksheet(
        201,
        "Team Summary",
        [
            [
                *SummaryWorksheetContent.COLUMNS,
                "Main Team ISV",
                "Main Team Power",
                "original_message",
                "manager_note",
            ],
            ["alice", "Old Alice", "", 1, 3.0, "old", sentinel],
        ],
    )
    sheet = FakeTeamGridSheet([team_worksheet, summary_worksheet])
    configure_team_transaction_manager(
        manager,
        sheet,
        team_worksheet_ids=[101],
        summary_worksheet_id=201,
    )
    await manager.refresh_summary_registration({})

    assert sheet.batch_reads == [[101, 201]]
    assert sentinel not in repr(sheet.batch_updates)


@pytest.mark.asyncio
async def test_team_manager_rejects_unbounded_header_after_batch() -> None:
    manager = TeamRegisterManager(make_feature_channel("team_register"), "service.json")
    team_worksheet = FakeTeamGridWorksheet(
        101,
        "Main Team",
        [
            ["username", "display_name", "manager_note"],
            ["ADMIN_SENTINEL_MUST_BE_IGNORED"],
        ],
    )
    summary_worksheet = FakeTeamGridWorksheet(
        201,
        "Team Summary",
        [[*SummaryWorksheetContent.COLUMNS, "original_message"]],
    )
    sheet = FakeTeamGridSheet([team_worksheet, summary_worksheet])
    configure_team_transaction_manager(
        manager,
        sheet,
        team_worksheet_ids=[101],
        summary_worksheet_id=201,
    )
    user = make_user()

    with pytest.raises(WorksheetContractError):
        await manager.upsert_user_registration(
            user,
            [],
            TeamParser.parse_line(user, "150/740/33.4 main"),
            None,
        )

    assert sheet.batch_reads == [[101, 201]]
    assert sheet.batch_updates == []


@pytest.mark.asyncio
async def test_team_manager_reads_and_repairs_moved_exact_missing_terminal() -> None:
    manager = TeamRegisterManager(make_feature_channel("team_register"), "service.json")
    team_worksheet = FakeTeamGridWorksheet(
        101,
        "Main Team",
        [
            [
                "display_name",
                "username",
                "leader_skill_value",
                "internal_skill_value",
                "team_power",
            ],
            ["Alice", "alice", 150, 740, 33.4],
        ],
    )
    summary_worksheet = FakeTeamGridWorksheet(
        201,
        "Team Summary",
        [
            [
                *SummaryWorksheetContent.COLUMNS,
                "Main Team ISV",
                "Main Team Power",
                "original_message",
            ]
        ],
    )
    sheet = FakeTeamGridSheet([team_worksheet, summary_worksheet])
    configure_team_transaction_manager(
        manager,
        sheet,
        team_worksheet_ids=[101],
        summary_worksheet_id=201,
    )

    await manager.refresh_summary_registration({})

    assert sheet.batch_reads == [[101, 201]]
    assert sheet.batch_updates[0][0] == DimensionMutation.insert_columns(
        101,
        start_column=6,
    )


@pytest.mark.asyncio
async def test_team_manager_uses_first_terminal_for_duplicate_incident_read() -> None:
    manager = TeamRegisterManager(make_feature_channel("team_register"), "service.json")
    team_worksheet = FakeTeamGridWorksheet(
        101,
        "Main Team",
        [TeamWorksheetContent.COLUMNS],
    )
    summary_worksheet = FakeTeamGridWorksheet(
        201,
        "Team Summary",
        [
            [
                *SummaryWorksheetContent.COLUMNS,
                "Main Team ISV",
                "Main Team Power",
                "original_message",
                "Old Team ISV",
                "Old Team Power",
                "original_message",
                "manager_note",
            ],
            ["alice", "Alice", "", 1, 1, "same", 2, 2, "stale", "private"],
        ],
    )
    sheet = FakeTeamGridSheet([team_worksheet, summary_worksheet])
    configure_team_transaction_manager(
        manager,
        sheet,
        team_worksheet_ids=[101],
        summary_worksheet_id=201,
    )

    await manager.refresh_summary_registration({})

    assert sheet.batch_reads == [[101, 201]]


@pytest.mark.asyncio
async def test_team_upsert_reconciles_retained_users_when_title_pair_is_added() -> None:
    manager = TeamRegisterManager(make_feature_channel("team_register"), "service.json")
    alice = make_user()
    bob = make_user("bob", "Bob")
    alice_team = TeamParser.parse_line(alice, "150/740/33.4 alice new")
    bob_team = TeamParser.parse_line(bob, "130/600/30.0 bob retained")
    team_worksheet = FakeTeamGridWorksheet(
        101,
        "Renamed Team",
        [
            TeamWorksheetContent.COLUMNS,
            ["alice", "Old Alice", 100, 500, 20.0, "alice old"],
            [
                bob_team.username,
                bob_team.display_name,
                bob_team.leader_skill_value,
                bob_team.internal_skill_value,
                bob_team.team_power,
                bob_team.original_message,
            ],
        ],
    )
    encore_worksheet = FakeTeamGridWorksheet(
        102,
        "Encore Team",
        [TeamWorksheetContent.COLUMNS],
    )
    summary_worksheet = FakeTeamGridWorksheet(
        201,
        "Team Summary",
        [
            [
                *SummaryWorksheetContent.COLUMNS,
                "Old Team ISV",
                "Old Team Power",
                "original_message",
            ],
            ["alice", "Old Alice", "", 1, 1, "alice old"],
            ["bob", "Bob", "", 2, 2, "bob old"],
        ],
    )
    sheet = FakeTeamGridSheet([team_worksheet, encore_worksheet, summary_worksheet])
    configure_team_transaction_manager(
        manager,
        sheet,
        team_worksheet_ids=[101, 102],
        summary_worksheet_id=201,
    )

    await manager.upsert_user_registration(alice, [], alice_team, None)

    bob_update = next(
        mutation
        for mutation in sheet.batch_updates[0]
        if isinstance(mutation, GridValueUpdate)
        and mutation.worksheet_id == 201
        and mutation.start_row_index == 2
    )
    assert bob_update.rows[0][3:5] == (
        bob_team.effective_skill_value,
        bob_team.team_power,
    )


@pytest.mark.asyncio
async def test_team_manager_repairs_duplicate_terminal_and_upserts_once() -> None:
    manager = TeamRegisterManager(make_feature_channel("team_register"), "service.json")
    user = make_user()
    parsed = TeamParser.parse_submission(
        user,
        ["160/800/35.7", "160/800/35.7", "160/800/100"],
    ).teams
    team_worksheets = [
        FakeTeamGridWorksheet(
            worksheet_id,
            title,
            [TeamWorksheetContent.COLUMNS],
        )
        for worksheet_id, title in zip(
            [101, 102, 103],
            ["Main Team", "Encore Team", "Backup Team"],
            strict=True,
        )
    ]
    summary_worksheet = FakeTeamGridWorksheet(
        201,
        "Team Summary",
        [
            [
                *SummaryWorksheetContent.COLUMNS,
                "Main Team ISV",
                "Main Team Power",
                "Encore Team ISV",
                "Encore Team Power",
                "Backup Team ISV",
                "Backup Team Power",
                "original_message",
                "Old Team ISV",
                "Old Team Power",
                "original_message",
                "manager_note",
            ],
            [
                "alice",
                "Old Alice",
                "",
                1,
                2,
                3,
                4,
                5,
                6,
                "old",
                7,
                8,
                "stale",
                "preserve",
            ],
        ],
    )
    sheet = FakeTeamGridSheet([*team_worksheets, summary_worksheet])
    configure_team_transaction_manager(
        manager,
        sheet,
        team_worksheet_ids=[101, 102, 103],
        summary_worksheet_id=201,
    )

    await manager.upsert_user_registration(
        user,
        [],
        parsed[0],
        parsed[1],
        parsed[2],
    )

    assert len(sheet.batch_updates) == 1
    mutations = sheet.batch_updates[0]
    assert mutations[0] == DimensionMutation.delete_columns(
        201,
        start_column=11,
        count=3,
    )
    assert [
        mutation.worksheet_id
        for mutation in mutations
        if isinstance(mutation, GridValueUpdate)
    ] == [101, 102, 103, 201]


@pytest.mark.asyncio
async def test_team_manager_composes_duplicate_repair_and_reconciles_growth() -> None:
    manager = TeamRegisterManager(make_feature_channel("team_register"), "service.json")
    alice = make_user()
    bob = make_user("bob", "Bob")
    alice_teams = TeamParser.parse_submission(
        alice,
        ["160/800/35.7 main", "150/740/33.4 encore", "140/680/30 backup"],
    ).teams
    bob_teams = TeamParser.parse_submission(
        bob,
        ["130/600/30 main", "120/550/28 encore", "110/500/26 backup"],
    ).teams
    team_worksheets = [
        FakeTeamGridWorksheet(
            worksheet_id,
            title,
            [
                TeamWorksheetContent.COLUMNS,
                ["alice", "Old Alice", 100, 500, 20.0, "old"],
                [
                    bob_team.username,
                    bob_team.display_name,
                    bob_team.leader_skill_value,
                    bob_team.internal_skill_value,
                    bob_team.team_power,
                    bob_team.original_message,
                ],
            ],
        )
        for worksheet_id, title, bob_team in zip(
            [101, 102, 103],
            ["Main Team", "Encore Team", "Backup Team"],
            bob_teams,
            strict=True,
        )
    ]
    summary_worksheet = FakeTeamGridWorksheet(
        201,
        "Team Summary",
        [
            [
                *SummaryWorksheetContent.COLUMNS,
                "Main Team ISV",
                "Main Team Power",
                "Old Encore ISV",
                "Old Encore Power",
                "original_message",
                "Retired Team ISV",
                "Retired Team Power",
                "original_message",
                "manager_note",
            ],
            ["alice", "Old Alice", "", 1, 1, 2, 2, "old", 3, 3, "stale", "a"],
            ["bob", "Bob", "", 4, 4, 5, 5, "old", 6, 6, "stale", "b"],
        ],
    )
    sheet = FakeTeamGridSheet([*team_worksheets, summary_worksheet])
    configure_team_transaction_manager(
        manager,
        sheet,
        team_worksheet_ids=[101, 102, 103],
        summary_worksheet_id=201,
    )

    await manager.upsert_user_registration(
        alice,
        [],
        alice_teams[0],
        alice_teams[1],
        alice_teams[2],
    )

    assert len(sheet.batch_updates) == 1
    mutations = sheet.batch_updates[0]
    assert mutations[:3] == [
        DimensionMutation.delete_columns(201, start_column=9, count=3),
        DimensionMutation.delete_columns(201, start_column=6, count=2),
        DimensionMutation.insert_columns(201, start_column=6, count=4),
    ]
    bob_update = next(
        mutation
        for mutation in mutations
        if isinstance(mutation, GridValueUpdate)
        and mutation.worksheet_id == 201
        and mutation.start_row_index == 2
    )
    assert bob_update.rows[0][3:9] == tuple(
        value
        for team in bob_teams
        for value in (team.effective_skill_value, team.team_power)
    )


@pytest.mark.asyncio
async def test_team_manager_deletes_complete_team_and_summary_rows_once() -> None:
    manager = TeamRegisterManager(make_feature_channel("team_register"), "service.json")
    team_worksheet = FakeTeamGridWorksheet(
        101,
        "Main Team",
        [
            [*TeamWorksheetContent.COLUMNS, "manager_note"],
            ["alice", "Alice", 150, 740, 33.4, "team", "delete too"],
        ],
    )
    summary_worksheet = FakeTeamGridWorksheet(
        201,
        "Team Summary",
        [
            [
                *SummaryWorksheetContent.COLUMNS,
                "Main Team ISV",
                "Main Team Power",
                "original_message",
                "manager_note",
            ],
            ["alice", "Alice", "", 268, 33.4, "team", "delete too"],
        ],
    )
    sheet = FakeTeamGridSheet([team_worksheet, summary_worksheet])
    configure_team_transaction_manager(
        manager,
        sheet,
        team_worksheet_ids=[101],
        summary_worksheet_id=201,
    )

    await manager.delete_user_registration(make_user())

    assert len(sheet.batch_updates) == 1
    assert sheet.batch_updates[0] == [
        DimensionMutation.delete_rows(101, start_row=2),
        DimensionMutation.delete_rows(201, start_row=2),
    ]


@pytest.mark.asyncio
async def test_team_delete_repairs_missing_configured_team_before_summary() -> None:
    manager = TeamRegisterManager(make_feature_channel("team_register"), "service.json")
    main_worksheet = FakeTeamGridWorksheet(
        101,
        "Main Team",
        [
            TeamWorksheetContent.COLUMNS,
            ["alice", "Alice", 150, 740, 33.4, "main"],
        ],
    )
    summary_worksheet = FakeTeamGridWorksheet(
        201,
        "Team Summary",
        [
            [
                *SummaryWorksheetContent.COLUMNS,
                "Main Team ISV",
                "Main Team Power",
                "Encore Team ISV",
                "Encore Team Power",
                "original_message",
            ],
            ["alice", "Alice", "", 268, 33.4, "", "", "main"],
        ],
    )
    sheet = FakeTeamGridSheet([main_worksheet, summary_worksheet])
    configure_team_transaction_manager(
        manager,
        sheet,
        team_worksheet_ids=[101, 102],
        summary_worksheet_id=201,
    )
    manager.upsert_sheet_config = AsyncMock()  # type: ignore[method-assign]

    await manager.delete_user_registration(make_user())

    assert sheet.created_titles == ["Encore Team"]
    manager.upsert_sheet_config.assert_awaited_once()
    repaired_metadata = manager.upsert_sheet_config.await_args.args[0]
    assert [worksheet.id for worksheet in repaired_metadata] == [101, 202, 201]
    assert not any(
        isinstance(mutation, DimensionMutation)
        and mutation.dimension == "COLUMNS"
        and mutation.operation == "delete"
        for mutation in sheet.batch_updates[0]
    )
    assert [
        mutation
        for mutation in sheet.batch_updates[0]
        if isinstance(mutation, DimensionMutation) and mutation.dimension == "ROWS"
    ] == [
        DimensionMutation.delete_rows(101, start_row=2),
        DimensionMutation.delete_rows(201, start_row=2),
    ]


@pytest.mark.asyncio
async def test_team_delete_reconciles_retained_users_when_title_pair_is_added() -> None:
    manager = TeamRegisterManager(make_feature_channel("team_register"), "service.json")
    bob = make_user("bob", "Bob")
    bob_team = TeamParser.parse_line(bob, "130/600/30.0 bob retained")
    team_worksheet = FakeTeamGridWorksheet(
        101,
        "Renamed Team",
        [
            TeamWorksheetContent.COLUMNS,
            ["alice", "Alice", 150, 740, 33.4, "alice delete"],
            [
                bob_team.username,
                bob_team.display_name,
                bob_team.leader_skill_value,
                bob_team.internal_skill_value,
                bob_team.team_power,
                bob_team.original_message,
            ],
        ],
    )
    summary_worksheet = FakeTeamGridWorksheet(
        201,
        "Team Summary",
        [
            [
                *SummaryWorksheetContent.COLUMNS,
                "Old Team ISV",
                "Old Team Power",
                "original_message",
            ],
            ["alice", "Alice", "", 1, 1, "alice old"],
            ["bob", "Bob", "", 2, 2, "bob old"],
        ],
    )
    sheet = FakeTeamGridSheet([team_worksheet, summary_worksheet])
    configure_team_transaction_manager(
        manager,
        sheet,
        team_worksheet_ids=[101],
        summary_worksheet_id=201,
    )

    await manager.delete_user_registration(make_user())

    bob_update = next(
        mutation
        for mutation in sheet.batch_updates[0]
        if isinstance(mutation, GridValueUpdate)
        and mutation.worksheet_id == 201
        and mutation.start_row_index == 2
    )
    assert bob_update.rows[0][3:5] == (
        bob_team.effective_skill_value,
        bob_team.team_power,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["upsert", "delete"])
@pytest.mark.parametrize(
    "migration",
    ["duplicate_marker", "missing_terminal", "pure_pair_deletion"],
)
async def test_team_row_local_migrations_do_not_parse_unrelated_numbers(
    operation: str,
    migration: str,
) -> None:
    manager = TeamRegisterManager(make_feature_channel("team_register"), "service.json")
    main_worksheet = FakeTeamGridWorksheet(
        101,
        "Main Team",
        [
            TeamWorksheetContent.COLUMNS,
            ["alice", "Old Alice", 100, 500, 20.0, "old"],
            ["bob", "Bob", "not-int", "bad", "bad", "unrelated"],
        ],
    )
    encore_worksheet = FakeTeamGridWorksheet(
        102,
        "Encore Team",
        [TeamWorksheetContent.COLUMNS],
    )
    canonical_without_terminal = [
        *SummaryWorksheetContent.COLUMNS,
        "Main Team ISV",
        "Main Team Power",
        "Encore Team ISV",
        "Encore Team Power",
    ]
    if migration == "duplicate_marker":
        summary_headers = [
            *canonical_without_terminal,
            "original_message",
            "Old Team ISV",
            "Old Team Power",
            "original_message",
        ]
    elif migration == "missing_terminal":
        summary_headers = canonical_without_terminal
    else:
        summary_headers = [
            *canonical_without_terminal,
            "Old Team ISV",
            "Old Team Power",
            "original_message",
        ]
    summary_worksheet = FakeTeamGridWorksheet(
        201,
        "Team Summary",
        [summary_headers, ["alice"], ["bob"]],
    )
    sheet = FakeTeamGridSheet([main_worksheet, encore_worksheet, summary_worksheet])
    configure_team_transaction_manager(
        manager,
        sheet,
        team_worksheet_ids=[101, 102],
        summary_worksheet_id=201,
    )

    if operation == "upsert":
        user = make_user()
        team = TeamParser.parse_line(user, "150/740/33.4 new")
        await manager.upsert_user_registration(user, [], team, None)
    else:
        await manager.delete_user_registration(make_user())

    assert len(sheet.batch_updates) == 1


@pytest.mark.asyncio
async def test_team_manager_refresh_reconciles_summary_and_returns_frame() -> None:
    manager = TeamRegisterManager(make_feature_channel("team_register"), "service.json")
    main_worksheet = FakeTeamGridWorksheet(
        101,
        "Main Team",
        [
            TeamWorksheetContent.COLUMNS,
            ["alice", "Alice", 150, 740, 33.4, "alice main"],
            ["bob", "Bob", 130, 600, 30.0, "bob main"],
        ],
    )
    backup_worksheet = FakeTeamGridWorksheet(
        102,
        "Backup Team",
        [
            TeamWorksheetContent.COLUMNS,
            ["alice", "Alice", 140, 680, 35.3, "alice backup"],
        ],
    )
    summary_worksheet = FakeTeamGridWorksheet(
        201,
        "Team Summary",
        [
            [
                *SummaryWorksheetContent.COLUMNS,
                "Main Team ISV",
                "Main Team Power",
                "Backup Team ISV",
                "Backup Team Power",
                "original_message",
                "manager_note",
            ],
            ["carol", "Carol", "", 1, 1, 1, 1, "old", "delete too"],
            ["alice", "Old Alice", "", 0, 0, 0, 0, "old", "preserve"],
            ["", "", "", "", "", "", "", "", "prepared"],
        ],
    )
    sheet = FakeTeamGridSheet([main_worksheet, backup_worksheet, summary_worksheet])
    configure_team_transaction_manager(
        manager,
        sheet,
        team_worksheet_ids=[101, 102],
        summary_worksheet_id=201,
    )

    final = await manager.refresh_summary_registration(
        {
            "alice": SimpleNamespace(display_name="Alice New", roles=[]),
            "bob": SimpleNamespace(display_name="Bob", roles=[]),
        }
    )

    assert len(sheet.batch_updates) == 1
    mutations = sheet.batch_updates[0]
    assert [
        mutation.start_row_index
        for mutation in mutations
        if isinstance(mutation, GridValueUpdate)
    ] == [2, 3]
    assert mutations[-1] == DimensionMutation.delete_rows(201, start_row=2)
    assert list(final.index) == ["alice", "bob"]
    assert final.loc["alice", "display_name"] == "Alice New"
    assert "original_message" not in final.columns
    assert build_summary_embed(final).title == "📊 Team Register Summary"


@pytest.mark.asyncio
async def test_team_refresh_treats_omitted_summary_cells_as_blank() -> None:
    manager = TeamRegisterManager(make_feature_channel("team_register"), "service.json")
    team_worksheet = FakeTeamGridWorksheet(
        101,
        "Main Team",
        [
            TeamWorksheetContent.COLUMNS,
            ["alice", "Alice", 150, 740, 33.4, "main"],
        ],
    )
    summary_worksheet = FakeTeamGridWorksheet(
        201,
        "Team Summary",
        [
            [
                *SummaryWorksheetContent.COLUMNS,
                "Main Team ISV",
                "Main Team Power",
                "original_message",
            ],
            ["alice"],
        ],
    )
    sheet = FakeTeamGridSheet([team_worksheet, summary_worksheet])
    configure_team_transaction_manager(
        manager,
        sheet,
        team_worksheet_ids=[101],
        summary_worksheet_id=201,
    )

    final = await manager.refresh_summary_registration(
        {"alice": SimpleNamespace(display_name="Alice", roles=[])}
    )

    assert final.loc["alice", "display_name"] == "Alice"
    assert final.loc["alice", "encore_roles"] == ""


@pytest.mark.asyncio
async def test_team_refresh_follows_moved_usernames_in_team_and_summary() -> None:
    manager = TeamRegisterManager(make_feature_channel("team_register"), "service.json")
    team_worksheet = FakeTeamGridWorksheet(
        101,
        "Main Team",
        [
            [
                "display_name",
                "username",
                "leader_skill_value",
                "internal_skill_value",
                "team_power",
                "original_message",
            ],
            ["Alice", "alice", 150, 740, 33.4, "same"],
        ],
    )
    summary_worksheet = FakeTeamGridWorksheet(
        201,
        "Team Summary",
        [
            [
                "Main Team ISV",
                "display_name",
                "Main Team Power",
                "encore_roles",
                "username",
                "original_message",
            ],
            [1, "Old Alice", 1, "Old", "alice", "same"],
        ],
    )
    sheet = FakeTeamGridSheet([team_worksheet, summary_worksheet])
    configure_team_transaction_manager(
        manager,
        sheet,
        team_worksheet_ids=[101],
        summary_worksheet_id=201,
    )

    final = await manager.refresh_summary_registration({})

    summary_update = next(
        mutation
        for mutation in sheet.batch_updates[0]
        if isinstance(mutation, GridValueUpdate)
    )
    assert summary_update.rows[0] == (
        268,
        "Old Alice",
        33.4,
        "Old",
        "alice",
        "same",
    )
    assert final.index.name == "username"
    assert list(final.index) == ["alice"]
    assert final.loc["alice", "display_name"] == "Old Alice"


@pytest.mark.asyncio
async def test_team_manager_three_to_one_shrink_writes_before_row_deletes() -> None:
    manager = TeamRegisterManager(make_feature_channel("team_register"), "service.json")
    user = make_user()
    team_worksheets = [
        FakeTeamGridWorksheet(
            worksheet_id,
            title,
            [
                TeamWorksheetContent.COLUMNS,
                ["alice", "Alice", 100, 500, 20.0, "old"],
            ],
        )
        for worksheet_id, title in zip(
            [101, 102, 103],
            ["Main Team", "Encore Team", "Backup Team"],
            strict=True,
        )
    ]
    summary_headers, _ = SummaryWorksheetContent.extended_columns_dtypes_from_titles(
        ["Main Team", "Encore Team", "Backup Team"]
    )
    summary_worksheet = FakeTeamGridWorksheet(
        201,
        "Team Summary",
        [
            [*SummaryWorksheetContent.COLUMNS, *summary_headers],
            ["alice", "Alice", "", 1, 1, 1, 1, 1, 1, "old"],
        ],
    )
    sheet = FakeTeamGridSheet([*team_worksheets, summary_worksheet])
    configure_team_transaction_manager(
        manager,
        sheet,
        team_worksheet_ids=[101, 102, 103],
        summary_worksheet_id=201,
    )
    main = TeamParser.parse_line(user, "160/800/35.7")

    await manager.upsert_user_registration(user, [], main, None)

    mutations = sheet.batch_updates[0]
    assert [(type(mutation), mutation.worksheet_id) for mutation in mutations] == [
        (GridValueUpdate, 101),
        (GridValueUpdate, 201),
        (DimensionMutation, 102),
        (DimensionMutation, 103),
    ]


@pytest.mark.asyncio
async def test_team_manager_grows_blank_grids_before_initialization_writes() -> None:
    manager = TeamRegisterManager(make_feature_channel("team_register"), "service.json")
    team_worksheet = FakeTeamGridWorksheet(
        101,
        "Main Team",
        [],
        row_count=1,
        col_count=2,
    )
    encore_worksheet = FakeTeamGridWorksheet(
        102,
        "Encore Team",
        [],
        row_count=1,
        col_count=2,
    )
    summary_worksheet = FakeTeamGridWorksheet(
        201,
        "Team Summary",
        [],
        row_count=1,
        col_count=2,
    )
    sheet = FakeTeamGridSheet([team_worksheet, encore_worksheet, summary_worksheet])
    configure_team_transaction_manager(
        manager,
        sheet,
        team_worksheet_ids=[101, 102],
        summary_worksheet_id=201,
    )
    user = make_user()
    team = TeamParser.parse_line(user, "150/740/33.4")

    await manager.upsert_user_registration(user, [], team, None)

    mutations = sheet.batch_updates[0]
    first_write = next(
        index
        for index, mutation in enumerate(mutations)
        if isinstance(mutation, GridValueUpdate)
    )
    assert mutations[:first_write] == [
        DimensionMutation.append_rows(101, 1),
        DimensionMutation.append_columns(101, 4),
        DimensionMutation.append_columns(102, 4),
        DimensionMutation.append_rows(201, 1),
        DimensionMutation.append_columns(201, 6),
    ]


@pytest.mark.asyncio
async def test_team_manager_clips_reads_to_small_grids_before_atomic_growth() -> None:
    manager = TeamRegisterManager(make_feature_channel("team_register"), "service.json")
    worksheets = [
        FakeTeamGridWorksheet(
            worksheet_id,
            title,
            [],
            row_count=2,
            col_count=2,
        )
        for worksheet_id, title in (
            (101, "Main Team"),
            (102, "Encore Team"),
            (201, "Team Summary"),
        )
    ]
    sheet = FakeTeamGridSheet(worksheets)
    configure_team_transaction_manager(
        manager,
        sheet,
        team_worksheet_ids=[101, 102],
        summary_worksheet_id=201,
    )
    team = TeamParser.parse_line(make_user(), "150/740/33.4")

    await manager.upsert_user_registration(make_user(), [], team, None)

    assert sheet.batch_reads == [[101, 102, 201]]


@pytest.mark.asyncio
async def test_team_manager_appends_exact_width_missing_terminals_once() -> None:
    manager = TeamRegisterManager(make_feature_channel("team_register"), "service.json")
    team_headers = TeamWorksheetContent.COLUMNS[:-1]
    summary_headers = [
        *SummaryWorksheetContent.COLUMNS,
        "Main Team ISV",
        "Main Team Power",
        "Encore Team ISV",
        "Encore Team Power",
    ]
    team_worksheet = FakeTeamGridWorksheet(
        101,
        "Main Team",
        [team_headers],
        col_count=len(team_headers),
    )
    summary_worksheet = FakeTeamGridWorksheet(
        201,
        "Team Summary",
        [summary_headers],
        col_count=len(summary_headers),
    )
    encore_worksheet = FakeTeamGridWorksheet(
        102,
        "Encore Team",
        [TeamWorksheetContent.COLUMNS],
        col_count=len(TeamWorksheetContent.COLUMNS),
    )
    sheet = FakeTeamGridSheet([team_worksheet, encore_worksheet, summary_worksheet])
    configure_team_transaction_manager(
        manager,
        sheet,
        team_worksheet_ids=[101, 102],
        summary_worksheet_id=201,
    )
    team = TeamParser.parse_line(make_user(), "150/740/33.4")

    await manager.upsert_user_registration(make_user(), [], team, None)

    column_appends = [
        mutation
        for mutation in sheet.batch_updates[0]
        if isinstance(mutation, DimensionMutation)
        and mutation.operation == "append"
        and mutation.dimension == "COLUMNS"
    ]
    assert column_appends == [
        DimensionMutation.append_columns(101, count=1),
        DimensionMutation.append_columns(201, count=1),
    ]
    assert not any(
        isinstance(mutation, DimensionMutation)
        and mutation.operation == "insert"
        and mutation.dimension == "COLUMNS"
        for mutation in sheet.batch_updates[0]
    )


@pytest.mark.asyncio
async def test_team_manager_creates_missing_tabs_saves_ids_then_batches_once() -> None:
    manager = TeamRegisterManager(make_feature_channel("team_register"), "service.json")
    main_worksheet = FakeTeamGridWorksheet(
        101,
        "Main Team",
        [TeamWorksheetContent.COLUMNS],
    )
    sheet = FakeTeamGridSheet([main_worksheet])
    configure_team_transaction_manager(
        manager,
        sheet,
        team_worksheet_ids=[101, 102],
        summary_worksheet_id=201,
    )
    saved_metadata: list[TeamRegisterGoogleSheetsMetadata] = []

    async def save_metadata(metadata: TeamRegisterGoogleSheetsMetadata) -> None:
        saved_metadata.append(metadata)

    manager.upsert_sheet_config = save_metadata  # type: ignore[method-assign]
    user = make_user()
    main = TeamParser.parse_line(user, "150/740/33.4")
    encore = TeamParser.parse_line(user, "140/680/35.3")

    await manager.upsert_user_registration(user, [], main, encore)

    assert sheet.created_titles == ["Encore Team", "Team Summary"]
    assert len(saved_metadata) == 1
    assert saved_metadata[0].team_worksheets[1].id is not None
    assert saved_metadata[0].summary_worksheet.id is not None
    assert len(sheet.batch_updates) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("team_titles", "summary_title"),
    [
        (["   "], "Team Summary"),
        (["Main Team"], "\t"),
        (["Same"], "Same"),
    ],
)
async def test_team_settings_rejects_invalid_titles_before_sheet_access(
    monkeypatch: pytest.MonkeyPatch,
    team_titles: list[str],
    summary_title: str,
) -> None:
    manager = TeamRegisterManager(make_feature_channel("team_register"), "service.json")
    sheet_factory = Mock()
    monkeypatch.setattr(
        team_register_manager_module,
        "GoogleSheet",
        sheet_factory,
    )

    with pytest.raises(WorksheetContractError):
        await manager.upsert_sheet_config_and_worksheets(
            "https://docs.google.com/spreadsheets/d/team-settings/edit",
            team_worksheet_titles=team_titles,
            summary_worksheet_title=summary_title,
        )

    sheet_factory.assert_not_called()


@pytest.mark.asyncio
async def test_team_settings_preflights_saves_then_reconciles_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = TeamRegisterManager(make_feature_channel("team_register"), "service.json")
    configure_team_settings_config(manager)
    main_worksheet = FakeTeamGridWorksheet(
        101,
        "Main Team",
        [
            TeamWorksheetContent.COLUMNS,
            ["alice", "Alice", 150, 740, 33.4, "main"],
        ],
    )
    summary_worksheet = FakeTeamGridWorksheet(
        201,
        "Team Summary",
        [
            [
                *SummaryWorksheetContent.COLUMNS,
                "Main Team ISV",
                "Main Team Power",
                "original_message",
            ],
        ],
    )
    sheet = FakeTeamGridSheet([main_worksheet, summary_worksheet])
    monkeypatch.setattr(
        team_register_manager_module,
        "GoogleSheet",
        lambda _url, _path: sheet,
        raising=False,
    )
    saved_metadata: list[TeamRegisterGoogleSheetsMetadata] = []

    async def save_metadata(metadata: TeamRegisterGoogleSheetsMetadata) -> None:
        saved_metadata.append(metadata)
        manager._sheet_config = SimpleNamespace(  # noqa: SLF001
            sheet_url=metadata.sheet_url,
            encore_role_ids=[],
            team_worksheet_ids=[ws.id for ws in metadata.team_worksheets],
            summary_worksheet_id=metadata.summary_worksheet.id,
            get_worksheet_ids=lambda: [ws.id for ws in metadata],
        )

    manager.upsert_sheet_config = save_metadata  # type: ignore[method-assign]

    result = await manager.upsert_sheet_config_and_worksheets(
        sheet.sheet_url,
        team_worksheet_titles=["Main Team"],
        summary_worksheet_title="Team Summary",
        member_by_names={},
    )

    assert result.team_worksheets[0].id == 101
    assert result.summary_worksheet.id == 201
    assert len(saved_metadata) == 1
    assert len(sheet.batch_updates) == 1
    assert any(
        isinstance(mutation, GridValueUpdate) and mutation.worksheet_id == 201
        for mutation in sheet.batch_updates[0]
    )


@pytest.mark.asyncio
async def test_team_settings_refreshes_encore_ids_after_waiting_for_channel_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = TeamRegisterManager(make_feature_channel("team_register"), "service.json")
    old_role = SimpleNamespace(id=7, name="Old Encore")
    new_role = SimpleNamespace(id=8, name="New Encore")
    old_config = SimpleNamespace(
        sheet_url="https://docs.google.com/spreadsheets/d/old-team/edit",
        encore_role_ids=[old_role.id],
    )
    new_config = SimpleNamespace(
        sheet_url="https://docs.google.com/spreadsheets/d/new-team/edit",
        encore_role_ids=[new_role.id],
    )
    persisted = {"config": old_config}
    manager._sheet_config = old_config  # type: ignore[assignment]  # noqa: SLF001
    main_worksheet = FakeTeamGridWorksheet(
        101,
        "Main Team",
        [
            TeamWorksheetContent.COLUMNS,
            ["alice", "Alice", 150, 740, 33.4, "main"],
        ],
    )
    summary_worksheet = FakeTeamGridWorksheet(
        201,
        "Team Summary",
        [
            [
                *SummaryWorksheetContent.COLUMNS,
                "Main Team ISV",
                "Main Team Power",
                "original_message",
            ],
            ["alice", "Alice", "Old Encore", 268, 33.4, "main"],
        ],
    )
    sheet = FakeTeamGridSheet([main_worksheet, summary_worksheet])
    monkeypatch.setattr(
        team_register_manager_module,
        "GoogleSheet",
        lambda _url, _path: sheet,
        raising=False,
    )

    async def load_config() -> SimpleNamespace:
        if manager._sheet_config is None:  # noqa: SLF001
            manager._sheet_config = persisted["config"]  # type: ignore[assignment]  # noqa: SLF001
        return manager._sheet_config  # type: ignore[return-value]  # noqa: SLF001

    manager.get_sheet_config_or_none = load_config  # type: ignore[method-assign]
    manager.upsert_sheet_config = AsyncMock()  # type: ignore[method-assign]
    channel_lock = KeyAsyncLock()
    encore_holds_lock = asyncio.Event()
    settings_attempted = asyncio.Event()
    release_encore = asyncio.Event()

    async def concurrent_encore_save() -> None:
        async with channel_lock(manager.feature_channel.channel_id):
            persisted["config"] = new_config
            encore_holds_lock.set()
            await settings_attempted.wait()
            await release_encore.wait()

    async def settings_save() -> None:
        await encore_holds_lock.wait()
        settings_attempted.set()
        async with channel_lock(manager.feature_channel.channel_id):
            await manager.upsert_sheet_config_and_worksheets(
                sheet.sheet_url,
                team_worksheet_titles=["Main Team"],
                summary_worksheet_title="Team Summary",
                member_by_names={
                    "alice": SimpleNamespace(
                        display_name="Alice",
                        roles=[old_role, new_role],
                    )
                },
            )

    encore_task = asyncio.create_task(concurrent_encore_save())
    settings_task = asyncio.create_task(settings_save())
    await settings_attempted.wait()
    await asyncio.sleep(0)
    assert not settings_task.done()
    release_encore.set()
    await asyncio.gather(encore_task, settings_task)

    summary_update = next(
        mutation
        for mutation in sheet.batch_updates[0]
        if isinstance(mutation, GridValueUpdate)
        and mutation.worksheet_id == summary_worksheet.id
        and mutation.start_row_index == 1
    )
    assert summary_update.rows[0][2] == "New Encore"


@pytest.mark.asyncio
async def test_team_settings_contract_failure_precedes_config_save(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = TeamRegisterManager(make_feature_channel("team_register"), "service.json")
    configure_team_settings_config(manager)
    main_worksheet = FakeTeamGridWorksheet(
        101,
        "Main Team",
        [
            TeamWorksheetContent.COLUMNS,
            ["alice", "Alice", "not-int", 740, 33.4, "bad"],
        ],
    )
    summary_worksheet = FakeTeamGridWorksheet(
        201,
        "Team Summary",
        [
            [
                *SummaryWorksheetContent.COLUMNS,
                "Main Team ISV",
                "Main Team Power",
                "original_message",
            ]
        ],
    )
    sheet = FakeTeamGridSheet([main_worksheet, summary_worksheet])
    monkeypatch.setattr(
        team_register_manager_module,
        "GoogleSheet",
        lambda _url, _path: sheet,
        raising=False,
    )
    manager.upsert_sheet_config = AsyncMock()  # type: ignore[method-assign]

    with pytest.raises(WorksheetContractError):
        await manager.upsert_sheet_config_and_worksheets(
            sheet.sheet_url,
            team_worksheet_titles=["Main Team"],
            summary_worksheet_title="Team Summary",
            member_by_names={},
        )

    manager.upsert_sheet_config.assert_not_awaited()
    assert sheet.created_titles == []
    assert sheet.batch_updates == []


@pytest.mark.asyncio
async def test_team_settings_validates_existing_tabs_before_creating_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = TeamRegisterManager(make_feature_channel("team_register"), "service.json")
    configure_team_settings_config(manager)
    main_worksheet = FakeTeamGridWorksheet(
        101,
        "Main Team",
        [["username", "display_name", "original_message"]],
    )
    summary_worksheet = FakeTeamGridWorksheet(
        201,
        "Team Summary",
        [
            [
                *SummaryWorksheetContent.COLUMNS,
                "Main Team ISV",
                "Main Team Power",
                "original_message",
            ]
        ],
    )
    sheet = FakeTeamGridSheet([main_worksheet, summary_worksheet])
    monkeypatch.setattr(
        team_register_manager_module,
        "GoogleSheet",
        lambda _url, _path: sheet,
        raising=False,
    )
    manager.upsert_sheet_config = AsyncMock()  # type: ignore[method-assign]

    with pytest.raises(WorksheetContractError):
        await manager.upsert_sheet_config_and_worksheets(
            sheet.sheet_url,
            team_worksheet_titles=["Main Team", "New Team"],
            summary_worksheet_title="Team Summary",
            member_by_names={},
        )

    manager.upsert_sheet_config.assert_not_awaited()
    assert sheet.created_titles == []
    assert sheet.batch_updates == []


@pytest.mark.asyncio
async def test_team_settings_rejects_later_headerless_team_row_before_creation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = TeamRegisterManager(make_feature_channel("team_register"), "service.json")
    configure_team_settings_config(manager)
    main_worksheet = FakeTeamGridWorksheet(
        101,
        "Main Team",
        [
            [],
            [],
            ["manual", "Manual", 150, 740, 33.4, "must not gain a header"],
        ],
    )
    summary_worksheet = FakeTeamGridWorksheet(
        201,
        "Team Summary",
        [
            [
                *SummaryWorksheetContent.COLUMNS,
                "Main Team ISV",
                "Main Team Power",
                "New Team ISV",
                "New Team Power",
                "original_message",
            ]
        ],
    )
    sheet = FakeTeamGridSheet([main_worksheet, summary_worksheet])
    monkeypatch.setattr(
        team_register_manager_module,
        "GoogleSheet",
        lambda _url, _path: sheet,
        raising=False,
    )
    manager.upsert_sheet_config = AsyncMock()  # type: ignore[method-assign]

    with pytest.raises(WorksheetContractError):
        await manager.upsert_sheet_config_and_worksheets(
            sheet.sheet_url,
            team_worksheet_titles=["Main Team", "New Team"],
            summary_worksheet_title="Team Summary",
            member_by_names={},
        )

    assert sheet.batch_reads == [[101, 201]]
    assert sheet.created_titles == []
    manager.upsert_sheet_config.assert_not_awaited()
    assert sheet.batch_updates == []


@pytest.mark.asyncio
async def test_team_settings_rejects_later_headerless_summary_row_before_creation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = TeamRegisterManager(make_feature_channel("team_register"), "service.json")
    configure_team_settings_config(manager)
    main_worksheet = FakeTeamGridWorksheet(
        101,
        "Main Team",
        [TeamWorksheetContent.COLUMNS],
    )
    summary_worksheet = FakeTeamGridWorksheet(
        201,
        "Team Summary",
        [
            [],
            [],
            ["manual", "Manual", "", 1, 1, "", "", "must not be deleted"],
        ],
    )
    sheet = FakeTeamGridSheet([main_worksheet, summary_worksheet])
    monkeypatch.setattr(
        team_register_manager_module,
        "GoogleSheet",
        lambda _url, _path: sheet,
        raising=False,
    )
    manager.upsert_sheet_config = AsyncMock()  # type: ignore[method-assign]

    with pytest.raises(WorksheetContractError):
        await manager.upsert_sheet_config_and_worksheets(
            sheet.sheet_url,
            team_worksheet_titles=["Main Team", "New Team"],
            summary_worksheet_title="Team Summary",
            member_by_names={},
        )

    assert sheet.batch_reads == [[101, 201]]
    assert sheet.created_titles == []
    manager.upsert_sheet_config.assert_not_awaited()
    assert sheet.batch_updates == []


@pytest.mark.asyncio
async def test_team_settings_validates_later_source_rows_before_creating_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = TeamRegisterManager(make_feature_channel("team_register"), "service.json")
    configure_team_settings_config(manager)
    main_worksheet = FakeTeamGridWorksheet(
        101,
        "Main Team",
        [
            TeamWorksheetContent.COLUMNS,
            ["alice", "Alice", 150, 740, 33.4, "valid"],
            ["bob", "Bob", "not-int", 700, 31.2, "malformed later row"],
        ],
    )
    summary_worksheet = FakeTeamGridWorksheet(
        201,
        "Team Summary",
        [
            [
                *SummaryWorksheetContent.COLUMNS,
                "Main Team ISV",
                "Main Team Power",
                "original_message",
            ]
        ],
    )
    sheet = FakeTeamGridSheet([main_worksheet, summary_worksheet])
    monkeypatch.setattr(
        team_register_manager_module,
        "GoogleSheet",
        lambda _url, _path: sheet,
        raising=False,
    )
    manager.upsert_sheet_config = AsyncMock()  # type: ignore[method-assign]

    with pytest.raises(WorksheetContractError):
        await manager.upsert_sheet_config_and_worksheets(
            sheet.sheet_url,
            team_worksheet_titles=["Main Team", "New Team"],
            summary_worksheet_title="Team Summary",
            member_by_names={},
        )

    manager.upsert_sheet_config.assert_not_awaited()
    assert sheet.created_titles == []
    assert sheet.batch_updates == []


@pytest.mark.asyncio
async def test_team_upsert_indexes_duplicate_keys_before_creating_missing() -> None:
    manager = TeamRegisterManager(make_feature_channel("team_register"), "service.json")
    main_worksheet = FakeTeamGridWorksheet(
        101,
        "Main Team",
        [
            TeamWorksheetContent.COLUMNS,
            ["alice", "Alice", 150, 740, 33.4, "first"],
            ["alice", "Duplicate", 140, 680, 35.3, "second"],
        ],
    )
    summary_worksheet = FakeTeamGridWorksheet(
        201,
        "Team Summary",
        [
            [
                *SummaryWorksheetContent.COLUMNS,
                "Main Team ISV",
                "Main Team Power",
                "Encore Team ISV",
                "Encore Team Power",
                "original_message",
            ]
        ],
    )
    sheet = FakeTeamGridSheet([main_worksheet, summary_worksheet])
    configure_team_transaction_manager(
        manager,
        sheet,
        team_worksheet_ids=[101, 102],
        summary_worksheet_id=201,
    )
    manager.upsert_sheet_config = AsyncMock()  # type: ignore[method-assign]
    user = make_user()

    with pytest.raises(WorksheetContractError):
        await manager.upsert_user_registration(
            user,
            [],
            TeamParser.parse_line(user, "150/740/33.4"),
            TeamParser.parse_line(user, "140/680/35.3"),
        )

    assert sheet.created_titles == []
    manager.upsert_sheet_config.assert_not_awaited()
    assert sheet.batch_updates == []


@pytest.mark.asyncio
async def test_team_upsert_validates_full_reconcile_before_creating_missing() -> None:
    manager = TeamRegisterManager(make_feature_channel("team_register"), "service.json")
    main_worksheet = FakeTeamGridWorksheet(
        101,
        "Main Team",
        [
            TeamWorksheetContent.COLUMNS,
            ["alice", "Alice", 150, 740, 33.4, "valid"],
            ["bob", "Bob", "not-int", 700, 31.2, "malformed later row"],
        ],
    )
    summary_worksheet = FakeTeamGridWorksheet(
        201,
        "Team Summary",
        [
            [
                *SummaryWorksheetContent.COLUMNS,
                "Main Team ISV",
                "Main Team Power",
                "original_message",
            ]
        ],
    )
    sheet = FakeTeamGridSheet([main_worksheet, summary_worksheet])
    configure_team_transaction_manager(
        manager,
        sheet,
        team_worksheet_ids=[101, 102],
        summary_worksheet_id=201,
    )
    manager.upsert_sheet_config = AsyncMock()  # type: ignore[method-assign]
    user = make_user()

    with pytest.raises(WorksheetContractError):
        await manager.upsert_user_registration(
            user,
            [],
            TeamParser.parse_line(user, "150/740/33.4"),
            TeamParser.parse_line(user, "140/680/35.3"),
        )

    assert sheet.created_titles == []
    manager.upsert_sheet_config.assert_not_awaited()
    assert sheet.batch_updates == []


@pytest.mark.asyncio
async def test_team_settings_indexes_duplicate_summary_keys_before_creating_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = TeamRegisterManager(make_feature_channel("team_register"), "service.json")
    configure_team_settings_config(manager)
    main_worksheet = FakeTeamGridWorksheet(
        101,
        "Main Team",
        [
            TeamWorksheetContent.COLUMNS,
            ["alice", "Alice", 150, 740, 33.4, "main"],
        ],
    )
    summary_worksheet = FakeTeamGridWorksheet(
        201,
        "Team Summary",
        [
            [
                *SummaryWorksheetContent.COLUMNS,
                "Main Team ISV",
                "Main Team Power",
                "original_message",
            ],
            ["alice", "Alice", "", 268, 33.4, "main"],
            ["alice", "Duplicate", "", 268, 33.4, "duplicate"],
        ],
    )
    sheet = FakeTeamGridSheet([main_worksheet, summary_worksheet])
    monkeypatch.setattr(
        team_register_manager_module,
        "GoogleSheet",
        lambda _url, _path: sheet,
        raising=False,
    )
    manager.upsert_sheet_config = AsyncMock()  # type: ignore[method-assign]

    with pytest.raises(WorksheetContractError):
        await manager.upsert_sheet_config_and_worksheets(
            sheet.sheet_url,
            team_worksheet_titles=["Main Team", "New Team"],
            summary_worksheet_title="Team Summary",
            member_by_names={},
        )

    assert sheet.created_titles == []
    manager.upsert_sheet_config.assert_not_awaited()
    assert sheet.batch_updates == []


@pytest.mark.asyncio
async def test_team_settings_batch_failure_keeps_saved_ids_for_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = TeamRegisterManager(make_feature_channel("team_register"), "service.json")
    configure_team_settings_config(manager)
    main_worksheet = FakeTeamGridWorksheet(
        101,
        "Main Team",
        [
            TeamWorksheetContent.COLUMNS,
            ["alice", "Alice", 150, 740, 33.4, "main"],
        ],
    )
    summary_worksheet = FakeTeamGridWorksheet(
        201,
        "Team Summary",
        [
            [
                *SummaryWorksheetContent.COLUMNS,
                "Main Team ISV",
                "Main Team Power",
                "original_message",
            ]
        ],
    )
    sheet = FakeTeamGridSheet(
        [main_worksheet, summary_worksheet],
        batch_error=GoogleSheetsError(
            GoogleSheetsErrorKind.TRANSIENT,
            "private transient detail",
        ),
    )
    monkeypatch.setattr(
        team_register_manager_module,
        "GoogleSheet",
        lambda _url, _path: sheet,
        raising=False,
    )
    saved_metadata: list[TeamRegisterGoogleSheetsMetadata] = []

    async def save_metadata(metadata: TeamRegisterGoogleSheetsMetadata) -> None:
        saved_metadata.append(metadata)

    manager.upsert_sheet_config = save_metadata  # type: ignore[method-assign]

    with pytest.raises(StorageError) as exc_info:
        await manager.upsert_sheet_config_and_worksheets(
            sheet.sheet_url,
            team_worksheet_titles=["Main Team"],
            summary_worksheet_title="Team Summary",
            member_by_names={},
        )

    assert exc_info.value.kind is StorageErrorKind.PARTIAL_SUCCESS
    assert len(saved_metadata) == 1
    assert [worksheet.id for worksheet in saved_metadata[0]] == [101, 201]
    assert len(sheet.batch_updates) == 1

    sheet.batch_error = None
    await manager.upsert_sheet_config_and_worksheets(
        sheet.sheet_url,
        team_worksheet_titles=["Main Team"],
        summary_worksheet_title="Team Summary",
        member_by_names={},
    )

    assert len(sheet.batch_updates) == 2


@pytest.mark.asyncio
async def test_team_upsert_batch_failure_without_prior_side_effect_stays_original() -> (
    None
):
    manager = TeamRegisterManager(make_feature_channel("team_register"), "service.json")
    main_worksheet = FakeTeamGridWorksheet(
        101,
        "Main Team",
        [TeamWorksheetContent.COLUMNS],
    )
    encore_worksheet = FakeTeamGridWorksheet(
        102,
        "Encore Team",
        [TeamWorksheetContent.COLUMNS],
    )
    summary_worksheet = FakeTeamGridWorksheet(
        201,
        "Team Summary",
        [
            [
                *SummaryWorksheetContent.COLUMNS,
                "Main Team ISV",
                "Main Team Power",
                "Encore Team ISV",
                "Encore Team Power",
                "original_message",
            ]
        ],
    )
    error = GoogleSheetsError(GoogleSheetsErrorKind.TRANSIENT, "private detail")
    sheet = FakeTeamGridSheet(
        [main_worksheet, encore_worksheet, summary_worksheet],
        batch_error=error,
    )
    configure_team_transaction_manager(
        manager,
        sheet,
        team_worksheet_ids=[101, 102],
        summary_worksheet_id=201,
    )
    user = make_user()

    with pytest.raises(GoogleSheetsError) as exc_info:
        await manager.upsert_user_registration(
            user,
            [],
            TeamParser.parse_line(user, "150/740/33.4 main"),
            None,
        )

    assert exc_info.value is error


@pytest.mark.asyncio
async def test_team_upsert_failure_after_worksheet_creation_is_partial() -> None:
    manager = TeamRegisterManager(make_feature_channel("team_register"), "service.json")
    main_worksheet = FakeTeamGridWorksheet(
        101,
        "Main Team",
        [TeamWorksheetContent.COLUMNS],
    )
    summary_worksheet = FakeTeamGridWorksheet(
        201,
        "Team Summary",
        [
            [
                *SummaryWorksheetContent.COLUMNS,
                "Main Team ISV",
                "Main Team Power",
                "Encore Team ISV",
                "Encore Team Power",
                "original_message",
            ]
        ],
    )
    sheet = FakeTeamGridSheet(
        [main_worksheet, summary_worksheet],
        batch_error=GoogleSheetsError(
            GoogleSheetsErrorKind.TRANSIENT,
            "private detail",
        ),
    )
    configure_team_transaction_manager(
        manager,
        sheet,
        team_worksheet_ids=[101, 102],
        summary_worksheet_id=201,
    )
    manager.upsert_sheet_config = AsyncMock()  # type: ignore[method-assign]
    user = make_user()

    with pytest.raises(StorageError) as exc_info:
        await manager.upsert_user_registration(
            user,
            [],
            TeamParser.parse_line(user, "150/740/33.4 main"),
            TeamParser.parse_line(user, "140/680/35.3 encore"),
        )

    assert exc_info.value.kind is StorageErrorKind.PARTIAL_SUCCESS
    assert sheet.create_calls == ["Encore Team"]
    assert sheet.created_titles == ["Encore Team"]
    manager.upsert_sheet_config.assert_awaited_once()


@pytest.mark.asyncio
async def test_team_upsert_partial_creation_failure_is_partial() -> None:
    manager = TeamRegisterManager(make_feature_channel("team_register"), "service.json")
    main_worksheet = FakeTeamGridWorksheet(
        101,
        "Main Team",
        [TeamWorksheetContent.COLUMNS],
    )
    sheet = FakeTeamGridSheet(
        [main_worksheet],
        create_error_title="Team Summary",
    )
    configure_team_transaction_manager(
        manager,
        sheet,
        team_worksheet_ids=[101, 102],
        summary_worksheet_id=201,
    )
    manager.upsert_sheet_config = AsyncMock()  # type: ignore[method-assign]
    user = make_user()

    with pytest.raises(StorageError) as exc_info:
        await manager.upsert_user_registration(
            user,
            [],
            TeamParser.parse_line(user, "150/740/33.4 main"),
            TeamParser.parse_line(user, "140/680/35.3 encore"),
        )

    assert exc_info.value.kind is StorageErrorKind.PARTIAL_SUCCESS
    assert sheet.create_calls == ["Encore Team", "Team Summary"]
    assert sheet.created_titles == ["Encore Team"]
    manager.upsert_sheet_config.assert_not_awaited()
    assert sheet.batch_updates == []


@pytest.mark.asyncio
async def test_team_refresh_batch_failure_without_side_effect_stays_original() -> None:
    manager, sheet, _config = make_encore_reconciliation_manager(
        batch_error=GoogleSheetsError(
            GoogleSheetsErrorKind.TRANSIENT,
            "private detail",
        )
    )
    error = sheet.batch_error

    with pytest.raises(GoogleSheetsError) as exc_info:
        await manager.refresh_summary_registration({})

    assert exc_info.value is error


@pytest.mark.asyncio
async def test_team_refresh_failure_after_worksheet_creation_is_partial() -> None:
    manager = TeamRegisterManager(make_feature_channel("team_register"), "service.json")
    main_worksheet = FakeTeamGridWorksheet(
        101,
        "Main Team",
        [
            TeamWorksheetContent.COLUMNS,
            ["alice", "Alice", 150, 740, 33.4, "main"],
        ],
    )
    sheet = FakeTeamGridSheet(
        [main_worksheet],
        batch_error=GoogleSheetsError(
            GoogleSheetsErrorKind.TRANSIENT,
            "private detail",
        ),
    )
    configure_team_transaction_manager(
        manager,
        sheet,
        team_worksheet_ids=[101],
        summary_worksheet_id=201,
    )
    manager.upsert_sheet_config = AsyncMock()  # type: ignore[method-assign]

    with pytest.raises(StorageError) as exc_info:
        await manager.refresh_summary_registration({})

    assert exc_info.value.kind is StorageErrorKind.PARTIAL_SUCCESS
    assert sheet.created_titles == ["Team Summary"]
    manager.upsert_sheet_config.assert_awaited_once()


@pytest.mark.asyncio
async def test_team_refresh_save_failure_after_worksheet_creation_is_partial() -> None:
    manager = TeamRegisterManager(make_feature_channel("team_register"), "service.json")
    main_worksheet = FakeTeamGridWorksheet(
        101,
        "Main Team",
        [
            TeamWorksheetContent.COLUMNS,
            ["alice", "Alice", 150, 740, 33.4, "main"],
        ],
    )
    sheet = FakeTeamGridSheet([main_worksheet])
    configure_team_transaction_manager(
        manager,
        sheet,
        team_worksheet_ids=[101],
        summary_worksheet_id=201,
    )
    manager.upsert_sheet_config = AsyncMock(  # type: ignore[method-assign]
        side_effect=GoogleSheetsError(
            GoogleSheetsErrorKind.TRANSIENT,
            "private detail",
        )
    )

    with pytest.raises(StorageError) as exc_info:
        await manager.refresh_summary_registration({})

    assert exc_info.value.kind is StorageErrorKind.PARTIAL_SUCCESS
    assert sheet.created_titles == ["Team Summary"]
    assert sheet.batch_updates == []


@pytest.mark.asyncio
async def test_team_manager_reuses_first_fully_blank_rows_in_both_tabs() -> None:
    manager = TeamRegisterManager(make_feature_channel("team_register"), "service.json")
    main_worksheet = FakeTeamGridWorksheet(
        101,
        "Main Team",
        [
            [*TeamWorksheetContent.COLUMNS, "manager_note"],
            ["", "occupied", "", "", "", "", "preserve"],
            ["", "", "", "", "", "", "prepared"],
        ],
    )
    encore_worksheet = FakeTeamGridWorksheet(
        102,
        "Encore Team",
        [TeamWorksheetContent.COLUMNS],
    )
    summary_worksheet = FakeTeamGridWorksheet(
        201,
        "Team Summary",
        [
            [
                *SummaryWorksheetContent.COLUMNS,
                "Main Team ISV",
                "Main Team Power",
                "Encore Team ISV",
                "Encore Team Power",
                "original_message",
                "manager_note",
            ],
            ["", "occupied", "", "", "", "", "", "", "preserve"],
            ["", "", "", "", "", "", "", "", "prepared"],
        ],
    )
    sheet = FakeTeamGridSheet([main_worksheet, encore_worksheet, summary_worksheet])
    configure_team_transaction_manager(
        manager,
        sheet,
        team_worksheet_ids=[101, 102],
        summary_worksheet_id=201,
    )
    user = make_user()
    team = TeamParser.parse_line(user, "150/740/33.4")

    await manager.upsert_user_registration(user, [], team, None)

    assert [
        (mutation.worksheet_id, mutation.start_row_index)
        for mutation in sheet.batch_updates[0]
        if isinstance(mutation, GridValueUpdate)
    ] == [(101, 2), (201, 2)]


@pytest.mark.asyncio
async def test_team_ordinary_upsert_and_delete_ignore_unrelated_bad_numbers() -> None:
    manager = TeamRegisterManager(make_feature_channel("team_register"), "service.json")
    main_worksheet = FakeTeamGridWorksheet(
        101,
        "Main Team",
        [
            TeamWorksheetContent.COLUMNS,
            ["alice", "Old Alice", 100, 500, 20.0, "old"],
            ["bob", "Bob", "not-int", "bad", "bad", "unrelated"],
        ],
    )
    encore_worksheet = FakeTeamGridWorksheet(
        102,
        "Encore Team",
        [TeamWorksheetContent.COLUMNS],
    )
    summary_worksheet = FakeTeamGridWorksheet(
        201,
        "Team Summary",
        [
            [
                *SummaryWorksheetContent.COLUMNS,
                "Main Team ISV",
                "Main Team Power",
                "Encore Team ISV",
                "Encore Team Power",
                "original_message",
            ],
            ["alice", "Old Alice", "", 1, 1, "", "", "old"],
        ],
    )
    sheet = FakeTeamGridSheet([main_worksheet, encore_worksheet, summary_worksheet])
    configure_team_transaction_manager(
        manager,
        sheet,
        team_worksheet_ids=[101, 102],
        summary_worksheet_id=201,
    )
    user = make_user()
    team = TeamParser.parse_line(user, "150/740/33.4")

    await manager.upsert_user_registration(user, [], team, None)
    await manager.delete_user_registration(user)

    assert len(sheet.batch_updates) == 2


@pytest.mark.asyncio
async def test_team_refresh_rejects_unrelated_bad_numbers_without_batch() -> None:
    manager = TeamRegisterManager(make_feature_channel("team_register"), "service.json")
    main_worksheet = FakeTeamGridWorksheet(
        101,
        "Main Team",
        [
            TeamWorksheetContent.COLUMNS,
            ["alice", "Alice", 150, 740, 33.4, "main"],
            ["bob", "Bob", "not-int", "bad", "bad", "unrelated"],
        ],
    )
    summary_worksheet = FakeTeamGridWorksheet(
        201,
        "Team Summary",
        [
            [
                *SummaryWorksheetContent.COLUMNS,
                "Main Team ISV",
                "Main Team Power",
                "original_message",
            ]
        ],
    )
    sheet = FakeTeamGridSheet([main_worksheet, summary_worksheet])
    configure_team_transaction_manager(
        manager,
        sheet,
        team_worksheet_ids=[101],
        summary_worksheet_id=201,
    )

    with pytest.raises(WorksheetContractError):
        await manager.refresh_summary_registration({})

    assert sheet.batch_updates == []


@pytest.mark.asyncio
async def test_team_manager_updates_encore_ids_and_summary_in_one_action() -> None:
    manager, sheet, config = make_encore_reconciliation_manager()
    encore_role = SimpleNamespace(id=7, name="Encore")
    member = SimpleNamespace(display_name="Alice", roles=[encore_role])

    metadata = await manager.update_encore_role_ids_and_summary(
        [encore_role.id],
        {"alice": member},
    )

    assert [worksheet.id for worksheet in metadata] == [101, 201]
    assert config.encore_role_ids == [7]
    config.save.assert_awaited_once()
    assert len(sheet.batch_updates) == 1
    summary_update = next(
        mutation
        for mutation in sheet.batch_updates[0]
        if isinstance(mutation, GridValueUpdate) and mutation.worksheet_id == 201
    )
    assert summary_update.rows[0][2] == "Encore"


@pytest.mark.asyncio
async def test_team_manager_validates_encore_summary_before_saving_ids() -> None:
    manager, sheet, config = make_encore_reconciliation_manager(
        malformed_later_row=True
    )

    with pytest.raises(WorksheetContractError):
        await manager.update_encore_role_ids_and_summary([7], {})

    assert config.encore_role_ids == []
    config.save.assert_not_awaited()
    assert sheet.batch_updates == []


@pytest.mark.asyncio
async def test_team_manager_encore_post_save_failure_is_partial_and_retryable() -> None:
    manager, sheet, config = make_encore_reconciliation_manager(
        batch_error=GoogleSheetsError(
            GoogleSheetsErrorKind.TRANSIENT,
            "private transient detail",
        )
    )

    with pytest.raises(StorageError) as exc_info:
        await manager.update_encore_role_ids_and_summary([7], {})

    assert exc_info.value.kind is StorageErrorKind.PARTIAL_SUCCESS
    assert config.encore_role_ids == [7]
    config.save.assert_awaited_once()
    assert len(sheet.batch_updates) == 1

    sheet.batch_error = None
    await manager.update_encore_role_ids_and_summary([7], {})

    assert config.encore_role_ids == [7]
    assert config.save.await_count == 2
    assert len(sheet.batch_updates) == 2


@pytest.mark.asyncio
async def test_team_encore_save_failure_after_creation_is_partial() -> None:
    manager = TeamRegisterManager(make_feature_channel("team_register"), "service.json")
    main_worksheet = FakeTeamGridWorksheet(
        101,
        "Main Team",
        [
            TeamWorksheetContent.COLUMNS,
            ["alice", "Alice", 150, 740, 33.4, "main"],
        ],
    )
    sheet = FakeTeamGridSheet([main_worksheet])
    configure_team_transaction_manager(
        manager,
        sheet,
        team_worksheet_ids=[101],
        summary_worksheet_id=201,
    )
    config = manager._sheet_config  # noqa: SLF001
    config.save = AsyncMock(
        side_effect=GoogleSheetsError(
            GoogleSheetsErrorKind.TRANSIENT,
            "private save detail",
        )
    )
    manager.upsert_sheet_config = AsyncMock()  # type: ignore[method-assign]

    with pytest.raises(StorageError) as exc_info:
        await manager.update_encore_role_ids_and_summary([7], {})

    assert exc_info.value.kind is StorageErrorKind.PARTIAL_SUCCESS
    assert sheet.created_titles == ["Team Summary"]
    assert sheet.batch_updates == []


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["upsert", "delete"])
async def test_team_contract_rejection_has_zero_batch(operation: str) -> None:
    manager = TeamRegisterManager(make_feature_channel("team_register"), "service.json")
    main_worksheet = FakeTeamGridWorksheet(
        101,
        "Main Team",
        [
            TeamWorksheetContent.COLUMNS,
            ["alice", "Alice", 150, 740, 33.4, "one"],
            ["alice", "Duplicate", 140, 680, 35.3, "two"],
        ],
    )
    encore_worksheet = FakeTeamGridWorksheet(
        102,
        "Encore Team",
        [TeamWorksheetContent.COLUMNS],
    )
    summary_worksheet = FakeTeamGridWorksheet(
        201,
        "Team Summary",
        [
            [
                *SummaryWorksheetContent.COLUMNS,
                "Main Team ISV",
                "Main Team Power",
                "Encore Team ISV",
                "Encore Team Power",
                "original_message",
            ]
        ],
    )
    sheet = FakeTeamGridSheet([main_worksheet, encore_worksheet, summary_worksheet])
    configure_team_transaction_manager(
        manager,
        sheet,
        team_worksheet_ids=[101, 102],
        summary_worksheet_id=201,
    )

    if operation == "upsert":
        user = make_user()
        team = TeamParser.parse_line(user, "150/740/33.4")
        action = manager.upsert_user_registration(user, [], team, None)
    else:
        action = manager.delete_user_registration(make_user())

    with pytest.raises(WorksheetContractError):
        await action

    assert sheet.batch_updates == []


@pytest.mark.asyncio
async def test_shift_sheet_setup_initializes_entry_presentation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = ShiftRegisterManager(
        make_feature_channel("shift_register"), "service.json"
    )
    sheet = FakeShiftSetupGoogleSheet(
        [
            FakeEntryWorksheet(rows=[], title="Entry", worksheet_id=1),
            FakeEntryWorksheet(rows=[], title="Draft", worksheet_id=2),
            FakeEntryWorksheet(rows=[], title="Final", worksheet_id=3),
        ]
    )
    monkeypatch.setattr(
        shift_register_manager,
        "GoogleSheet",
        lambda _url, _path: sheet,
    )
    manager.upsert_sheet_config = AsyncMock()  # type: ignore[method-assign]
    manager.get_sheet_config = AsyncMock(
        return_value=SimpleNamespace(recruitment_time_ranges=[{"start": 4, "end": 28}])
    )
    manager._sync_entry_presentation_locked = AsyncMock()  # noqa: SLF001

    result = await manager.upsert_sheet_config_and_worksheets(
        sheet.sheet_url,
        entry_worksheet_title="Entry",
        draft_worksheet_title="Draft",
        final_schedule_worksheet_title="Final",
    )

    assert [worksheet.id for worksheet in result] == [1, 2, 3]
    assert sheet.calls == [["Entry"], ["Draft", "Final"]]
    manager.upsert_sheet_config.assert_awaited_once_with(result)
    manager._sync_entry_presentation_locked.assert_awaited_once()  # noqa: SLF001
    sync_metadata, sync_ranges = manager._sync_entry_presentation_locked.await_args.args  # noqa: SLF001
    assert sync_metadata is result
    assert sync_ranges.to_json() == [{"start": 4, "end": 28}]
    assert manager._sync_entry_presentation_locked.await_args.kwargs == {  # noqa: SLF001
        "entry_grid": [],
        "force": True,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "titles",
    [
        ("Entry", "Entry", "Final"),
        ("Entry", "", "Final"),
    ],
)
async def test_shift_sheet_setup_rejects_invalid_titles_before_sheet_access(
    monkeypatch: pytest.MonkeyPatch,
    titles: tuple[str, str, str],
) -> None:
    manager = ShiftRegisterManager(
        make_feature_channel("shift_register"), "service.json"
    )
    sheet_factory = Mock()
    monkeypatch.setattr(shift_register_manager, "GoogleSheet", sheet_factory)

    with pytest.raises(WorksheetContractError):
        await manager.upsert_sheet_config_and_worksheets(
            FakeShiftSetupGoogleSheet.sheet_url,
            entry_worksheet_title=titles[0],
            draft_worksheet_title=titles[1],
            final_schedule_worksheet_title=titles[2],
        )

    sheet_factory.assert_not_called()


@pytest.mark.asyncio
async def test_shift_sheet_setup_preflights_entry_before_creating_other_tabs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = ShiftRegisterManager(
        make_feature_channel("shift_register"), "service.json"
    )
    entry = FakeEntryWorksheet(
        current_entry_rows(
            entry_participant_row("alice", "Alice"),
            entry_participant_row("alice", "Duplicate"),
        ),
        title="Entry",
        worksheet_id=1,
    )
    sheet = FakeShiftSetupGoogleSheet([entry])
    monkeypatch.setattr(
        shift_register_manager,
        "GoogleSheet",
        lambda _url, _path: sheet,
    )
    manager.upsert_sheet_config = AsyncMock()  # type: ignore[method-assign]

    with pytest.raises(WorksheetContractError):
        await manager.upsert_sheet_config_and_worksheets(
            sheet.sheet_url,
            entry_worksheet_title="Entry",
            draft_worksheet_title="Draft",
            final_schedule_worksheet_title="Final",
        )

    assert sheet.created_titles == []
    manager.upsert_sheet_config.assert_not_awaited()


@pytest.mark.asyncio
async def test_shift_sheet_setup_saves_normalized_anchor_with_worksheet_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = ShiftRegisterManager(
        make_feature_channel("shift_register"), "service.json"
    )
    sheet = FakeShiftSetupGoogleSheet(
        [
            FakeEntryWorksheet(rows=[], title="Entry", worksheet_id=1),
            FakeEntryWorksheet(rows=[], title="Draft", worksheet_id=2),
            FakeEntryWorksheet(rows=[], title="Final", worksheet_id=3),
        ]
    )
    monkeypatch.setattr(
        shift_register_manager,
        "GoogleSheet",
        lambda _url, _path: sheet,
    )
    manager.upsert_sheet_config = AsyncMock()  # type: ignore[method-assign]
    manager.get_sheet_config = AsyncMock(
        return_value=SimpleNamespace(recruitment_time_ranges=[{"start": 4, "end": 28}])
    )
    manager._sync_entry_presentation_locked = AsyncMock()  # noqa: SLF001

    metadata = await manager.upsert_sheet_config_and_worksheets(
        sheet.sheet_url,
        entry_worksheet_title="Entry",
        draft_worksheet_title="Draft",
        final_schedule_worksheet_title="Final",
        final_schedule_anchor_cell="invalid anchor",
    )

    assert [worksheet.id for worksheet in metadata] == [1, 2, 3]
    assert sheet.calls == [["Entry"], ["Draft", "Final"]]
    manager.upsert_sheet_config.assert_awaited_once_with(
        metadata,
        extra_defaults={"final_schedule_anchor_cell": "A1"},
    )


@pytest.mark.asyncio
async def test_shift_sheet_setup_contract_failure_precedes_config_save(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = ShiftRegisterManager(
        make_feature_channel("shift_register"), "service.json"
    )
    worksheet = FakeEntryWorksheet(
        current_entry_rows(
            entry_participant_row("alice", "Alice"),
            entry_participant_row("alice", "Duplicate"),
        )
    )
    worksheet.title = "Entry"
    sheet = FakeShiftSetupGoogleSheet([worksheet])
    monkeypatch.setattr(
        shift_register_manager,
        "GoogleSheet",
        lambda _url, _path: sheet,
    )
    manager.upsert_sheet_config = AsyncMock()  # type: ignore[method-assign]
    manager.get_sheet_config = AsyncMock(
        return_value=SimpleNamespace(recruitment_time_ranges=[{"start": 4, "end": 28}])
    )

    with pytest.raises(WorksheetContractError):
        await manager.upsert_sheet_config_and_worksheets(
            "https://docs.google.com/spreadsheets/d/shift-settings/edit",
            entry_worksheet_title="Entry",
            draft_worksheet_title="Draft",
            final_schedule_worksheet_title="Final",
        )

    assert sheet.calls == [["Entry"]]
    assert sheet.created_titles == []
    manager.upsert_sheet_config.assert_not_awaited()
    assert worksheet.batch_updates == []


@pytest.mark.asyncio
async def test_shift_manager_initializes_empty_entry_worksheet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[tuple[str, object]] = []
    monkeypatch.setattr(
        manager_base_module,
        "WORKSHEET_TRANSACTION_LOCK",
        RecordingKeyLock("worksheet", events),
    )
    manager = ShiftRegisterManager(
        make_feature_channel("shift_register"), "service.json"
    )
    sheet = configure_row_source(
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

    assert ("enter_worksheet", ("shift-transaction", 1)) in events
    assert sheet.batch_reads == [[1]]
    assert len(worksheet.typed_batch_updates) == 1
    assert [item["range"] for item in worksheet.typed_batch_updates[0]] == [
        "A1",
        "F1:AI1",
        "A2:AJ2",
        "A3:B3",
        "F3:AJ3",
    ]
    assert worksheet.rows[0][0] == "count"
    assert worksheet.rows[0][5:35] == EntryWorksheetContent.count_row()[5:35]
    assert worksheet.rows[1][:36] == EntryWorksheetContent.COLUMNS
    assert worksheet.rows[2][0:2] == ["alice", "Alice"]
    assert worksheet.rows[2][9] == 1
    assert worksheet.rows[2][13] == 0
    assert worksheet.typed_minimums == [(3, 36)]


@pytest.mark.asyncio
async def test_shift_manager_repairs_only_required_count_ranges() -> None:
    manager = ShiftRegisterManager(
        make_feature_channel("shift_register"), "service.json"
    )
    sheet = configure_row_source(
        manager,
        shift_register_manager.TeamSourceResolution(
            shift_register_manager.TeamSourceStatus.MISSING
        ),
    )
    count_row = EntryWorksheetContent.count_row()
    count_row[0] = "stale count label"
    count_row[1:5] = ["manual B", "manual C", "manual D", "manual E"]
    count_row[5] = "=WRONG"
    count_row[35] = "manual AJ"
    worksheet = FakeEntryWorksheet([count_row, EntryWorksheetContent.COLUMNS])
    shift = ShiftParser.parse_submission(make_user(), ["4-8"]).shift
    assert shift is not None

    await manager.upsert_or_delete_user_shift(
        make_user(), shift, make_shift_metadata(worksheet)
    )

    assert sheet.batch_reads == [[1]]
    assert [item["range"] for item in worksheet.typed_batch_updates[0]][:2] == [
        "A1",
        "F1:AI1",
    ]
    assert worksheet.rows[0][1:5] == ["manual B", "manual C", "manual D", "manual E"]
    assert worksheet.rows[0][35] == "manual AJ"


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
    count_gap_rule = next(
        rule for rule in plan.conditional_format_rules if "gap-count:v1" in str(rule)
    )
    assert count_gap_rule["ranges"] == [
        {
            "sheetId": 1,
            "startRowIndex": 0,
            "endRowIndex": 1,
            "startColumnIndex": 17,
            "endColumnIndex": 25,
        }
    ]
    assert count_gap_rule["booleanRule"]["format"] == {
        "backgroundColorStyle": {"rgbColor": {"red": 0.8, "green": 0.8, "blue": 0.8}},
        "textFormat": {
            "foregroundColorStyle": {
                "rgbColor": {
                    "red": 183 / 255,
                    "green": 183 / 255,
                    "blue": 183 / 255,
                }
            }
        },
    }
    header_gap_rule = next(
        rule for rule in plan.conditional_format_rules if "gap-header:v1" in str(rule)
    )
    assert header_gap_rule["ranges"] == [
        {
            "sheetId": 1,
            "startRowIndex": 1,
            "endRowIndex": 2,
            "startColumnIndex": 17,
            "endColumnIndex": 25,
        }
    ]
    assert header_gap_rule["booleanRule"]["format"] == {
        "backgroundColorStyle": {
            "rgbColor": {"red": 183 / 255, "green": 183 / 255, "blue": 183 / 255}
        },
        "textFormat": {
            "foregroundColorStyle": {
                "rgbColor": {"red": 0.6, "green": 0.6, "blue": 0.6}
            }
        },
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
    sheet = configure_shift_value_sheet(manager)
    worksheet = FakeEntryWorksheet(rows=[], row_count=2, col_count=20)

    await manager.sync_entry_presentation(
        make_shift_metadata(worksheet),
        RecruitmentTimeRanges.from_modal_input("4-12, 20-28"),
        force=True,
    )

    assert [item["range"] for item in worksheet.typed_batch_updates[-1]] == [
        "A1",
        "F1:AI1",
        "A2:AJ2",
    ]
    assert worksheet.rows[0][0] == "count"
    assert worksheet.rows[0][5:35] == EntryWorksheetContent.count_row()[5:35]
    assert worksheet.rows[1][:36] == EntryWorksheetContent.COLUMNS
    assert len(worksheet.rows) == 2
    assert worksheet.typed_minimums == [(3, 36)]
    assert worksheet.presentation_updates[-1]["frozen_column_count"] == 5
    assert sheet.batch_reads == [[1]]


@pytest.mark.asyncio
async def test_empty_shift_entry_repairs_stale_a1_and_blank_header() -> None:
    manager = ShiftRegisterManager(
        make_feature_channel("shift_register"), "service.json"
    )
    configure_shift_value_sheet(manager)
    worksheet = FakeEntryWorksheet([["stale count label"]], row_count=2)

    await manager.sync_entry_presentation(
        make_shift_metadata(worksheet),
        RecruitmentTimeRanges.default(),
        force=True,
    )

    assert [item["range"] for item in worksheet.typed_batch_updates[-1]] == [
        "A1",
        "F1:AI1",
        "A2:AJ2",
    ]
    assert worksheet.rows[0][0] == "count"
    assert worksheet.rows[1][:36] == EntryWorksheetContent.COLUMNS


@pytest.mark.asyncio
async def test_recruitment_range_contract_failure_precedes_config_save() -> None:
    manager = ShiftRegisterManager(
        make_feature_channel("shift_register"), "service.json"
    )
    configure_shift_value_sheet(manager)
    config = SimpleNamespace(
        recruitment_time_ranges=[{"start": 4, "end": 28}],
        save=AsyncMock(),
    )
    manager._sheet_config = config  # type: ignore[assignment]  # noqa: SLF001
    worksheet = FakeEntryWorksheet(
        current_entry_rows(
            entry_participant_row("alice", "Alice"),
            entry_participant_row("alice", "Duplicate"),
        )
    )
    manager.fetch_google_sheets_metadata = AsyncMock(  # type: ignore[method-assign]
        return_value=make_shift_metadata(worksheet)
    )

    with pytest.raises(WorksheetContractError):
        await manager.update_recruitment_time_ranges(
            RecruitmentTimeRanges.from_modal_input("4-12")
        )

    config.save.assert_not_awaited()
    assert worksheet.batch_updates == []


@pytest.mark.asyncio
async def test_entry_presentation_sync_rejects_duplicate_usernames() -> None:
    manager = ShiftRegisterManager(
        make_feature_channel("shift_register"), "service.json"
    )
    configure_shift_value_sheet(manager)
    worksheet = FakeEntryWorksheet(
        current_entry_rows(
            entry_participant_row("alice", "Alice"),
            entry_participant_row("alice", "Duplicate"),
        )
    )

    with pytest.raises(WorksheetContractError):
        await manager.sync_entry_presentation(
            make_shift_metadata(worksheet),
            RecruitmentTimeRanges.default(),
            force=True,
        )

    assert worksheet.typed_batch_updates == []


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
async def test_existing_shift_updates_owned_ranges_on_same_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[tuple[str, object]] = []
    monkeypatch.setattr(
        manager_base_module,
        "WORKSHEET_TRANSACTION_LOCK",
        RecordingKeyLock("worksheet", events),
    )
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

    assert [event for event in events if event[0] == "enter_worksheet"] == [
        ("enter_worksheet", ("shift-transaction", 1)),
        ("enter_worksheet", ("team-source", 201)),
    ]
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
    configure_row_source(manager, resolution)
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
    resolution = available_team_source()
    configure_row_source(manager, resolution)
    worksheet = FakeEntryWorksheet(
        current_entry_rows(
            entry_participant_row("alice", "Alice", expected_formula(3)),
        )
    )

    changed = await manager.repair_team_references(
        make_shift_metadata(worksheet), resolution
    )

    assert changed == 0
    assert worksheet.batch_updates == []


@pytest.mark.asyncio
async def test_select_team_source_preflights_before_persist_and_repair() -> None:
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

    configure_row_source(manager, resolution)

    async def resolve_metadata(
        *, team_channel_id: int | None = None
    ) -> tuple[object, object, object]:
        assert team_channel_id == 22
        source = resolution.source
        assert source is not None
        return resolution.status, source.config, source.metadata

    async def fetch() -> ShiftRegisterGoogleSheetsMetadata:
        events.append("fetch")
        return make_shift_metadata(FakeEntryWorksheet(current_entry_rows()))

    async def repair(*_args: object, **_kwargs: object) -> int:
        events.append("repair")
        return 0

    manager._resolve_team_source_metadata = resolve_metadata  # type: ignore[method-assign]  # noqa: SLF001
    manager.fetch_google_sheets_metadata = fetch  # type: ignore[method-assign]
    manager._repair_team_references_locked = repair  # type: ignore[method-assign]  # noqa: SLF001
    manager._sheet_config = config  # type: ignore[assignment]  # noqa: SLF001

    result = await manager.select_team_source_and_repair(22)

    assert result is resolution
    assert config.team_source_feature_channel_id == 22
    assert events == [
        "fetch",
        ("save", ["team_source_feature_channel_id", "updated_at"]),
        "repair",
    ]


@pytest.mark.asyncio
async def test_shift_delete_removes_physical_username_row() -> None:
    manager = ShiftRegisterManager(
        make_feature_channel("shift_register"), "service.json"
    )
    configure_shift_value_sheet(manager)
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
    reusable = entry_participant_row("", "", manual_value="prepared")
    reusable[3:5] = ["manual blocker", "#REF!"]
    worksheet = FakeEntryWorksheet(
        current_entry_rows(
            entry_participant_row("bob", "Bob", expected_formula(3)),
            reusable,
            entry_participant_row("carol", "Carol", expected_formula(5)),
        )
    )
    shift = ShiftParser.parse_submission(make_user(), ["4-8"]).shift
    assert shift is not None

    await manager.upsert_or_delete_user_shift(
        make_user(), shift, make_shift_metadata(worksheet)
    )

    assert worksheet.rows[3][0] == "alice"
    assert worksheet.rows[3][3:5] == ["manual blocker", "#REF!"]
    assert worksheet.rows[3][36] == "prepared"


@pytest.mark.asyncio
async def test_shift_manager_skips_occupied_blank_username_row() -> None:
    manager = ShiftRegisterManager(
        make_feature_channel("shift_register"), "service.json"
    )
    configure_row_source(manager, available_team_source())
    occupied = entry_participant_row(
        "",
        "manual identity",
        "=KEEP",
        slots={0},
        original_message="manual availability",
    )
    worksheet = FakeEntryWorksheet(
        current_entry_rows(
            occupied,
            entry_participant_row("bob", "Bob", expected_formula(4)),
        )
    )
    shift = ShiftParser.parse_submission(make_user(), ["4-8"]).shift
    assert shift is not None

    await manager.upsert_or_delete_user_shift(
        make_user(), shift, make_shift_metadata(worksheet)
    )

    assert worksheet.rows[2][:3] == ["", "manual identity", "=KEEP"]
    assert worksheet.rows[2][5] == 1
    assert worksheet.rows[2][35] == "manual availability"
    assert worksheet.rows[4][0] == "alice"


@pytest.mark.asyncio
async def test_unrelated_malformed_availability_does_not_block_registration() -> None:
    manager = ShiftRegisterManager(
        make_feature_channel("shift_register"), "service.json"
    )
    configure_row_source(manager, available_team_source())
    malformed = entry_participant_row("bob", "Bob", expected_formula(3))
    malformed[5] = "not binary"
    worksheet = FakeEntryWorksheet(current_entry_rows(malformed))
    shift = ShiftParser.parse_submission(make_user(), ["4-8"]).shift
    assert shift is not None

    await manager.upsert_or_delete_user_shift(
        make_user(), shift, make_shift_metadata(worksheet)
    )

    assert worksheet.rows[2][5] == "not binary"
    assert worksheet.rows[3][0] == "alice"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "rows",
    [
        [
            ["username", "display_name"],
            [],
            entry_participant_row("alice", "Alice"),
        ],
        current_entry_rows(
            entry_participant_row("alice", "Alice"),
            entry_participant_row("alice", "Duplicate"),
        ),
        [
            EntryWorksheetContent.count_row(),
            [
                "username",
                "display_name",
                "",
                "",
                "",
                *EntryWorksheetContent.HOUR_COLUMNS,
                "original_message",
            ],
        ],
    ],
    ids=["legacy-row-one-header", "duplicate-username", "noncanonical-header"],
)
async def test_shift_manager_raises_contract_error_before_update(
    rows: list[list[object]],
) -> None:
    manager = ShiftRegisterManager(
        make_feature_channel("shift_register"), "service.json"
    )
    configure_shift_value_sheet(manager)
    worksheet = FakeEntryWorksheet(rows)
    shift = ShiftParser.parse_submission(make_user(), ["4-8"]).shift
    assert shift is not None

    with pytest.raises(WorksheetContractError):
        await manager.upsert_or_delete_user_shift(
            make_user(), shift, make_shift_metadata(worksheet)
        )

    assert worksheet.batch_updates == []
    assert worksheet.deleted_rows == []


@pytest.mark.asyncio
async def test_shift_manager_skips_missing_worksheets_without_updates() -> None:
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

    await shift_manager.upsert_or_delete_user_shift(make_user(), None, metadata)

    assert isinstance(metadata.sheet_url, str)
