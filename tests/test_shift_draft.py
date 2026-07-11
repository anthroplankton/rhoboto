from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING

import pandas as pd
import pytest

from tests.fakes import FakeWorksheet
from utils.shift_register_manager import ShiftRegisterManager
from utils.shift_register_structs import (
    DraftWorksheetContent,
    DraftWorksheetMetadata,
    EntryWorksheetContent,
    EntryWorksheetMetadata,
    FinalScheduleWorksheetMetadata,
    Shift,
    ShiftParser,
    ShiftRegisterGoogleSheetsMetadata,
)
from utils.shift_scheduler import ShiftScheduler
from utils.storage_errors import StorageError, StorageErrorKind

if TYPE_CHECKING:
    from collections.abc import Iterable


def make_feature_channel() -> SimpleNamespace:
    return SimpleNamespace(guild_id=1, channel_id=2, feature_name="shift_register")


class RawDataFakeWorksheet(FakeWorksheet):
    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self.raw_data_calls: list[bool] = []

    async def update_from_dataframe(
        self,
        dataframe: pd.DataFrame,
        *,
        raw_data: bool = False,
    ) -> None:
        self.raw_data_calls.append(raw_data)
        await super().update_from_dataframe(dataframe)


class EntryRangeFakeWorksheet(FakeWorksheet):
    def __init__(self, range_values: list[list[list[object]]]) -> None:
        super().__init__(title="Shift Entry")
        self.range_values = range_values
        self.batch_get_calls: list[list[str]] = []
        self.ignored_values = {
            "A1": "count formula row",
            "C3:E3": "Team display formulas",
            "AK3": "admin-owned value",
        }

    async def batch_get_values(
        self,
        ranges: list[str],
    ) -> list[list[list[object]]]:
        self.batch_get_calls.append(ranges)
        assert ranges == ["2:2", "A3:B", "F3:AJ"]
        return self.range_values

    async def to_frame(self) -> pd.DataFrame:
        msg = "draft generation must not use the legacy whole-frame read"
        raise AssertionError(msg)


def build_entry_frame(rows: list[tuple[str, str, set[int]]]) -> pd.DataFrame:
    records = []
    for username, display_name, slots in rows:
        record: dict[str, object] = {
            "username": username,
            "display_name": display_name,
            "original_message": "",
        }
        for index, label in enumerate(ShiftParser.HOUR_LABELS):
            record[label] = 1 if index in slots else 0
        records.append(record)
    return pd.DataFrame(records, columns=EntryWorksheetContent.COLUMNS)


def build_entry_ranges(
    rows: list[tuple[str, str, set[int]]],
) -> list[list[list[object]]]:
    identities = []
    availability = []
    for username, display_name, slots in rows:
        identities.append([username, display_name])
        availability.append(
            [
                *(1 if index in slots else 0 for index in range(30)),
                "",
            ]
        )
    return [[EntryWorksheetContent.COLUMNS], identities, availability]


def make_shift(username: str, slots: Iterable[int]) -> Shift:
    return Shift(
        username=username,
        display_name=username.capitalize(),
        original_message="",
        slots=set(slots),
    )


def test_to_shifts_reads_slots_from_worksheet() -> None:
    frame = build_entry_frame([("alice", "Alice", {4, 5, 6})])
    shift_df, plain_df = EntryWorksheetContent.standardize_dataframe(frame)
    content = EntryWorksheetContent(shift_df, plain_df)

    shifts = content.to_shifts()

    assert len(shifts) == 1
    shift = shifts[0]
    assert shift.username == "alice"
    assert shift.display_name == "Alice"
    assert 4 in shift
    assert 6 in shift
    assert 7 not in shift


def test_shifts_from_ranges_reads_current_entry_owned_columns() -> None:
    availability = [
        1 if index in {4, 6} else 0 for index in range(len(ShiftParser.HOUR_LABELS))
    ]

    shifts = EntryWorksheetContent.shifts_from_ranges(
        [EntryWorksheetContent.COLUMNS],
        [["alice", "Alice"]],
        [[*availability, "original"]],
    )

    assert shifts == [
        Shift(
            username="alice",
            display_name="Alice",
            original_message="original",
            slots={4, 6},
        )
    ]


def test_from_schedule_renders_lane_columns() -> None:
    shifts = [make_shift("a", {4, 5}), make_shift("b", {4, 5})]
    schedule = ShiftScheduler.assign(shifts, [4, 5], runner="Run")

    frame = DraftWorksheetContent.from_schedule(schedule)

    assert list(frame.columns) == DraftWorksheetContent.COLUMNS
    assert list(frame["JST"]) == ["4-5", "5-6"]
    assert (frame["ランナー"] == "Run").all()
    first_row = frame.iloc[0]
    assert {first_row["アンコ"], first_row["本走①"]} == {"A", "B"}
    # Only two people, so the standby seat stays empty.
    assert first_row["待機"] == ""


def test_from_schedule_with_no_hours_is_header_only() -> None:
    schedule = ShiftScheduler.assign([], [], runner=None)

    frame = DraftWorksheetContent.from_schedule(schedule)

    assert list(frame.columns) == DraftWorksheetContent.COLUMNS
    assert frame.empty


@pytest.mark.asyncio
async def test_generate_draft_writes_draft_worksheet() -> None:
    manager = ShiftRegisterManager(make_feature_channel(), "service.json")
    manager._sheet_config = SimpleNamespace(  # noqa: SLF001
        recruitment_time_ranges=[{"start": 4, "end": 7}]
    )
    entry_worksheet = EntryRangeFakeWorksheet(
        build_entry_ranges(
            [
                ("alice", "Alice", {4, 5, 6}),
                ("bob", "Bob", {4, 5}),
                ("carol", "Carol", {6}),
            ]
        )
    )
    draft_worksheet = RawDataFakeWorksheet(title="Shift Draft")
    metadata = ShiftRegisterGoogleSheetsMetadata(
        "https://sheet.example",
        [
            EntryWorksheetMetadata(1, "Shift Entry", entry_worksheet),
            DraftWorksheetMetadata(2, "Shift Draft", draft_worksheet),
            FinalScheduleWorksheetMetadata(3, "Shift Final Schedule", None),
        ],
    )

    schedule = await manager.generate_draft(metadata, runner="Run")

    written = draft_worksheet.updated_frames[-1]
    assert list(written.columns) == DraftWorksheetContent.COLUMNS
    # Recruitment range 4-7 -> slots 4, 5, 6.
    assert list(written["JST"]) == ["4-5", "5-6", "6-7"]
    assert (written["ランナー"] == "Run").all()
    assert draft_worksheet.raw_data_calls == [True]
    assert schedule.hours == [4, 5, 6]
    assert entry_worksheet.batch_get_calls == [["2:2", "A3:B", "F3:AJ"]]


@pytest.mark.asyncio
async def test_generate_draft_rejects_old_entry_header() -> None:
    manager = ShiftRegisterManager(make_feature_channel(), "service.json")
    manager._sheet_config = SimpleNamespace(  # noqa: SLF001
        recruitment_time_ranges=[{"start": 4, "end": 7}]
    )
    old_columns = [
        "username",
        "display_name",
        *[f"{hour}-{hour + 1}" for hour in range(4, 28)],
        "original_message",
    ]
    entry_worksheet = EntryRangeFakeWorksheet(
        [[old_columns], [], []],
    )
    draft_worksheet = FakeWorksheet(title="Shift Draft")
    metadata = ShiftRegisterGoogleSheetsMetadata(
        "https://sheet.example",
        [
            EntryWorksheetMetadata(1, "Shift Entry", entry_worksheet),
            DraftWorksheetMetadata(2, "Shift Draft", draft_worksheet),
            FinalScheduleWorksheetMetadata(3, "Shift Final Schedule", None),
        ],
    )

    with pytest.raises(StorageError) as exc_info:
        await manager.generate_draft(metadata, runner="Run")

    assert exc_info.value.kind is StorageErrorKind.MALFORMED_SHEET
    assert draft_worksheet.updated_frames == []


@pytest.mark.asyncio
async def test_generate_draft_raises_when_draft_worksheet_missing() -> None:
    manager = ShiftRegisterManager(make_feature_channel(), "service.json")
    manager._sheet_config = SimpleNamespace(  # noqa: SLF001
        recruitment_time_ranges=[{"start": 4, "end": 7}]
    )
    entry_worksheet = FakeWorksheet(
        title="Shift Entry",
        frame=build_entry_frame([("alice", "Alice", {4, 5})]),
    )
    metadata = ShiftRegisterGoogleSheetsMetadata(
        "https://sheet.example",
        [
            EntryWorksheetMetadata(1, "Shift Entry", entry_worksheet),
            DraftWorksheetMetadata(2, "Shift Draft", None),
            FinalScheduleWorksheetMetadata(3, "Shift Final Schedule", None),
        ],
    )

    with pytest.raises(StorageError) as exc_info:
        await manager.generate_draft(metadata, runner="Run")

    assert exc_info.value.kind is StorageErrorKind.GOOGLE_SHEETS_MISSING_WORKSHEET
