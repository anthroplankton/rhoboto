from __future__ import annotations

import re
from dataclasses import dataclass
from operator import index
from typing import TYPE_CHECKING, NoReturn

import gspread_asyncio
import numpy as np
import pandas as pd
from async_lru import alru_cache
from google.oauth2.service_account import Credentials
from gspread.exceptions import WorksheetNotFound
from gspread.utils import a1_range_to_grid_range, absolute_range_name, rowcol_to_a1

from utils.google_sheets_errors import (
    GOOGLE_SHEETS_EXTERNAL_EXCEPTIONS,
    GoogleSheetsError,
    classify_google_sheets_exception,
)


class _InvalidValuesBatchResponseError(ValueError):
    pass


class _InvalidPdfExportResponseError(ValueError):
    pass


if TYPE_CHECKING:
    from collections.abc import Sequence

    from requests import Response

RGB_CHANNEL_MAX = 0xFF
BORDER_NAMES = (
    "top",
    "bottom",
    "left",
    "right",
    "innerHorizontal",
    "innerVertical",
)


@dataclass(slots=True)
class WorksheetCreationStatus:
    """Track whether this operation completed a worksheet creation."""

    created: bool = False


@dataclass(frozen=True, slots=True)
class GridFormula:
    """An explicitly intended Google Sheets formula value."""

    value: str

    def __post_init__(self) -> None:
        if not isinstance(self.value, str):
            msg = "A grid formula value must be a string."
            raise TypeError(msg)
        if not self.value.startswith("="):
            msg = "A grid formula value must begin with '='."
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class GridValueUpdate:
    """One typed rectangular value update in zero-based API coordinates."""

    worksheet_id: int
    start_row_index: int
    start_column_index: int
    rows: tuple[tuple[object, ...], ...]

    @classmethod
    def from_values(
        cls,
        *,
        worksheet_id: int,
        start_row: int,
        start_column: int,
        values: Sequence[Sequence[object]],
    ) -> GridValueUpdate:
        """Create an update from one-based domain row and column coordinates."""
        start_row = _positive_integer(start_row, "A grid update row")
        start_column = _positive_integer(start_column, "A grid update column")
        normalized_values = tuple(
            tuple(_normalize_grid_value(value) for value in row) for row in values
        )
        if (
            not normalized_values
            or not normalized_values[0]
            or any(len(row) != len(normalized_values[0]) for row in normalized_values)
        ):
            msg = "A grid update must contain a non-empty rectangular value matrix."
            raise ValueError(msg)
        return cls(
            worksheet_id,
            start_row - 1,
            start_column - 1,
            normalized_values,
        )


@dataclass(frozen=True, slots=True)
class DimensionMutation:
    """One append, insert, or delete mutation for worksheet grid dimensions."""

    worksheet_id: int
    operation: str
    dimension: str
    start_index: int | None = None
    end_index: int | None = None
    length: int | None = None

    @classmethod
    def append_rows(cls, worksheet_id: int, count: int) -> DimensionMutation:
        return cls._append(worksheet_id, "ROWS", count)

    @classmethod
    def append_columns(cls, worksheet_id: int, count: int) -> DimensionMutation:
        return cls._append(worksheet_id, "COLUMNS", count)

    @classmethod
    def insert_columns(
        cls,
        worksheet_id: int,
        *,
        start_column: int,
        count: int = 1,
    ) -> DimensionMutation:
        return cls._range(worksheet_id, "insert", "COLUMNS", start_column, count)

    @classmethod
    def delete_columns(
        cls,
        worksheet_id: int,
        *,
        start_column: int,
        count: int = 1,
    ) -> DimensionMutation:
        return cls._range(worksheet_id, "delete", "COLUMNS", start_column, count)

    @classmethod
    def delete_rows(
        cls,
        worksheet_id: int,
        *,
        start_row: int,
        count: int = 1,
    ) -> DimensionMutation:
        return cls._range(worksheet_id, "delete", "ROWS", start_row, count)

    @classmethod
    def _append(
        cls,
        worksheet_id: int,
        dimension: str,
        count: int,
    ) -> DimensionMutation:
        count = _positive_integer(count, "A dimension mutation count")
        return cls(worksheet_id, "append", dimension, length=count)

    @classmethod
    def _range(
        cls,
        worksheet_id: int,
        operation: str,
        dimension: str,
        start: int,
        count: int,
    ) -> DimensionMutation:
        start = _positive_integer(start, "A dimension mutation start")
        count = _positive_integer(count, "A dimension mutation count")
        start_index = start - 1
        return cls(
            worksheet_id,
            operation,
            dimension,
            start_index,
            start_index + count,
        )


def _positive_integer(value: object, name: str) -> int:
    msg = f"{name} must be a positive integer."
    if isinstance(value, bool):
        raise ValueError(msg)  # noqa: TRY004 - one domain validation error type
    try:
        integer = index(value)
    except TypeError:
        raise ValueError(msg) from None
    if integer < 1:
        raise ValueError(msg)
    return integer


class RhobotoGspreadClientManager(gspread_asyncio.AsyncioGspreadClientManager):
    async def handle_gspread_error(
        self,
        e: Exception,
        _method: object,
        _args: object,
        _kwargs: object,
    ) -> None:
        raise e

    async def handle_requests_error(
        self,
        e: Exception,
        _method: object,
        _args: object,
        _kwargs: object,
    ) -> None:
        raise e


class AsyncioGspreadWorksheet:
    """Adapter for the Google Sheets worksheet operations used by Rhoboto."""

    def __init__(self, worksheet: gspread_asyncio.AsyncioGspreadWorksheet) -> None:
        self._worksheet = worksheet

    @property
    def worksheet(self) -> gspread_asyncio.AsyncioGspreadWorksheet:
        return self._worksheet

    @property
    def id(self) -> int:
        return self._worksheet.id

    @property
    def title(self) -> str:
        return self._worksheet.title

    @property
    def is_gridlines_hidden(self) -> bool:
        return self._worksheet.ws.is_gridlines_hidden

    def __getattr__(self, name: str) -> object:
        return getattr(self._worksheet, name)

    async def get_conditional_format_rules(self) -> list[dict[str, object]]:
        """Return this worksheet's conditional-format rules."""
        try:
            metadata = await self._worksheet.agcm._call(  # noqa: SLF001
                self._worksheet.ws.client.fetch_sheet_metadata,
                self._worksheet.ws.spreadsheet_id,
                params={"fields": "sheets(properties(sheetId),conditionalFormats)"},
            )
        except GoogleSheetsError:
            raise
        except GOOGLE_SHEETS_EXTERNAL_EXCEPTIONS as exc:
            _raise_google_sheets_error(exc, "read_worksheet")
        sheet = next(
            (
                item
                for item in metadata.get("sheets", [])
                if item.get("properties", {}).get("sheetId") == self.id
            ),
            {},
        )
        return list(sheet.get("conditionalFormats", []))

    async def get_effective_background_colors(self, range_name: str) -> list[str]:
        """Return concrete effective background colors from one worksheet range."""
        try:
            metadata = await self._worksheet.agcm._call(  # noqa: SLF001
                self._worksheet.ws.client.fetch_sheet_metadata,
                self._worksheet.ws.spreadsheet_id,
                params={
                    "ranges": absolute_range_name(self.title, range_name),
                    "fields": (
                        "properties.spreadsheetTheme.themeColors,"
                        "sheets(properties.sheetId,data.rowData.values."
                        "effectiveFormat.backgroundColorStyle)"
                    ),
                },
            )
            return _effective_background_colors(metadata, self.id)
        except GoogleSheetsError:
            raise
        except GOOGLE_SHEETS_EXTERNAL_EXCEPTIONS as exc:
            _raise_google_sheets_error(exc, "read_worksheet")

    def typed_update_requests(  # noqa: PLR0913
        self,
        data: list[dict[str, object]],
        *,
        formula_ranges: set[str],
        background_updates: Sequence[tuple[str, str]] = (),
        border_updates: Sequence[tuple[str, str | None, str, Sequence[str]]] = (),
        format_updates: Sequence[tuple[str, dict[str, object], str]] = (),
        column_width_updates: Sequence[tuple[str, int]] = (),
        hidden_column_updates: Sequence[tuple[str, bool]] = (),
        conditional_format_rule_deletes: Sequence[int] = (),
        conditional_format_rule_adds: Sequence[dict[str, object]] = (),
        frozen_column_count: int | None = None,
        min_rows: int | None = None,
        min_cols: int | None = None,
    ) -> list[dict[str, object]]:
        """Build typed value and narrow formatting requests without sending them."""
        requests = _worksheet_growth_requests(
            self.id,
            current_rows=self._worksheet.row_count,
            current_cols=self._worksheet.col_count,
            min_rows=min_rows,
            min_cols=min_cols,
        )
        for item in data:
            range_name = str(item["range"])
            values = item["values"]
            if not isinstance(values, list):
                msg = f"Invalid values for range {range_name!r}."
                raise TypeError(msg)
            requests.append(
                {
                    "updateCells": {
                        "range": a1_range_to_grid_range(range_name, self.id),
                        "rows": [
                            {
                                "values": [
                                    {
                                        "userEnteredValue": _extended_value(
                                            value,
                                            formulas=range_name in formula_ranges,
                                        )
                                    }
                                    for value in row
                                ]
                            }
                            for row in values
                        ],
                        "fields": "userEnteredValue",
                    }
                }
            )
        for range_name, color in background_updates:
            requests.append(
                {
                    "repeatCell": {
                        "range": a1_range_to_grid_range(range_name, self.id),
                        "cell": {
                            "userEnteredFormat": {
                                "backgroundColorStyle": {"rgbColor": _hex_rgb(color)}
                            }
                        },
                        "fields": "userEnteredFormat.backgroundColorStyle",
                    }
                }
            )
        for range_name, color, style, sides in border_updates:
            border = (
                {"style": "NONE"}
                if color is None
                else {
                    "style": style,
                    "colorStyle": {"rgbColor": _hex_rgb(color)},
                }
            )
            requests.append(
                {
                    "updateBorders": {
                        "range": a1_range_to_grid_range(range_name, self.id),
                        **dict.fromkeys(sides, border),
                    }
                }
            )
        requests.extend(
            _presentation_requests(
                self.id,
                format_updates=format_updates,
                column_width_updates=column_width_updates,
                hidden_column_updates=hidden_column_updates,
                conditional_format_rule_deletes=conditional_format_rule_deletes,
                conditional_format_rule_adds=conditional_format_rule_adds,
            )
        )
        if frozen_column_count is not None:
            requests.append(
                {
                    "updateSheetProperties": {
                        "properties": {
                            "sheetId": self.id,
                            "gridProperties": {
                                "frozenColumnCount": frozen_column_count
                            },
                        },
                        "fields": "gridProperties.frozenColumnCount",
                    }
                }
            )
        return requests

    async def batch_update_typed_values(  # noqa: PLR0913
        self,
        data: list[dict[str, object]],
        *,
        formula_ranges: set[str],
        background_updates: Sequence[tuple[str, str]] = (),
        border_updates: Sequence[tuple[str, str | None, str, Sequence[str]]] = (),
        format_updates: Sequence[tuple[str, dict[str, object], str]] = (),
        column_width_updates: Sequence[tuple[str, int]] = (),
        hidden_column_updates: Sequence[tuple[str, bool]] = (),
        conditional_format_rule_deletes: Sequence[int] = (),
        conditional_format_rule_adds: Sequence[dict[str, object]] = (),
        frozen_column_count: int | None = None,
        min_rows: int | None = None,
        min_cols: int | None = None,
    ) -> None:
        """Atomically write typed values plus narrow grid formatting."""
        requests = self.typed_update_requests(
            data,
            formula_ranges=formula_ranges,
            background_updates=background_updates,
            border_updates=border_updates,
            format_updates=format_updates,
            column_width_updates=column_width_updates,
            hidden_column_updates=hidden_column_updates,
            conditional_format_rule_deletes=conditional_format_rule_deletes,
            conditional_format_rule_adds=conditional_format_rule_adds,
            frozen_column_count=frozen_column_count,
            min_rows=min_rows,
            min_cols=min_cols,
        )
        if not requests:
            return
        try:
            await self._worksheet.agcm._call(  # noqa: SLF001
                self._worksheet.ws.client.batch_update,
                self._worksheet.ws.spreadsheet_id,
                {"requests": requests},
            )
        except GoogleSheetsError:
            raise
        except GOOGLE_SHEETS_EXTERNAL_EXCEPTIONS as exc:
            _raise_google_sheets_error(exc, "update_worksheet")

    async def delete_row(self, index: int) -> None:
        """Delete one physical worksheet row by its one-based index."""
        try:
            await self._worksheet.delete_rows(index)
        except GoogleSheetsError:
            raise
        except GOOGLE_SHEETS_EXTERNAL_EXCEPTIONS as exc:
            _raise_google_sheets_error(exc, "delete_worksheet_row")


def _extended_value(value: object, *, formulas: bool) -> dict[str, object]:
    value = _normalize_grid_value(value)
    if value is None or value == "":
        return {}
    if formulas:
        if not isinstance(value, str) or not value.startswith("="):
            msg = "A formula range value must begin with '='."
            raise ValueError(msg)
        return {"formulaValue": value}
    if isinstance(value, bool):
        return {"boolValue": value}
    if isinstance(value, int | float):
        return {"numberValue": value}
    return {"stringValue": str(value)}


def _worksheet_growth_requests(
    worksheet_id: int,
    *,
    current_rows: int,
    current_cols: int,
    min_rows: int | None,
    min_cols: int | None,
) -> list[dict[str, object]]:
    requests = []
    if min_rows is not None:
        min_rows = _positive_integer(min_rows, "Minimum worksheet row count")
        if min_rows > current_rows:
            requests.append(
                _dimension_request(
                    DimensionMutation.append_rows(
                        worksheet_id,
                        min_rows - current_rows,
                    )
                )
            )
    if min_cols is not None:
        min_cols = _positive_integer(min_cols, "Minimum worksheet column count")
        if min_cols > current_cols:
            requests.append(
                _dimension_request(
                    DimensionMutation.append_columns(
                        worksheet_id,
                        min_cols - current_cols,
                    )
                )
            )
    return requests


def _normalize_grid_value(value: object) -> object:
    if value is None or value is pd.NA or value is pd.NaT:
        return None
    if isinstance(value, GridFormula):
        return value
    if isinstance(value, (float, np.floating)) and not np.isfinite(value):
        msg = "Non-finite grid value is not valid JSON."
        raise ValueError(msg)
    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, str | bool | int | float):
        return value
    msg = f"Unsupported grid value type: {type(value).__name__}."
    raise TypeError(msg)


def _hex_rgb(value: str) -> dict[str, float]:
    if not re.fullmatch(r"#[0-9A-Fa-f]{6}", value):
        msg = f"Invalid RGB color: {value!r}."
        raise ValueError(msg)
    return {
        name: int(value[start : start + 2], 16) / RGB_CHANNEL_MAX
        for name, start in (("red", 1), ("green", 3), ("blue", 5))
    }


def _rgb_hex_string(value: dict[str, object]) -> str:
    channels: list[int] = []
    for name in ("red", "green", "blue"):
        channel = value.get(name, 0)
        if (
            isinstance(channel, bool)
            or not isinstance(channel, int | float)
            or not 0 <= channel <= 1
        ):
            msg = "Invalid Google Sheets RGB channel."
            raise ValueError(msg)
        channels.append(round(channel * RGB_CHANNEL_MAX))
    return "#" + "".join(f"{channel:02X}" for channel in channels)


def _effective_background_colors(
    metadata: dict[str, object],
    worksheet_id: int,
) -> list[str]:
    theme_colors = {
        item.get("colorType"): item.get("color", {}).get("rgbColor")
        for item in metadata.get("properties", {})
        .get("spreadsheetTheme", {})
        .get("themeColors", [])
        if isinstance(item, dict) and isinstance(item.get("color"), dict)
    }
    sheet = next(
        (
            item
            for item in metadata.get("sheets", [])
            if item.get("properties", {}).get("sheetId") == worksheet_id
        ),
        {},
    )
    colors: list[str] = []
    for data in sheet.get("data", []):
        for row in data.get("rowData", []):
            for cell in row.get("values", []):
                style = cell.get("effectiveFormat", {}).get("backgroundColorStyle", {})
                rgb = style.get("rgbColor")
                if rgb is None:
                    rgb = theme_colors.get(style.get("themeColor"))
                if isinstance(rgb, dict):
                    colors.append(_rgb_hex_string(rgb))
    return list(dict.fromkeys(colors))


def _column_dimension_request(
    range_name: str,
    worksheet_id: int,
    *,
    properties: dict[str, object],
    fields: str,
) -> dict[str, object]:
    grid_range = a1_range_to_grid_range(range_name, worksheet_id)
    return {
        "updateDimensionProperties": {
            "range": {
                "sheetId": worksheet_id,
                "dimension": "COLUMNS",
                "startIndex": grid_range["startColumnIndex"],
                "endIndex": grid_range["endColumnIndex"],
            },
            "properties": properties,
            "fields": fields,
        }
    }


def _presentation_requests(  # noqa: PLR0913
    worksheet_id: int,
    *,
    format_updates: Sequence[tuple[str, dict[str, object], str]],
    column_width_updates: Sequence[tuple[str, int]],
    hidden_column_updates: Sequence[tuple[str, bool]],
    conditional_format_rule_deletes: Sequence[int],
    conditional_format_rule_adds: Sequence[dict[str, object]],
) -> list[dict[str, object]]:
    requests = [
        {
            "repeatCell": {
                "range": a1_range_to_grid_range(range_name, worksheet_id),
                "cell": {"userEnteredFormat": cell_format},
                "fields": fields,
            }
        }
        for range_name, cell_format, fields in format_updates
    ]
    requests.extend(
        _column_dimension_request(
            range_name,
            worksheet_id,
            properties={"pixelSize": pixel_size},
            fields="pixelSize",
        )
        for range_name, pixel_size in column_width_updates
    )
    requests.extend(
        _column_dimension_request(
            range_name,
            worksheet_id,
            properties={"hiddenByUser": hidden},
            fields="hiddenByUser",
        )
        for range_name, hidden in hidden_column_updates
    )
    requests.extend(
        {
            "deleteConditionalFormatRule": {
                "sheetId": worksheet_id,
                "index": index,
            }
        }
        for index in conditional_format_rule_deletes
    )
    requests.extend(
        {
            "addConditionalFormatRule": {
                "rule": rule,
                "index": 0,
            }
        }
        for rule in conditional_format_rule_adds
    )
    return requests


def _grid_value_request(update: GridValueUpdate) -> dict[str, object]:
    return {
        "updateCells": {
            "range": {
                "sheetId": update.worksheet_id,
                "startRowIndex": update.start_row_index,
                "endRowIndex": update.start_row_index + len(update.rows),
                "startColumnIndex": update.start_column_index,
                "endColumnIndex": update.start_column_index + len(update.rows[0]),
            },
            "rows": [
                {
                    "values": [
                        {"userEnteredValue": _grid_extended_value(value)}
                        for value in row
                    ]
                }
                for row in update.rows
            ],
            "fields": "userEnteredValue",
        }
    }


def _grid_extended_value(value: object) -> dict[str, object]:
    if isinstance(value, GridFormula):
        return {"formulaValue": value.value}
    return _extended_value(value, formulas=False)


def _dimension_request(mutation: DimensionMutation) -> dict[str, object]:
    if mutation.operation == "append":
        return {
            "appendDimension": {
                "sheetId": mutation.worksheet_id,
                "dimension": mutation.dimension,
                "length": mutation.length,
            }
        }
    request = {
        "range": {
            "sheetId": mutation.worksheet_id,
            "dimension": mutation.dimension,
            "startIndex": mutation.start_index,
            "endIndex": mutation.end_index,
        }
    }
    if mutation.operation == "insert":
        request["inheritFromBefore"] = False
    return {f"{mutation.operation}Dimension": request}


def _grid_mutation_request(
    mutation: GridValueUpdate | DimensionMutation,
) -> dict[str, object]:
    if isinstance(mutation, GridValueUpdate):
        return _grid_value_request(mutation)
    return _dimension_request(mutation)


class GoogleSheet:
    def __init__(self, sheet_url: str, service_account_path: str) -> None:
        """
        Initialize a GoogleSheet API wrapper.

        Args:
            sheet_url (str): The URL of the Google Sheet.
            service_account_path (str): Path to the Google service account JSON file.
        """
        self.sheet_url = sheet_url
        self.service_account_path = service_account_path
        self._agcm = RhobotoGspreadClientManager(self._get_creds)

    @staticmethod
    def _wrap_worksheet(
        worksheet: gspread_asyncio.AsyncioGspreadWorksheet | None,
    ) -> AsyncioGspreadWorksheet | None:
        if worksheet is None:
            return None
        return AsyncioGspreadWorksheet(worksheet)

    def _get_creds(self) -> Credentials:
        """
        Get Google API credentials from the service account file.

        Returns:
            Credentials: Google API credentials for spreadsheet access.
        """
        try:
            return Credentials.from_service_account_file(
                self.service_account_path,
                scopes=["https://www.googleapis.com/auth/spreadsheets"],
            )
        except GoogleSheetsError:
            raise
        except GOOGLE_SHEETS_EXTERNAL_EXCEPTIONS as exc:
            _raise_google_sheets_error(exc, "load_credentials")

    @property
    @alru_cache
    async def sheet(self) -> gspread_asyncio.AsyncioGspreadSpreadsheet:
        """
        Get the Google Spreadsheet object, using cached instance if available.

        Returns:
            AsyncioGspreadSpreadsheet: The spreadsheet object.
        """
        try:
            agc = await self._agcm.authorize()
            return await agc.open_by_url(self.sheet_url)
        except GoogleSheetsError:
            raise
        except GOOGLE_SHEETS_EXTERNAL_EXCEPTIONS as exc:
            _raise_google_sheets_error(exc, "open_spreadsheet")

    async def batch_update_grid(
        self,
        mutations: Sequence[GridValueUpdate | DimensionMutation],
        *,
        worksheet_requests: Sequence[dict[str, object]] = (),
    ) -> None:
        """Apply ordered grid mutations and typed worksheet requests atomically."""
        requests = [
            *(_grid_mutation_request(mutation) for mutation in mutations),
            *worksheet_requests,
        ]
        if not requests:
            return
        try:
            sh = await self.sheet
            await sh.batch_update({"requests": requests})
        except GoogleSheetsError:
            raise
        except GOOGLE_SHEETS_EXTERNAL_EXCEPTIONS as exc:
            _raise_google_sheets_error(exc, "update_worksheet")

    async def batch_get_worksheet_values(
        self,
        worksheets: Sequence[AsyncioGspreadWorksheet],
    ) -> dict[int, list[list[object]]]:
        """Read complete value grids from this spreadsheet in one request."""
        if not worksheets:
            return {}
        try:
            sh = await self.sheet
            ranges = [
                absolute_range_name(
                    worksheet.title,
                    f"A1:{rowcol_to_a1(worksheet.row_count, worksheet.col_count)}",
                )
                for worksheet in worksheets
            ]
            response = await sh.values_batch_get(
                ranges,
                params={"valueRenderOption": "FORMULA"},
            )
            if not isinstance(response, dict):
                raise _InvalidValuesBatchResponseError
            value_ranges = response.get("valueRanges")
            if not isinstance(value_ranges, list) or len(value_ranges) != len(
                worksheets
            ):
                raise _InvalidValuesBatchResponseError
            result: dict[int, list[list[object]]] = {}
            for worksheet, value_range in zip(
                worksheets,
                value_ranges,
                strict=True,
            ):
                if not isinstance(value_range, dict):
                    raise _InvalidValuesBatchResponseError
                values = value_range.get("values", [])
                if not isinstance(values, list) or any(
                    not isinstance(row, list) for row in values
                ):
                    raise _InvalidValuesBatchResponseError
                result[worksheet.id] = [list(row) for row in values]
        except GoogleSheetsError:
            raise
        except GOOGLE_SHEETS_EXTERNAL_EXCEPTIONS as exc:
            _raise_google_sheets_error(exc, "read_worksheet")
        return result

    async def export_worksheet_range_pdf(
        self,
        worksheet: AsyncioGspreadWorksheet,
        range_a1: str,
    ) -> bytes:
        """Export one worksheet rectangle as a Google-rendered PDF."""
        try:
            sh = await self.sheet
            grid_range = a1_range_to_grid_range(range_a1)
            params: dict[str, str | int | float] = {
                "format": "pdf",
                "gid": worksheet.id,
                "r1": grid_range["startRowIndex"],
                "c1": grid_range["startColumnIndex"],
                "r2": grid_range["endRowIndex"],
                "c2": grid_range["endColumnIndex"],
                "portrait": "false",
                "fitw": "true",
                "top_margin": 0.1,
                "bottom_margin": 0.1,
                "left_margin": 0.1,
                "right_margin": 0.1,
                "sheetnames": "false",
                "printtitle": "false",
                "pagenum": "UNDEFINED",
                "fzr": "false",
                "gridlines": "false" if worksheet.is_gridlines_hidden else "true",
                "attachment": "true",
            }
            endpoint = f"https://docs.google.com/spreadsheets/d/{sh.ss.id}/export"

            def request_pdf() -> Response:
                return sh.ss.client.request("get", endpoint, params=params)

            response = await sh.agcm._call(request_pdf)  # noqa: SLF001
            content_type = (
                response.headers.get("Content-Type", "")
                .partition(";")[0]
                .strip()
                .casefold()
            )
            pdf_bytes = response.content
            if (
                not response.ok
                or content_type != "application/pdf"
                or not isinstance(pdf_bytes, bytes)
                or not pdf_bytes
            ):
                raise _InvalidPdfExportResponseError
        except GoogleSheetsError:
            raise
        except GOOGLE_SHEETS_EXTERNAL_EXCEPTIONS as exc:
            _raise_google_sheets_error(exc, "export_worksheet")
        return pdf_bytes

    async def get_worksheet(self, worksheet_id: int) -> AsyncioGspreadWorksheet | None:
        """
        Get a worksheet by its ID, returned as AsyncioGspreadWorksheet.

        Args:
            worksheet_id (int): The worksheet ID.

        Returns:
            AsyncioGspreadWorksheet | None: The worksheet object, or None if not found.
        """
        try:
            sh = await self.sheet
            ws = await sh.get_worksheet_by_id(worksheet_id)
        except WorksheetNotFound:
            return None
        except GoogleSheetsError:
            raise
        except GOOGLE_SHEETS_EXTERNAL_EXCEPTIONS as exc:
            _raise_google_sheets_error(exc, "get_worksheet")
        else:
            return self._wrap_worksheet(ws)

    async def get_worksheets(
        self, worksheet_ids: list[int]
    ) -> dict[int, AsyncioGspreadWorksheet | None]:
        """
        Get multiple worksheets by their IDs, returned as AsyncioGspreadWorksheet.

        Args:
            worksheet_ids (list[int]): List of worksheet IDs.

        Returns:
            list[AsyncioGspreadWorksheet | None]:
                List of worksheet objects or None for not found.
        """
        try:
            sh = await self.sheet
            all_worksheets = await sh.worksheets()
        except GoogleSheetsError:
            raise
        except GOOGLE_SHEETS_EXTERNAL_EXCEPTIONS as exc:
            _raise_google_sheets_error(exc, "get_worksheets")
        id_to_ws = {ws.id: ws for ws in all_worksheets}
        result = {}
        for ws_id in worksheet_ids:
            ws = id_to_ws.get(ws_id)
            result[ws_id] = self._wrap_worksheet(ws)
        return result

    async def get_or_create_worksheet(
        self,
        worksheet_title: str,
        default_rows: int = 100,
        default_cols: int = 20,
    ) -> AsyncioGspreadWorksheet:
        """
        Get or create a worksheet by its title, returned as AsyncioGspreadWorksheet.

        Args:
            worksheet_title (str): The title of the worksheet to get or create.
            default_rows (int, optional):
                Default number of rows for new worksheets. Defaults to 100.
            default_cols (int, optional):
                Default number of columns for new worksheets. Defaults to 20.

        Returns:
            AsyncioGspreadWorksheet: The worksheet object.
        """
        try:
            sh = await self.sheet
            ws = await sh.worksheet(worksheet_title)
        except WorksheetNotFound:
            try:
                ws = await sh.add_worksheet(
                    worksheet_title, rows=default_rows, cols=default_cols
                )
            except GoogleSheetsError:
                raise
            except GOOGLE_SHEETS_EXTERNAL_EXCEPTIONS as exc:
                _raise_google_sheets_error(exc, "create_worksheet")
        except GoogleSheetsError:
            raise
        except GOOGLE_SHEETS_EXTERNAL_EXCEPTIONS as exc:
            _raise_google_sheets_error(exc, "get_or_create_worksheet")
        return AsyncioGspreadWorksheet(ws)

    async def get_or_create_worksheets(
        self,
        worksheet_titles: list[str],
        default_rows: int = 100,
        default_cols: int = 20,
        *,
        creation_status: WorksheetCreationStatus | None = None,
    ) -> dict[str, AsyncioGspreadWorksheet]:
        """
        Get or create worksheets by their titles, returned as AsyncioGspreadWorksheet.

        Args:
            worksheet_titles (list[str]): List of worksheet titles to get or create.
            default_rows (int, optional):
                Default number of rows for new worksheets. Defaults to 100.
            default_cols (int, optional):
                Default number of columns for new worksheets. Defaults to 20.
            creation_status: Tracks whether a worksheet creation completed.

        Returns:
            dict[str, AsyncioGspreadWorksheet]:
                Dictionary of worksheet titles to worksheet objects.
        """
        try:
            sh = await self.sheet
            all_worksheets = await sh.worksheets()
        except GoogleSheetsError:
            raise
        except GOOGLE_SHEETS_EXTERNAL_EXCEPTIONS as exc:
            _raise_google_sheets_error(exc, "get_or_create_worksheets")
        title_to_ws = {ws.title: ws for ws in all_worksheets}
        result = {}
        for worksheet_title in worksheet_titles:
            ws = title_to_ws.get(worksheet_title)
            if ws is None:
                try:
                    ws = await sh.add_worksheet(
                        worksheet_title, rows=default_rows, cols=default_cols
                    )
                    if creation_status is not None:
                        creation_status.created = True
                except GoogleSheetsError:
                    raise
                except GOOGLE_SHEETS_EXTERNAL_EXCEPTIONS as exc:
                    _raise_google_sheets_error(exc, "create_worksheet")
            wrapped_ws = AsyncioGspreadWorksheet(ws)
            result[wrapped_ws.title] = wrapped_ws
        return result


def _raise_google_sheets_error(exc: Exception, operation: str) -> NoReturn:
    raise classify_google_sheets_exception(exc, operation=operation) from exc
