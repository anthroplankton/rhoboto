from __future__ import annotations

import re
from typing import TYPE_CHECKING, NoReturn

import gspread_asyncio
import pandas as pd
from async_lru import alru_cache
from google.oauth2.service_account import Credentials
from gspread.exceptions import WorksheetNotFound
from gspread.utils import a1_range_to_grid_range

from utils.google_sheets_errors import (
    GOOGLE_SHEETS_EXTERNAL_EXCEPTIONS,
    GoogleSheetsError,
    classify_google_sheets_exception,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

RGB_CHANNEL_MAX = 0xFF
BORDER_NAMES = (
    "top",
    "bottom",
    "left",
    "right",
    "innerHorizontal",
    "innerVertical",
)


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
    """
    Adapter for AsyncioGspreadWorksheet with DataFrame utilities.
    """

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

    def __getattr__(self, name: str) -> object:
        return getattr(self._worksheet, name)

    async def to_frame(self) -> pd.DataFrame:
        """
        Convert worksheet data to a pandas DataFrame.

        Returns:
            pd.DataFrame:
                DataFrame containing worksheet data.
                Empty if worksheet is empty.
        """
        try:
            values = await self._worksheet.get(value_render_option="FORMULA")
        except GoogleSheetsError:
            raise
        except GOOGLE_SHEETS_EXTERNAL_EXCEPTIONS as exc:
            _raise_google_sheets_error(exc, "read_worksheet")
        if not values:
            return pd.DataFrame()

        header = values[0]
        expected_cols = len(header)
        data = values[1:]

        # Pad rows with empty strings to match the header length
        # gspread omits trailing empty cells, which causes pandas to fail
        for row in data:
            if len(row) < expected_cols:
                row.extend([""] * (expected_cols - len(row)))

        return pd.DataFrame(data, columns=header)

    async def update_from_dataframe(
        self,
        df: pd.DataFrame,
        *,
        raw_data: bool = False,
    ) -> None:
        """
        Update worksheet from a pandas DataFrame.

        Args:
            df (pd.DataFrame): DataFrame to upload to worksheet.
            raw_data (bool): Whether to store data rows without USER_ENTERED parsing.
        """
        df = df.fillna("")
        rows = df.to_numpy().tolist()
        try:
            await self._worksheet.update(
                [df.columns.tolist()],
                range_name="A1",
                raw=True,
            )
            if rows:
                await self._worksheet.update(
                    rows,
                    range_name="A2",
                    raw=raw_data,
                )
        except GoogleSheetsError:
            raise
        except GOOGLE_SHEETS_EXTERNAL_EXCEPTIONS as exc:
            _raise_google_sheets_error(exc, "update_worksheet")

    async def batch_get_values(self, ranges: list[str]) -> list[list[list[object]]]:
        """Read disjoint ranges while preserving formula text."""
        try:
            values = await self._worksheet.batch_get(
                ranges,
                value_render_option="FORMULA",
            )
        except GoogleSheetsError:
            raise
        except GOOGLE_SHEETS_EXTERNAL_EXCEPTIONS as exc:
            _raise_google_sheets_error(exc, "read_worksheet")
        return [list(value_range) for value_range in values]

    async def batch_update_values(self, data: list[dict[str, object]]) -> None:
        """Write disjoint ranges as user-entered values and formulas."""
        try:
            await self._worksheet.batch_update(data, raw=False)
        except GoogleSheetsError:
            raise
        except GOOGLE_SHEETS_EXTERNAL_EXCEPTIONS as exc:
            _raise_google_sheets_error(exc, "update_worksheet")

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
    ) -> None:
        """Atomically write typed values plus narrow grid formatting."""
        requests = []
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

    async def ensure_size(self, *, min_rows: int, min_cols: int) -> None:
        """Grow the worksheet only when the requested grid exceeds its size."""
        current_rows = self._worksheet.row_count
        current_cols = self._worksheet.col_count
        rows = max(current_rows, min_rows)
        cols = max(current_cols, min_cols)
        if (rows, cols) == (current_rows, current_cols):
            return
        try:
            await self._worksheet.resize(rows=rows, cols=cols)
        except GoogleSheetsError:
            raise
        except GOOGLE_SHEETS_EXTERNAL_EXCEPTIONS as exc:
            _raise_google_sheets_error(exc, "resize_worksheet")

    async def delete_row(self, index: int) -> None:
        """Delete one physical worksheet row by its one-based index."""
        try:
            await self._worksheet.delete_rows(index)
        except GoogleSheetsError:
            raise
        except GOOGLE_SHEETS_EXTERNAL_EXCEPTIONS as exc:
            _raise_google_sheets_error(exc, "delete_worksheet_row")


def _extended_value(value: object, *, formulas: bool) -> dict[str, object]:
    if formulas and isinstance(value, str) and value.startswith("="):
        return {"formulaValue": value}
    if isinstance(value, bool):
        return {"boolValue": value}
    if isinstance(value, int | float):
        return {"numberValue": value}
    return {"stringValue": "" if value is None else str(value)}


def _hex_rgb(value: str) -> dict[str, float]:
    if not re.fullmatch(r"#[0-9A-Fa-f]{6}", value):
        msg = f"Invalid RGB color: {value!r}."
        raise ValueError(msg)
    return {
        name: int(value[start : start + 2], 16) / RGB_CHANNEL_MAX
        for name, start in (("red", 1), ("green", 3), ("blue", 5))
    }


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
    ) -> dict[str, AsyncioGspreadWorksheet]:
        """
        Get or create worksheets by their titles, returned as AsyncioGspreadWorksheet.

        Args:
            worksheet_titles (list[str]): List of worksheet titles to get or create.
            default_rows (int, optional):
                Default number of rows for new worksheets. Defaults to 100.
            default_cols (int, optional):
                Default number of columns for new worksheets. Defaults to 20.

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
                except GoogleSheetsError:
                    raise
                except GOOGLE_SHEETS_EXTERNAL_EXCEPTIONS as exc:
                    _raise_google_sheets_error(exc, "create_worksheet")
            wrapped_ws = AsyncioGspreadWorksheet(ws)
            result[wrapped_ws.title] = wrapped_ws
        return result


def _raise_google_sheets_error(exc: Exception, operation: str) -> NoReturn:
    raise classify_google_sheets_exception(exc, operation=operation) from exc
