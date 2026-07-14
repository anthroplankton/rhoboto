from __future__ import annotations

import copy
from datetime import date
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest

from utils.google_sheets_errors import GoogleSheetsError, GoogleSheetsErrorKind
from utils.shift_final import (
    FinalScheduleValidationError,
    ScheduleUpdateRequest,
    build_schedule_update_request,
    parse_a1_range,
)
from utils.shift_register_manager import (
    FinalScheduleReconfirmationRequired,
    ShiftRegisterManager,
)
from utils.shift_register_structs import (
    DraftWorksheetContent,
    DraftWorksheetMetadata,
    EntryWorksheetMetadata,
    FinalScheduleWorksheetMetadata,
    RecruitmentTimeRanges,
    ShiftRegisterGoogleSheetsMetadata,
)
from utils.shift_scheduler import hour_label
from utils.storage_errors import StorageError, StorageErrorKind
from utils.structs_base import WorksheetContractError

if TYPE_CHECKING:
    from collections.abc import Sequence


class FinalBatchWorksheet:
    def __init__(
        self,
        worksheet_id: int,
        title: str,
        *,
        row_count: int = 2,
        col_count: int = 7,
    ) -> None:
        self.id = worksheet_id
        self.title = title
        self.row_count = row_count
        self.col_count = col_count
        self.typed_calls: list[dict[str, object]] = []

    def typed_update_requests(
        self,
        data: list[dict[str, object]],
        **kwargs: object,
    ) -> list[dict[str, object]]:
        self.typed_calls.append({"data": data, **kwargs})
        return list(data)


class FinalValueSheet:
    def __init__(  # noqa: PLR0913
        self,
        draft: FinalBatchWorksheet,
        final: FinalBatchWorksheet,
        *,
        draft_grid: list[list[object]] | None = None,
        final_grid: list[list[object]] | None = None,
        read_error: Exception | None = None,
        write_error: Exception | None = None,
    ) -> None:
        self.draft = draft
        self.final = final
        self.batch_reads: list[list[int]] = []
        self.batch_updates: list[list[dict[str, object]]] = []
        self.draft_grid = draft_grid or [
            list(DraftWorksheetContent.COLUMNS),
            [hour_label(4), "Runner", "=MANUAL()", "A", "", "", ""],
            [hour_label(5), "", "", "", "", "", ""],
        ]
        self.final_grid = final_grid if final_grid is not None else []
        self.read_error = read_error
        self.write_error = write_error

    async def batch_get_worksheet_values(
        self,
        worksheets: Sequence[FinalBatchWorksheet],
    ) -> dict[int, list[list[object]]]:
        self.batch_reads.append([worksheet.id for worksheet in worksheets])
        if self.read_error is not None:
            raise self.read_error
        return {
            worksheet.id: (
                self.final_grid if worksheet.id == self.final.id else self.draft_grid
            )
            for worksheet in worksheets
        }

    async def batch_update_grid(
        self,
        _mutations: Sequence[object],
        *,
        worksheet_requests: Sequence[dict[str, object]] = (),
    ) -> None:
        self.batch_updates.append(copy.deepcopy(list(worksheet_requests)))
        if self.write_error is not None:
            raise self.write_error


def make_request() -> ScheduleUpdateRequest:
    return build_schedule_update_request(
        recruitment_ranges=RecruitmentTimeRanges.from_json([{"start": 4, "end": 6}]),
        saved_anchor="B2",
        supplied_anchor=None,
        event_date=None,
        event_day_anchor=None,
        event_day_format=None,
    )


def make_request_with_anchor(supplied_anchor: str | None) -> ScheduleUpdateRequest:
    return build_schedule_update_request(
        recruitment_ranges=RecruitmentTimeRanges.from_json([{"start": 4, "end": 6}]),
        saved_anchor="B2",
        supplied_anchor=supplied_anchor,
        event_date=None,
        event_day_anchor=None,
        event_day_format=None,
    )


def make_event_request(
    *,
    saved_anchor: str = "B2",
    supplied_anchor: str | None = None,
    event_day_anchor: str | None = "A1",
    event_day_format: str | None = None,
) -> ScheduleUpdateRequest:
    return build_schedule_update_request(
        recruitment_ranges=RecruitmentTimeRanges.from_json(
            [{"start": 4, "end": 5}, {"start": 6, "end": 7}]
        ),
        saved_anchor=saved_anchor,
        supplied_anchor=supplied_anchor,
        event_date=date(2026, 12, 21),
        event_day_anchor=event_day_anchor,
        event_day_format=event_day_format,
    )


def draft_grid_for_event() -> list[list[object]]:
    return [
        list(DraftWorksheetContent.COLUMNS),
        [hour_label(4), "Runner", "Encore", "A", "B", "C", "Standby"],
        [hour_label(5), "", "", "", "", "", ""],
        [hour_label(6), "Runner", "Encore", "A", "B", "C", "Standby"],
    ]


def make_metadata(
    draft: FinalBatchWorksheet,
    final: FinalBatchWorksheet | None,
) -> ShiftRegisterGoogleSheetsMetadata:
    return ShiftRegisterGoogleSheetsMetadata(
        "https://docs.google.com/spreadsheets/d/final/edit",
        [
            EntryWorksheetMetadata(1, "Shift Entry", None),
            DraftWorksheetMetadata(draft.id, draft.title, draft),
            FinalScheduleWorksheetMetadata(
                3 if final is None else final.id,
                "Shift Final Schedule" if final is None else final.title,
                final,
            ),
        ],
    )


def make_manager(
    sheet: FinalValueSheet,
    metadata: ShiftRegisterGoogleSheetsMetadata,
    *,
    config: SimpleNamespace | None = None,
) -> ShiftRegisterManager:
    manager = ShiftRegisterManager(
        SimpleNamespace(guild_id=1, channel_id=2, feature_name="shift_register"),
        "service.json",
    )
    manager._google_sheet = sheet  # noqa: SLF001
    manager._sheet_config = config or SimpleNamespace(  # noqa: SLF001
        final_schedule_anchor_cell="B2",
        save=AsyncMock(),
    )
    manager._ensure_current_worksheets = AsyncMock(  # type: ignore[method-assign]  # noqa: SLF001
        return_value=(metadata, False)
    )
    return manager


@pytest.mark.asyncio
async def test_update_from_draft_reads_only_draft_and_writes_one_batch() -> None:
    draft = FinalBatchWorksheet(2, "Shift Draft")
    final = FinalBatchWorksheet(3, "Shift Final Schedule")
    sheet = FinalValueSheet(draft, final)
    metadata = make_metadata(draft, final)
    manager = make_manager(sheet, metadata)

    result = await manager.update_schedule_from_draft(
        metadata,
        request=make_request(),
    )

    assert result.schedule.values == [
        ["Runner", "=MANUAL()", "A", "", "", ""],
        ["", "", "", "", "", ""],
    ]
    assert sheet.batch_reads == [[draft.id]]
    assert len(sheet.batch_updates) == 1
    assert final.typed_calls[0]["formula_ranges"] == set()
    assert final.typed_calls[0]["data"][0]["range"] == "B2:G3"


@pytest.mark.asyncio
async def test_final_role_source_default_range_and_sparse_values() -> None:
    draft = FinalBatchWorksheet(2, "Shift Draft")
    final = FinalBatchWorksheet(3, "Shift Final Schedule")
    sheet = FinalValueSheet(
        draft,
        final,
        final_grid=[
            [],
            ["", "", "Alice", "", "Bob", "Alice"],
            ["", "", "Carol"],
        ],
    )
    metadata = make_metadata(draft, final)
    manager = make_manager(sheet, metadata)

    result = await manager.read_final_schedule_role_source(
        metadata,
        final_schedule_range=None,
        recruitment_ranges=RecruitmentTimeRanges.from_json([{"start": 4, "end": 6}]),
        saved_anchor="B2",
    )

    assert result.selected_range.a1 == "C2:G3"
    assert result.labels == ("Alice", "Bob", "Carol")
    assert result.projected_values == (
        (2, 3, "Alice"),
        (2, 5, "Bob"),
        (2, 6, "Alice"),
        (3, 3, "Carol"),
    )
    assert sheet.batch_reads == [[final.id]]
    assert sheet.batch_updates == []


@pytest.mark.asyncio
async def test_final_role_source_explicit_range_bypasses_derived_inputs() -> None:
    draft = FinalBatchWorksheet(2, "Shift Draft")
    final = FinalBatchWorksheet(3, "Shift Final Schedule")
    sheet = FinalValueSheet(
        draft,
        final,
        final_grid=[["Alice", ""], ["", "Bob"]],
    )
    metadata = make_metadata(draft, final)
    manager = make_manager(sheet, metadata)

    result = await manager.read_final_schedule_role_source(
        metadata,
        final_schedule_range=parse_a1_range("A1:B2"),
        recruitment_ranges=None,
        saved_anchor="not-a-cell",
    )

    assert result.selected_range.a1 == "A1:B2"
    assert result.labels == ("Alice", "Bob")
    assert result.projected_values == ((1, 1, "Alice"), (2, 2, "Bob"))


@pytest.mark.asyncio
async def test_final_role_source_sparse_large_range() -> None:
    draft = FinalBatchWorksheet(2, "Shift Draft")
    final = FinalBatchWorksheet(3, "Shift Final Schedule")
    sheet = FinalValueSheet(draft, final, final_grid=[["Alice", "", "Bob"]])
    metadata = make_metadata(draft, final)
    manager = make_manager(sheet, metadata)

    result = await manager.read_final_schedule_role_source(
        metadata,
        final_schedule_range=parse_a1_range("A1:Z100"),
        recruitment_ranges=None,
        saved_anchor="not-a-cell",
    )

    assert result.projected_values == ((1, 1, "Alice"), (1, 3, "Bob"))


@pytest.mark.asyncio
async def test_final_role_source_rejects_missing_or_non_text() -> None:
    draft = FinalBatchWorksheet(2, "Shift Draft")
    final = FinalBatchWorksheet(3, "Shift Final Schedule")
    sheet = FinalValueSheet(draft, final, final_grid=[[123]])
    missing_metadata = make_metadata(draft, None)
    manager = make_manager(sheet, missing_metadata)

    with pytest.raises(StorageError) as missing:
        await manager.read_final_schedule_role_source(
            missing_metadata,
            final_schedule_range=parse_a1_range("A1:A1"),
            recruitment_ranges=None,
            saved_anchor="not-a-cell",
        )
    assert missing.value.kind is StorageErrorKind.GOOGLE_SHEETS_MISSING_WORKSHEET

    metadata = make_metadata(draft, final)
    manager = make_manager(sheet, metadata)
    with pytest.raises(WorksheetContractError) as invalid:
        await manager.read_final_schedule_role_source(
            metadata,
            final_schedule_range=parse_a1_range("A1:A1"),
            recruitment_ranges=None,
            saved_anchor="not-a-cell",
        )
    assert invalid.value.log_hint == "final_schedule_role_value_not_text"


@pytest.mark.asyncio
async def test_update_from_draft_formats_roles_and_grows_for_date() -> None:
    draft = FinalBatchWorksheet(2, "Shift Draft")
    final = FinalBatchWorksheet(3, "Shift Final Schedule", row_count=1, col_count=1)
    sheet = FinalValueSheet(draft, final, draft_grid=draft_grid_for_event())
    metadata = make_metadata(draft, final)
    manager = make_manager(sheet, metadata)

    request = make_event_request()
    result = await manager.update_schedule_from_draft(metadata, request=request)

    typed_call = final.typed_calls[0]
    assert typed_call["data"] == [
        {"range": "B2:G4", "values": result.schedule.values},
        {"range": "A1", "values": [["12月21日 月曜日 Monday, December 21"]]},
    ]
    assert typed_call["formula_ranges"] == set()
    assert typed_call["min_rows"] == 4
    assert typed_call["min_cols"] == 7
    background_updates = typed_call["background_updates"]
    format_updates = typed_call["format_updates"]
    assert all(range_name[0] in "CDEFG" for range_name, _color in background_updates)
    assert all(range_name[0] in "CDEFG" for range_name, *_rest in format_updates)
    assert any(
        range_name == "C3:G3" and color == "#CCCCCC"
        for range_name, color in background_updates
    )


@pytest.mark.asyncio
async def test_update_from_draft_validation_failure_does_not_write_or_save() -> None:
    draft = FinalBatchWorksheet(2, "Shift Draft")
    final = FinalBatchWorksheet(3, "Shift Final Schedule")
    sheet = FinalValueSheet(
        draft,
        final,
        draft_grid=[list(DraftWorksheetContent.COLUMNS)],
    )
    metadata = make_metadata(draft, final)
    config = SimpleNamespace(final_schedule_anchor_cell="B2", save=AsyncMock())
    manager = make_manager(sheet, metadata, config=config)

    with pytest.raises(FinalScheduleValidationError):
        await manager.update_schedule_from_draft(
            metadata,
            request=make_event_request(supplied_anchor="C3"),
        )

    assert sheet.batch_reads == [[draft.id]]
    assert sheet.batch_updates == []
    config.save.assert_not_awaited()


@pytest.mark.asyncio
async def test_update_from_draft_repairs_and_requires_reconfirmation() -> None:
    draft = FinalBatchWorksheet(2, "Shift Draft")
    final = FinalBatchWorksheet(3, "Shift Final Schedule")
    sheet = FinalValueSheet(draft, final)
    metadata = make_metadata(draft, final)
    manager = make_manager(sheet, metadata)
    manager._ensure_current_worksheets = AsyncMock(  # type: ignore[method-assign]  # noqa: SLF001
        return_value=(metadata, True)
    )

    with pytest.raises(FinalScheduleReconfirmationRequired):
        await manager.update_schedule_from_draft(metadata, request=make_request())

    assert sheet.batch_reads == []
    assert sheet.batch_updates == []


@pytest.mark.asyncio
async def test_update_from_draft_anchor_saves_after_sheet_batch() -> None:
    draft = FinalBatchWorksheet(2, "Shift Draft")
    final = FinalBatchWorksheet(3, "Shift Final Schedule")
    sheet = FinalValueSheet(draft, final, draft_grid=draft_grid_for_event())
    metadata = make_metadata(draft, final)
    config = SimpleNamespace(final_schedule_anchor_cell="B2", save=AsyncMock())
    manager = make_manager(sheet, metadata, config=config)

    await manager.update_schedule_from_draft(
        metadata,
        request=make_event_request(
            supplied_anchor="C3",
            event_day_anchor=None,
        ),
    )

    config.save.assert_awaited_once_with(
        update_fields=["final_schedule_anchor_cell", "updated_at"]
    )
    assert config.final_schedule_anchor_cell == "C3"


@pytest.mark.asyncio
async def test_update_from_draft_sheet_failure_does_not_save_anchor() -> None:
    draft = FinalBatchWorksheet(2, "Shift Draft")
    final = FinalBatchWorksheet(3, "Shift Final Schedule")
    sheet = FinalValueSheet(
        draft,
        final,
        draft_grid=draft_grid_for_event(),
        write_error=GoogleSheetsError(
            GoogleSheetsErrorKind.TRANSIENT,
            "temporary",
        ),
    )
    metadata = make_metadata(draft, final)
    config = SimpleNamespace(final_schedule_anchor_cell="B2", save=AsyncMock())
    manager = make_manager(sheet, metadata, config=config)

    with pytest.raises(GoogleSheetsError):
        await manager.update_schedule_from_draft(
            metadata,
            request=make_event_request(
                supplied_anchor="C3",
                event_day_anchor=None,
            ),
        )

    config.save.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("supplied_anchor", [None, "B2"])
async def test_update_schedule_from_draft_omitted_or_unchanged_anchor_skips_db_save(
    supplied_anchor: str | None,
) -> None:
    draft = FinalBatchWorksheet(2, "Shift Draft")
    final = FinalBatchWorksheet(3, "Shift Final Schedule")
    sheet = FinalValueSheet(draft, final)
    metadata = make_metadata(draft, final)
    config = SimpleNamespace(final_schedule_anchor_cell="B2", save=AsyncMock())
    manager = make_manager(sheet, metadata, config=config)

    await manager.update_schedule_from_draft(
        metadata,
        request=make_request_with_anchor(supplied_anchor),
    )

    config.save.assert_not_awaited()
    assert config.final_schedule_anchor_cell == "B2"


@pytest.mark.asyncio
async def test_update_from_draft_anchor_save_failure_is_partial_success() -> None:
    draft = FinalBatchWorksheet(2, "Shift Draft")
    final = FinalBatchWorksheet(3, "Shift Final Schedule")
    sheet = FinalValueSheet(draft, final)
    metadata = make_metadata(draft, final)
    config = SimpleNamespace(
        final_schedule_anchor_cell="B2",
        save=AsyncMock(
            side_effect=GoogleSheetsError(
                GoogleSheetsErrorKind.TRANSIENT,
                "temporary",
            )
        ),
    )
    manager = make_manager(sheet, metadata, config=config)

    with pytest.raises(StorageError) as caught:
        await manager.update_schedule_from_draft(
            metadata,
            request=make_request_with_anchor("C3"),
        )

    assert caught.value.kind is StorageErrorKind.PARTIAL_SUCCESS
    assert caught.value.log_hint == "final_schedule_written_anchor_not_persisted"
    assert len(sheet.batch_updates) == 1


@pytest.mark.asyncio
async def test_update_from_draft_missing_worksheet_stops_before_read() -> None:
    draft = FinalBatchWorksheet(2, "Shift Draft")
    final = FinalBatchWorksheet(3, "Shift Final Schedule")
    sheet = FinalValueSheet(draft, final)
    metadata = ShiftRegisterGoogleSheetsMetadata(
        "https://docs.google.com/spreadsheets/d/final/edit",
        [
            EntryWorksheetMetadata(1, "Shift Entry", None),
            DraftWorksheetMetadata(draft.id, draft.title, None),
            FinalScheduleWorksheetMetadata(final.id, final.title, final),
        ],
    )
    manager = make_manager(sheet, metadata)

    with pytest.raises(StorageError) as caught:
        await manager.update_schedule_from_draft(metadata, request=make_request())

    assert caught.value.kind is StorageErrorKind.GOOGLE_SHEETS_MISSING_WORKSHEET
    assert sheet.batch_reads == []
    assert sheet.batch_updates == []
