from __future__ import annotations

import copy

import numpy as np
import pandas as pd
import pytest
from gspread.exceptions import WorksheetNotFound

from utils import google_sheets as google_sheets_module
from utils.google_sheets import BORDER_NAMES, AsyncioGspreadWorksheet, GoogleSheet
from utils.google_sheets_errors import GoogleSheetsError, GoogleSheetsErrorKind


class RawWorksheet:
    def __init__(  # noqa: PLR0913
        self,
        *,
        worksheet_id: int = 1,
        title: str = "Worksheet",
        batch_values: list[list[list[object]]] | None = None,
        metadata: dict[str, object] | None = None,
        row_count: int = 100,
        col_count: int = 20,
    ) -> None:
        self.id = worksheet_id
        self.title = title
        self.batch_values = batch_values or []
        self.row_count = row_count
        self.col_count = col_count
        self.batch_get_calls: list[dict[str, object]] = []
        self.delete_calls: list[tuple[int, int | None]] = []
        self.spreadsheet_batch_update_calls: list[dict[str, object]] = []
        self.agcm = RawClientManager()
        self.ws = RawWorksheetResource(self.spreadsheet_batch_update_calls, metadata)
        self.extra_attribute = "delegated"

    async def batch_get(
        self, ranges: list[str], **kwargs: object
    ) -> list[list[list[object]]]:
        self.batch_get_calls.append({"ranges": ranges, **kwargs})
        return self.batch_values

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
    def __init__(
        self,
        worksheets: list[RawWorksheet] | None = None,
        *,
        grids: dict[int, list[list[object]]] | None = None,
    ) -> None:
        self._worksheets = worksheets or []
        self.added_worksheets: list[dict[str, object]] = []
        self.batch_update_calls: list[dict[str, object]] = []
        self.grids = copy.deepcopy(grids or {})

    async def batch_update(self, body: dict[str, object]) -> None:
        self.batch_update_calls.append(copy.deepcopy(body))
        requests = body.get("requests")
        if not isinstance(requests, list):
            msg = "Invalid batch requests."
            raise TypeError(msg)
        original = self.grids
        self.grids = copy.deepcopy(original)
        try:
            for request in requests:
                self._apply_request(request)
        except (TypeError, ValueError):
            self.grids = original
            raise

    def _apply_request(self, request: object) -> None:
        if not isinstance(request, dict):
            msg = "Invalid batch request."
            raise TypeError(msg)
        if isinstance(payload := request.get("appendDimension"), dict):
            self._apply_append(payload)
            return
        if isinstance(payload := request.get("insertDimension"), dict):
            self._apply_range_dimension(payload, insert=True)
            return
        if isinstance(payload := request.get("deleteDimension"), dict):
            self._apply_range_dimension(payload, insert=False)
            return
        if isinstance(payload := request.get("updateCells"), dict):
            self._apply_update(payload)
            return
        msg = "Unsupported batch request."
        raise ValueError(msg)

    def _grid(self, sheet_id: object) -> list[list[object]]:
        if not isinstance(sheet_id, int) or sheet_id not in self.grids:
            msg = "Unknown sheet ID."
            raise ValueError(msg)
        return self.grids[sheet_id]

    def _apply_append(self, payload: dict[str, object]) -> None:
        grid = self._grid(payload.get("sheetId"))
        dimension = payload.get("dimension")
        length = payload.get("length")
        if not isinstance(length, int) or length < 1:
            msg = "Invalid append length."
            raise ValueError(msg)
        if dimension == "ROWS":
            width = len(grid[0]) if grid else 0
            grid.extend([[""] * width for _ in range(length)])
        elif dimension == "COLUMNS":
            for row in grid:
                row.extend([""] * length)
        else:
            msg = "Invalid append dimension."
            raise ValueError(msg)

    def _apply_range_dimension(
        self,
        payload: dict[str, object],
        *,
        insert: bool,
    ) -> None:
        dimension_range = payload.get("range")
        if not isinstance(dimension_range, dict):
            msg = "Invalid dimension range."
            raise TypeError(msg)
        grid = self._grid(dimension_range.get("sheetId"))
        dimension = dimension_range.get("dimension")
        start = dimension_range.get("startIndex")
        end = dimension_range.get("endIndex")
        if not isinstance(start, int) or not isinstance(end, int) or start >= end:
            msg = "Invalid dimension indices."
            raise ValueError(msg)
        size = len(grid) if dimension == "ROWS" else len(grid[0]) if grid else 0
        if start < 0 or start > size or (not insert and end > size):
            msg = "Dimension range is outside the grid."
            raise ValueError(msg)
        count = end - start
        if dimension == "ROWS":
            width = len(grid[0]) if grid else 0
            if insert:
                grid[start:start] = [[""] * width for _ in range(count)]
            else:
                del grid[start:end]
        elif dimension == "COLUMNS":
            for row in grid:
                if insert:
                    row[start:start] = [""] * count
                else:
                    del row[start:end]
        else:
            msg = "Invalid ranged dimension."
            raise ValueError(msg)

    def _apply_update(self, payload: dict[str, object]) -> None:
        grid_range = payload.get("range")
        rows = payload.get("rows")
        if not isinstance(grid_range, dict) or not isinstance(rows, list):
            msg = "Invalid cell update."
            raise TypeError(msg)
        grid = self._grid(grid_range.get("sheetId"))
        start_row = grid_range.get("startRowIndex")
        end_row = grid_range.get("endRowIndex")
        start_column = grid_range.get("startColumnIndex")
        end_column = grid_range.get("endColumnIndex")
        indices = (start_row, end_row, start_column, end_column)
        if not all(isinstance(index, int) for index in indices):
            msg = "Invalid cell indices."
            raise ValueError(msg)
        assert isinstance(start_row, int)
        assert isinstance(end_row, int)
        assert isinstance(start_column, int)
        assert isinstance(end_column, int)
        width = len(grid[0]) if grid else 0
        if (
            start_row < 0
            or start_column < 0
            or end_row > len(grid)
            or end_column > width
            or len(rows) != end_row - start_row
        ):
            msg = "Cell update is outside the grid."
            raise ValueError(msg)
        for row_index, row_data in enumerate(rows, start=start_row):
            if not isinstance(row_data, dict):
                msg = "Invalid cell row."
                raise TypeError(msg)
            cells = row_data.get("values")
            if not isinstance(cells, list) or len(cells) != end_column - start_column:
                msg = "Invalid cell row width."
                raise ValueError(msg)
            grid[row_index][start_column:end_column] = [
                self._cell_value(cell) for cell in cells
            ]

    @staticmethod
    def _cell_value(cell: object) -> object:
        if not isinstance(cell, dict):
            msg = "Invalid cell."
            raise TypeError(msg)
        extended = cell.get("userEnteredValue")
        if not isinstance(extended, dict):
            msg = "Invalid extended value."
            raise TypeError(msg)
        if not extended:
            return None
        for key in ("numberValue", "boolValue", "stringValue", "formulaValue"):
            if key in extended:
                return extended[key]
        msg = "Invalid extended value type."
        raise ValueError(msg)

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
async def test_batch_update_grid_converts_domain_coordinates_once() -> None:
    spreadsheet = RawSpreadsheet(grids={42: [["", "", "", ""] for _ in range(2)]})
    sheet = FakeGoogleSheet(spreadsheet)
    mutation = google_sheets_module.GridValueUpdate.from_values(
        worksheet_id=42,
        start_row=2,
        start_column=3,
        values=[["Alice", 7]],
    )

    await sheet.batch_update_grid([mutation])

    assert spreadsheet.batch_update_calls == [
        {
            "requests": [
                {
                    "updateCells": {
                        "range": {
                            "sheetId": 42,
                            "startRowIndex": 1,
                            "endRowIndex": 2,
                            "startColumnIndex": 2,
                            "endColumnIndex": 4,
                        },
                        "rows": [
                            {
                                "values": [
                                    {"userEnteredValue": {"stringValue": "Alice"}},
                                    {"userEnteredValue": {"numberValue": 7}},
                                ]
                            }
                        ],
                        "fields": "userEnteredValue",
                    }
                }
            ]
        }
    ]


@pytest.mark.asyncio
async def test_batch_update_grid_preserves_cross_sheet_mutation_order() -> None:
    spreadsheet = RawSpreadsheet(
        grids={
            10: [
                ["old_h1", "old_h2", "old_h3"],
                ["a", "b", "c"],
                ["d", "e", "f"],
                ["g", "h", "i"],
            ],
            20: [["x", "x2", ""], ["y", "y2", ""], ["z", "z2", ""]],
        }
    )
    sheet = FakeGoogleSheet(spreadsheet)
    mutations = [
        google_sheets_module.DimensionMutation.append_rows(10, 2),
        google_sheets_module.DimensionMutation.append_columns(10, 3),
        google_sheets_module.DimensionMutation.insert_columns(
            10,
            start_column=2,
            count=2,
        ),
        google_sheets_module.DimensionMutation.delete_columns(
            10,
            start_column=4,
        ),
        google_sheets_module.GridValueUpdate.from_values(
            worksheet_id=10,
            start_row=1,
            start_column=1,
            values=[["username", "score"]],
        ),
        google_sheets_module.GridValueUpdate.from_values(
            worksheet_id=20,
            start_row=3,
            start_column=2,
            values=[["alice", 7]],
        ),
        google_sheets_module.DimensionMutation.delete_rows(10, start_row=6),
        google_sheets_module.DimensionMutation.delete_rows(10, start_row=4),
    ]

    await sheet.batch_update_grid(mutations)

    requests = spreadsheet.batch_update_calls[0]["requests"]
    assert [next(iter(request)) for request in requests] == [
        "appendDimension",
        "appendDimension",
        "insertDimension",
        "deleteDimension",
        "updateCells",
        "updateCells",
        "deleteDimension",
        "deleteDimension",
    ]
    assert requests[:4] == [
        {
            "appendDimension": {
                "sheetId": 10,
                "dimension": "ROWS",
                "length": 2,
            }
        },
        {
            "appendDimension": {
                "sheetId": 10,
                "dimension": "COLUMNS",
                "length": 3,
            }
        },
        {
            "insertDimension": {
                "range": {
                    "sheetId": 10,
                    "dimension": "COLUMNS",
                    "startIndex": 1,
                    "endIndex": 3,
                },
                "inheritFromBefore": False,
            }
        },
        {
            "deleteDimension": {
                "range": {
                    "sheetId": 10,
                    "dimension": "COLUMNS",
                    "startIndex": 3,
                    "endIndex": 4,
                }
            }
        },
    ]
    assert [
        request["updateCells"]["range"]["sheetId"] for request in requests[4:6]
    ] == [10, 20]
    assert [request["deleteDimension"]["range"] for request in requests[6:]] == [
        {
            "sheetId": 10,
            "dimension": "ROWS",
            "startIndex": 5,
            "endIndex": 6,
        },
        {
            "sheetId": 10,
            "dimension": "ROWS",
            "startIndex": 3,
            "endIndex": 4,
        },
    ]
    assert spreadsheet.grids == {
        10: [
            ["username", "score", "", "old_h3", "", "", ""],
            ["a", "", "", "c", "", "", ""],
            ["d", "", "", "f", "", "", ""],
            ["", "", "", "", "", "", ""],
        ],
        20: [["x", "x2", ""], ["y", "y2", ""], ["z", "alice", 7]],
    }


@pytest.mark.asyncio
async def test_batch_update_grid_skips_empty_mutation_list() -> None:
    spreadsheet = RawSpreadsheet()
    sheet = FakeGoogleSheet(spreadsheet)

    await sheet.batch_update_grid([])

    assert spreadsheet.batch_update_calls == []


@pytest.mark.asyncio
async def test_batch_update_grid_rolls_back_on_invalid_late_request() -> None:
    spreadsheet = RawSpreadsheet(grids={1: [["original"]]})
    sheet = FakeGoogleSheet(spreadsheet)
    mutations = [
        google_sheets_module.GridValueUpdate.from_values(
            worksheet_id=1,
            start_row=1,
            start_column=1,
            values=[["changed"]],
        ),
        google_sheets_module.DimensionMutation.delete_rows(1, start_row=99),
    ]

    with pytest.raises(GoogleSheetsError):
        await sheet.batch_update_grid(mutations)

    requests = spreadsheet.batch_update_calls[0]["requests"]
    assert [next(iter(request)) for request in requests] == [
        "updateCells",
        "deleteDimension",
    ]
    assert spreadsheet.grids == {1: [["original"]]}


@pytest.mark.asyncio
async def test_batch_update_grid_serializes_native_and_dataframe_scalars() -> None:
    spreadsheet = RawSpreadsheet(grids={1: [[""] * 12]})
    sheet = FakeGoogleSheet(spreadsheet)
    mutation = google_sheets_module.GridValueUpdate.from_values(
        worksheet_id=1,
        start_row=1,
        start_column=1,
        values=[
            [
                1,
                1.25,
                True,
                "plain",
                "=SUM(A2:A3)",
                None,
                np.int64(2),
                np.float32(2.5),
                np.bool_(0),
                np.str_("label"),
                pd.NA,
                pd.NaT,
            ]
        ],
    )

    await sheet.batch_update_grid([mutation])

    request = spreadsheet.batch_update_calls[0]["requests"][0]["updateCells"]
    assert [cell["userEnteredValue"] for cell in request["rows"][0]["values"]] == [
        {"numberValue": 1},
        {"numberValue": 1.25},
        {"boolValue": True},
        {"stringValue": "plain"},
        {"stringValue": "=SUM(A2:A3)"},
        {},
        {"numberValue": 2},
        {"numberValue": 2.5},
        {"boolValue": False},
        {"stringValue": "label"},
        {},
        {},
    ]


@pytest.mark.asyncio
async def test_batch_update_grid_requires_explicit_formula_values() -> None:
    spreadsheet = RawSpreadsheet(grids={1: [["", ""]]})
    sheet = FakeGoogleSheet(spreadsheet)
    mutation = google_sheets_module.GridValueUpdate.from_values(
        worksheet_id=1,
        start_row=1,
        start_column=1,
        values=[
            [
                "=literal text",
                google_sheets_module.GridFormula("=SUM(A2:A3)"),
            ]
        ],
    )

    await sheet.batch_update_grid([mutation])

    request = spreadsheet.batch_update_calls[0]["requests"][0]["updateCells"]
    assert [cell["userEnteredValue"] for cell in request["rows"][0]["values"]] == [
        {"stringValue": "=literal text"},
        {"formulaValue": "=SUM(A2:A3)"},
    ]


@pytest.mark.parametrize("value", [None, 1, np.nan])
def test_grid_formula_rejects_non_string_payloads(value: object) -> None:
    with pytest.raises(TypeError, match="string"):
        google_sheets_module.GridFormula(value)


@pytest.mark.parametrize("value", ["", "SUM(A2:A3)", " formula"])
def test_grid_formula_requires_equals_marker(value: str) -> None:
    with pytest.raises(ValueError, match="begin with"):
        google_sheets_module.GridFormula(value)


@pytest.mark.parametrize(
    ("value", "error"),
    [
        (object(), TypeError),
        (pd.Timestamp("2026-07-13"), TypeError),
        (float("nan"), ValueError),
        (np.float64("nan"), ValueError),
        (np.float32("nan"), ValueError),
        (np.longdouble("nan"), ValueError),
        (float("inf"), ValueError),
        (float("-inf"), ValueError),
        (np.float64("inf"), ValueError),
        (np.longdouble("inf"), ValueError),
    ],
)
def test_grid_value_update_rejects_non_json_scalar_values(
    value: object,
    error: type[Exception],
) -> None:
    with pytest.raises(error, match="grid value"):
        google_sheets_module.GridValueUpdate.from_values(
            worksheet_id=1,
            start_row=1,
            start_column=1,
            values=[[value]],
        )


@pytest.mark.parametrize(
    ("start_row", "start_column", "values"),
    [
        (0, 1, [[1]]),
        (1, 0, [[1]]),
        (1, 1, []),
        (1, 1, [[]]),
        (1, 1, [[1], [2, 3]]),
    ],
)
def test_grid_value_update_rejects_invalid_domain_ranges(
    start_row: int,
    start_column: int,
    values: list[list[object]],
) -> None:
    with pytest.raises(ValueError, match="grid update"):
        google_sheets_module.GridValueUpdate.from_values(
            worksheet_id=1,
            start_row=start_row,
            start_column=start_column,
            values=values,
        )


@pytest.mark.parametrize(
    "factory",
    [
        lambda: google_sheets_module.DimensionMutation.append_rows(1, 0),
        lambda: google_sheets_module.DimensionMutation.append_columns(1, -1),
        lambda: google_sheets_module.DimensionMutation.insert_columns(
            1,
            start_column=0,
        ),
        lambda: google_sheets_module.DimensionMutation.delete_columns(
            1,
            start_column=1,
            count=0,
        ),
        lambda: google_sheets_module.DimensionMutation.delete_rows(
            1,
            start_row=0,
        ),
    ],
)
def test_dimension_mutation_rejects_invalid_domain_ranges(factory: object) -> None:
    assert callable(factory)
    with pytest.raises(ValueError, match="dimension mutation"):
        factory()


@pytest.mark.parametrize(
    "factory",
    [
        lambda: google_sheets_module.GridValueUpdate.from_values(
            worksheet_id=1,
            start_row=True,
            start_column=1,
            values=[[1]],
        ),
        lambda: google_sheets_module.GridValueUpdate.from_values(
            worksheet_id=1,
            start_row=1,
            start_column=1.5,
            values=[[1]],
        ),
        lambda: google_sheets_module.DimensionMutation.append_rows(1, count=True),
        lambda: google_sheets_module.DimensionMutation.append_columns(
            1,
            np.float64(2.5),
        ),
        lambda: google_sheets_module.DimensionMutation.insert_columns(
            1,
            start_column=False,
        ),
        lambda: google_sheets_module.DimensionMutation.delete_columns(
            1,
            start_column=1,
            count=1.5,
        ),
        lambda: google_sheets_module.DimensionMutation.delete_rows(
            1,
            start_row=np.float64(2.5),
        ),
    ],
)
def test_grid_mutation_factories_reject_non_integer_coordinates_and_counts(
    factory: object,
) -> None:
    assert callable(factory)
    with pytest.raises(ValueError, match="positive integer"):
        factory()


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
async def test_adapter_typed_values_normalize_scalars_and_clear_blanks() -> None:
    raw = RawWorksheet()
    adapter = AsyncioGspreadWorksheet(raw)

    await adapter.batch_update_typed_values(
        [
            {
                "range": "A1:H1",
                "values": [
                    [
                        "",
                        None,
                        pd.NA,
                        pd.NaT,
                        np.int64(2),
                        np.bool_(0),
                        np.str_("label"),
                        "=literal",
                    ]
                ],
            }
        ],
        formula_ranges=set(),
    )

    request = raw.spreadsheet_batch_update_calls[0]["requests"][0]["updateCells"]
    assert [value["userEnteredValue"] for value in request["rows"][0]["values"]] == [
        {},
        {},
        {},
        {},
        {"numberValue": 2},
        {"boolValue": False},
        {"stringValue": "label"},
        {"stringValue": "=literal"},
    ]


@pytest.mark.asyncio
async def test_adapter_rejects_plain_text_in_formula_range_before_request() -> None:
    raw = RawWorksheet()
    adapter = AsyncioGspreadWorksheet(raw)

    with pytest.raises(ValueError, match="formula range"):
        await adapter.batch_update_typed_values(
            [{"range": "A1", "values": [["not a formula"]]}],
            formula_ranges={"A1"},
        )

    assert raw.spreadsheet_batch_update_calls == []


@pytest.mark.asyncio
async def test_adapter_skips_empty_typed_request() -> None:
    raw = RawWorksheet()
    adapter = AsyncioGspreadWorksheet(raw)

    await adapter.batch_update_typed_values([], formula_ranges=set())

    assert raw.spreadsheet_batch_update_calls == []


@pytest.mark.asyncio
async def test_adapter_batches_grid_growth_before_typed_values() -> None:
    raw = RawWorksheet(row_count=2, col_count=20)
    adapter = AsyncioGspreadWorksheet(raw)

    await adapter.batch_update_typed_values(
        [{"range": "A3:AJ3", "values": [[""] * 36]}],
        formula_ranges=set(),
        min_rows=3,
        min_cols=36,
    )

    requests = raw.spreadsheet_batch_update_calls[0]["requests"]
    assert [next(iter(request)) for request in requests] == [
        "appendDimension",
        "appendDimension",
        "updateCells",
    ]
    assert requests[0]["appendDimension"] == {
        "sheetId": 1,
        "dimension": "ROWS",
        "length": 1,
    }
    assert requests[1]["appendDimension"] == {
        "sheetId": 1,
        "dimension": "COLUMNS",
        "length": 16,
    }


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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure_title", "expected_created_title"),
    [("First", None), ("Second", "First")],
)
async def test_get_or_create_worksheets_tracks_only_completed_creations(
    failure_title: str,
    expected_created_title: str | None,
) -> None:
    class FailingCreationSpreadsheet(RawSpreadsheet):
        async def add_worksheet(
            self,
            title: str,
            *,
            rows: int,
            cols: int,
        ) -> RawWorksheet:
            if title == failure_title:
                raise GoogleSheetsError(
                    GoogleSheetsErrorKind.TRANSIENT,
                    "private create failure",
                )
            return await super().add_worksheet(title, rows=rows, cols=cols)

    spreadsheet = FailingCreationSpreadsheet()
    sheet = FakeGoogleSheet(spreadsheet)
    status = google_sheets_module.WorksheetCreationStatus()

    with pytest.raises(GoogleSheetsError):
        await sheet.get_or_create_worksheets(
            ["First", "Second"],
            creation_status=status,
        )

    assert status.created is (expected_created_title is not None)
    assert [item["title"] for item in spreadsheet.added_worksheets] == (
        [expected_created_title] if expected_created_title is not None else []
    )
