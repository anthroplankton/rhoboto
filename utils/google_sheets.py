from __future__ import annotations

from typing import NoReturn

import gspread_asyncio
import pandas as pd
from async_lru import alru_cache
from google.oauth2.service_account import Credentials
from gspread.exceptions import WorksheetNotFound

from utils.google_sheets_errors import (
    GOOGLE_SHEETS_EXTERNAL_EXCEPTIONS,
    GoogleSheetsError,
    classify_google_sheets_exception,
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
