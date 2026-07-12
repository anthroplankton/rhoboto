from __future__ import annotations

import pandas as pd
import pytest
from gspread.exceptions import WorksheetNotFound

from utils.google_sheets import BORDER_NAMES, AsyncioGspreadWorksheet, GoogleSheet


class RawWorksheet:
    def __init__(  # noqa: PLR0913
        self,
        *,
        worksheet_id: int = 1,
        title: str = "Worksheet",
        values: list[list[object]] | None = None,
        batch_values: list[list[list[object]]] | None = None,
        metadata: dict[str, object] | None = None,
        row_count: int = 100,
        col_count: int = 20,
    ) -> None:
        self.id = worksheet_id
        self.title = title
        self.values = values or []
        self.batch_values = batch_values or []
        self.row_count = row_count
        self.col_count = col_count
        self.get_calls: list[dict[str, object]] = []
        self.update_calls: list[dict[str, object]] = []
        self.batch_get_calls: list[dict[str, object]] = []
        self.batch_update_calls: list[dict[str, object]] = []
        self.resize_calls: list[dict[str, int]] = []
        self.delete_calls: list[tuple[int, int | None]] = []
        self.spreadsheet_batch_update_calls: list[dict[str, object]] = []
        self.agcm = RawClientManager()
        self.ws = RawWorksheetResource(self.spreadsheet_batch_update_calls, metadata)
        self.extra_attribute = "delegated"

    async def get(self, **kwargs: object) -> list[list[object]]:
        self.get_calls.append(kwargs)
        return self.values

    async def update(self, values: list[list[object]], **kwargs: object) -> None:
        self.update_calls.append({"values": values, **kwargs})

    async def batch_get(
        self, ranges: list[str], **kwargs: object
    ) -> list[list[list[object]]]:
        self.batch_get_calls.append({"ranges": ranges, **kwargs})
        return self.batch_values

    async def batch_update(
        self, data: list[dict[str, object]], **kwargs: object
    ) -> None:
        self.batch_update_calls.append({"data": data, **kwargs})

    async def resize(self, *, rows: int, cols: int) -> None:
        self.resize_calls.append({"rows": rows, "cols": cols})
        self.row_count = rows
        self.col_count = cols

    async def delete_rows(self, index: int, end_index: int | None = None) -> None:
        self.delete_calls.append((index, end_index))


class RawClientManager:
    async def _call(
        self,
        method: object,
        *args: object,
        **kwargs: object,
    ) -> object:
        assert callable(method)
        return method(*args, **kwargs)


class RawWorksheetResource:
    spreadsheet_id = "spreadsheet-id"

    def __init__(
        self,
        calls: list[dict[str, object]],
        metadata: dict[str, object] | None = None,
    ) -> None:
        self.client = RawSpreadsheetClient(calls, metadata)


class RawSpreadsheetClient:
    def __init__(
        self,
        calls: list[dict[str, object]],
        metadata: dict[str, object] | None,
    ) -> None:
        self.calls = calls
        self.metadata = metadata or {"sheets": []}
        self.metadata_calls: list[dict[str, object]] = []

    def batch_update(self, spreadsheet_id: str, body: dict[str, object]) -> None:
        assert spreadsheet_id == "spreadsheet-id"
        self.calls.append(body)

    def fetch_sheet_metadata(
        self,
        spreadsheet_id: str,
        params: dict[str, object],
    ) -> dict[str, object]:
        assert spreadsheet_id == "spreadsheet-id"
        self.metadata_calls.append(params)
        return self.metadata


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
async def test_adapter_batch_reads_formulas() -> None:
    batch_values = [[["count"]], [["username"], ["alice"]]]
    raw = RawWorksheet(batch_values=batch_values)
    adapter = AsyncioGspreadWorksheet(raw)

    values = await adapter.batch_get_values(["1:2", "A3:C"])

    assert values == batch_values
    assert raw.batch_get_calls == [
        {
            "ranges": ["1:2", "A3:C"],
            "value_render_option": "FORMULA",
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
async def test_adapter_batch_updates_user_entered_ranges() -> None:
    raw = RawWorksheet()
    adapter = AsyncioGspreadWorksheet(raw)
    data = [{"range": "A3:B3", "values": [["alice", "Alice"]]}]

    await adapter.batch_update_values(data)

    assert raw.batch_update_calls == [{"data": data, "raw": False}]


@pytest.mark.asyncio
async def test_adapter_batch_updates_mixed_cell_types_atomically() -> None:
    raw = RawWorksheet()
    adapter = AsyncioGspreadWorksheet(raw)
    data = [
        {"range": "F1", "values": [["=COUNTIF(F$3:F, 1)"]]},
        {"range": "G1", "values": [["0-1"]]},
        {"range": "F3:G3", "values": [[1, False]]},
    ]

    await adapter.batch_update_typed_values(data, formula_ranges={"F1"})

    assert len(raw.spreadsheet_batch_update_calls) == 1
    requests = raw.spreadsheet_batch_update_calls[0]["requests"]
    assert isinstance(requests, list)
    cells = [request["updateCells"] for request in requests]
    assert cells[0]["range"] == {
        "sheetId": 1,
        "startRowIndex": 0,
        "endRowIndex": 1,
        "startColumnIndex": 5,
        "endColumnIndex": 6,
    }
    assert cells[0]["rows"][0]["values"][0]["userEnteredValue"] == {
        "formulaValue": "=COUNTIF(F$3:F, 1)"
    }
    assert cells[1]["rows"][0]["values"][0]["userEnteredValue"] == {
        "stringValue": "0-1"
    }
    assert [value["userEnteredValue"] for value in cells[2]["rows"][0]["values"]] == [
        {"numberValue": 1},
        {"boolValue": False},
    ]


@pytest.mark.asyncio
async def test_adapter_reads_worksheet_conditional_format_rules() -> None:
    own_rules = [{"booleanRule": {"condition": {"type": "CUSTOM_FORMULA"}}}]
    raw = RawWorksheet(
        worksheet_id=42,
        metadata={
            "sheets": [
                {"properties": {"sheetId": 1}, "conditionalFormats": [{"other": 1}]},
                {"properties": {"sheetId": 42}, "conditionalFormats": own_rules},
            ]
        },
    )
    adapter = AsyncioGspreadWorksheet(raw)

    assert await adapter.get_conditional_format_rules() == own_rules
    assert raw.ws.client.metadata_calls == [
        {"fields": "sheets(properties(sheetId),conditionalFormats)"}
    ]


@pytest.mark.asyncio
async def test_adapter_batches_entry_presentation_requests_atomically() -> None:
    raw = RawWorksheet()
    adapter = AsyncioGspreadWorksheet(raw)
    desired_rule = {
        "ranges": [{"sheetId": 1, "startRowIndex": 2}],
        "booleanRule": {
            "condition": {
                "type": "CUSTOM_FORMULA",
                "values": [{"userEnteredValue": "=TRUE"}],
            },
            "format": {"backgroundColorStyle": {"rgbColor": {"red": 1.0}}},
        },
    }

    await adapter.batch_update_typed_values(
        [{"range": "A1", "values": [["count"]]}],
        formula_ranges=set(),
        format_updates=[
            (
                "A2:AJ2",
                {
                    "backgroundColorStyle": {
                        "rgbColor": {
                            "red": 60 / 255,
                            "green": 120 / 255,
                            "blue": 216 / 255,
                        }
                    },
                    "textFormat": {
                        "bold": True,
                        "foregroundColorStyle": {
                            "rgbColor": {"red": 1.0, "green": 1.0, "blue": 1.0}
                        },
                    },
                },
                "userEnteredFormat(backgroundColorStyle,textFormat)",
            )
        ],
        column_width_updates=[("A:B", 100), ("F:AI", 40)],
        hidden_column_updates=[("F:AI", False), ("F:I", True)],
        conditional_format_rule_deletes=[3, 1],
        conditional_format_rule_adds=[desired_rule],
        frozen_column_count=5,
    )

    assert len(raw.spreadsheet_batch_update_calls) == 1
    requests = raw.spreadsheet_batch_update_calls[0]["requests"]
    assert [next(iter(request)) for request in requests] == [
        "updateCells",
        "repeatCell",
        "updateDimensionProperties",
        "updateDimensionProperties",
        "updateDimensionProperties",
        "updateDimensionProperties",
        "deleteConditionalFormatRule",
        "deleteConditionalFormatRule",
        "addConditionalFormatRule",
        "updateSheetProperties",
    ]
    assert requests[2]["updateDimensionProperties"] == {
        "range": {
            "sheetId": 1,
            "dimension": "COLUMNS",
            "startIndex": 0,
            "endIndex": 2,
        },
        "properties": {"pixelSize": 100},
        "fields": "pixelSize",
    }
    assert requests[4]["updateDimensionProperties"]["properties"] == {
        "hiddenByUser": False
    }
    assert [
        request["deleteConditionalFormatRule"]["index"] for request in requests[6:8]
    ] == [3, 1]
    assert requests[8] == {
        "addConditionalFormatRule": {"rule": desired_rule, "index": 0}
    }


@pytest.mark.asyncio
async def test_adapter_batch_updates_values_and_draft_formats_atomically() -> None:
    raw = RawWorksheet()
    adapter = AsyncioGspreadWorksheet(raw)

    await adapter.batch_update_typed_values(
        [{"range": "A1", "values": [["JST"]]}],
        formula_ranges=set(),
        background_updates=[
            ("A1:G5", "#FFFFFF"),
            ("A3:G3", "#CCCCCC"),
        ],
        border_updates=[
            ("A1:G8", None, "NONE", BORDER_NAMES),
            (
                "A1:G5",
                "#000000",
                "SOLID",
                ("top", "bottom", "left", "right"),
            ),
            ("K5", "#FF0000", "SOLID_MEDIUM", BORDER_NAMES[:4]),
        ],
        frozen_column_count=1,
    )

    assert len(raw.spreadsheet_batch_update_calls) == 1
    requests = raw.spreadsheet_batch_update_calls[0]["requests"]
    assert [next(iter(request)) for request in requests] == [
        "updateCells",
        "repeatCell",
        "repeatCell",
        "updateBorders",
        "updateBorders",
        "updateBorders",
        "updateSheetProperties",
    ]
    white, gray = (request["repeatCell"] for request in requests[1:3])
    assert white["range"] == {
        "sheetId": 1,
        "startRowIndex": 0,
        "endRowIndex": 5,
        "startColumnIndex": 0,
        "endColumnIndex": 7,
    }
    assert white["cell"]["userEnteredFormat"]["backgroundColorStyle"] == {
        "rgbColor": {"red": 1.0, "green": 1.0, "blue": 1.0}
    }
    assert gray["range"]["startRowIndex"] == 2
    assert gray["range"]["endRowIndex"] == 3
    assert gray["cell"]["userEnteredFormat"]["backgroundColorStyle"] == {
        "rgbColor": {"red": 0.8, "green": 0.8, "blue": 0.8}
    }
    clear_borders, outer_borders, input_borders = (
        request["updateBorders"] for request in requests[3:6]
    )
    assert all(clear_borders[name] == {"style": "NONE"} for name in BORDER_NAMES)
    expected_border = {
        "style": "SOLID",
        "colorStyle": {"rgbColor": {"red": 0.0, "green": 0.0, "blue": 0.0}},
    }
    assert set(outer_borders) == {"range", "top", "bottom", "left", "right"}
    assert all(
        outer_borders[name] == expected_border
        for name in ("top", "bottom", "left", "right")
    )
    assert set(input_borders) == {"range", *BORDER_NAMES[:4]}
    assert all(
        input_borders[name]
        == {
            "style": "SOLID_MEDIUM",
            "colorStyle": {"rgbColor": {"red": 1.0, "green": 0.0, "blue": 0.0}},
        }
        for name in BORDER_NAMES[:4]
    )
    assert requests[-1] == {
        "updateSheetProperties": {
            "properties": {
                "sheetId": 1,
                "gridProperties": {"frozenColumnCount": 1},
            },
            "fields": "gridProperties.frozenColumnCount",
        }
    }


@pytest.mark.asyncio
async def test_adapter_ensures_only_missing_grid_capacity() -> None:
    raw = RawWorksheet(row_count=2, col_count=20)
    adapter = AsyncioGspreadWorksheet(raw)

    await adapter.ensure_size(min_rows=3, min_cols=36)

    assert raw.resize_calls == [{"rows": 3, "cols": 36}]


@pytest.mark.asyncio
async def test_adapter_does_not_resize_sufficient_grid() -> None:
    raw = RawWorksheet(row_count=100, col_count=40)
    adapter = AsyncioGspreadWorksheet(raw)

    await adapter.ensure_size(min_rows=3, min_cols=36)

    assert raw.resize_calls == []


@pytest.mark.asyncio
async def test_adapter_deletes_one_physical_row() -> None:
    raw = RawWorksheet()
    adapter = AsyncioGspreadWorksheet(raw)

    await adapter.delete_row(4)

    assert raw.delete_calls == [(4, None)]


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
