from __future__ import annotations

import pandas as pd
import pytest
from gspread.exceptions import WorksheetNotFound

from utils.google_sheets import AsyncioGspreadWorksheet, GoogleSheet


class RawWorksheet:
    def __init__(
        self,
        *,
        worksheet_id: int = 1,
        title: str = "Worksheet",
        values: list[list[object]] | None = None,
    ) -> None:
        self.id = worksheet_id
        self.title = title
        self.values = values or []
        self.get_calls: list[dict[str, object]] = []
        self.update_calls: list[dict[str, object]] = []
        self.extra_attribute = "delegated"

    async def get(self, **kwargs: object) -> list[list[object]]:
        self.get_calls.append(kwargs)
        return self.values

    async def update(self, values: list[list[object]], **kwargs: object) -> None:
        self.update_calls.append({"values": values, **kwargs})


class RawSpreadsheet:
    def __init__(self, worksheets: list[RawWorksheet] | None = None) -> None:
        self._worksheets = worksheets or []
        self.added_worksheets: list[dict[str, object]] = []

    async def get_worksheet_by_id(self, worksheet_id: int) -> RawWorksheet | None:
        for worksheet in self._worksheets:
            if worksheet.id == worksheet_id:
                return worksheet
        msg = "worksheet not found"
        raise WorksheetNotFound(msg)

    async def worksheets(self) -> list[RawWorksheet]:
        return list(self._worksheets)

    async def worksheet(self, title: str) -> RawWorksheet:
        for worksheet in self._worksheets:
            if worksheet.title == title:
                return worksheet
        msg = "worksheet not found"
        raise WorksheetNotFound(msg)

    async def add_worksheet(self, title: str, *, rows: int, cols: int) -> RawWorksheet:
        worksheet = RawWorksheet(
            worksheet_id=100 + len(self._worksheets),
            title=title,
        )
        self.added_worksheets.append({"title": title, "rows": rows, "cols": cols})
        self._worksheets.append(worksheet)
        return worksheet


class FakeGoogleSheet(GoogleSheet):
    def __init__(self, spreadsheet: RawSpreadsheet) -> None:
        self.sheet_url = "https://sheet.example"
        self.service_account_path = "service.json"
        self.spreadsheet = spreadsheet

    @property
    async def sheet(self) -> RawSpreadsheet:
        return self.spreadsheet


@pytest.mark.asyncio
async def test_adapter_delegates_worksheet_api_and_pads_rows() -> None:
    raw = RawWorksheet(
        worksheet_id=42,
        title="Existing",
        values=[["username", "score"], ["alice"], ["bob", 10]],
    )
    adapter = AsyncioGspreadWorksheet(raw)

    frame = await adapter.to_frame()

    assert adapter.id == 42
    assert adapter.title == "Existing"
    assert adapter.extra_attribute == "delegated"
    assert raw.get_calls == [{"value_render_option": "FORMULA"}]
    assert frame.equals(
        pd.DataFrame(
            [["alice", ""], ["bob", 10]],
            columns=["username", "score"],
        )
    )


@pytest.mark.asyncio
async def test_adapter_updates_dataframe_through_wrapped_worksheet() -> None:
    raw = RawWorksheet()
    adapter = AsyncioGspreadWorksheet(raw)
    frame = pd.DataFrame({"username": ["alice"], "score": [None]})

    await adapter.update_from_dataframe(frame)

    assert raw.update_calls == [
        {
            "values": [["username", "score"]],
            "range_name": "A1",
            "raw": True,
        },
        {
            "values": [["alice", ""]],
            "range_name": "A2",
            "raw": False,
        },
    ]


@pytest.mark.asyncio
async def test_adapter_updates_empty_dataframe_header_as_raw_text() -> None:
    raw = RawWorksheet()
    adapter = AsyncioGspreadWorksheet(raw)
    frame = pd.DataFrame(columns=["username", "1-2"])

    await adapter.update_from_dataframe(frame)

    assert raw.update_calls == [
        {
            "values": [["username", "1-2"]],
            "range_name": "A1",
            "raw": True,
        }
    ]


@pytest.mark.asyncio
async def test_adapter_updates_dataframe_rows_as_raw_when_requested() -> None:
    raw = RawWorksheet()
    adapter = AsyncioGspreadWorksheet(raw)
    frame = pd.DataFrame({"JST": ["4-5"]})

    await adapter.update_from_dataframe(frame, raw_data=True)

    assert raw.update_calls == [
        {
            "values": [["JST"]],
            "range_name": "A1",
            "raw": True,
        },
        {
            "values": [["4-5"]],
            "range_name": "A2",
            "raw": True,
        },
    ]


@pytest.mark.asyncio
async def test_get_worksheet_wraps_without_mutating_raw_class() -> None:
    raw = RawWorksheet(worksheet_id=7, title="Main")
    raw_class = raw.__class__
    sheet = FakeGoogleSheet(RawSpreadsheet([raw]))

    worksheet = await sheet.get_worksheet(7)

    assert isinstance(worksheet, AsyncioGspreadWorksheet)
    assert worksheet is not raw
    assert worksheet.worksheet is raw
    assert raw.__class__ is raw_class
    assert worksheet.id == 7
    assert worksheet.title == "Main"


@pytest.mark.asyncio
async def test_get_worksheet_returns_none_when_missing() -> None:
    sheet = FakeGoogleSheet(RawSpreadsheet())

    worksheet = await sheet.get_worksheet(999)

    assert worksheet is None


@pytest.mark.asyncio
async def test_get_worksheets_preserves_requested_id_mapping() -> None:
    first = RawWorksheet(worksheet_id=1, title="First")
    third = RawWorksheet(worksheet_id=3, title="Third")
    sheet = FakeGoogleSheet(RawSpreadsheet([first, third]))

    worksheets = await sheet.get_worksheets([1, 2, 3])

    assert list(worksheets) == [1, 2, 3]
    assert worksheets[1] is not None
    assert worksheets[1].worksheet is first
    assert worksheets[2] is None
    assert worksheets[3] is not None
    assert worksheets[3].worksheet is third
    assert first.__class__ is RawWorksheet
    assert third.__class__ is RawWorksheet


@pytest.mark.asyncio
async def test_get_or_create_worksheets_returns_title_keyed_adapters() -> None:
    existing = RawWorksheet(worksheet_id=1, title="Existing")
    spreadsheet = RawSpreadsheet([existing])
    sheet = FakeGoogleSheet(spreadsheet)

    worksheets = await sheet.get_or_create_worksheets(
        ["Existing", "Created"],
        default_rows=12,
        default_cols=8,
    )

    assert list(worksheets) == ["Existing", "Created"]
    assert worksheets["Existing"].worksheet is existing
    assert worksheets["Created"].id == 101
    assert worksheets["Created"].title == "Created"
    assert spreadsheet.added_worksheets == [{"title": "Created", "rows": 12, "cols": 8}]
    assert all(isinstance(ws, AsyncioGspreadWorksheet) for ws in worksheets.values())
