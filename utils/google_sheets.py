from __future__ import annotations

import gspread_asyncio
import pandas as pd
from async_lru import alru_cache
from google.oauth2.service_account import Credentials
from gspread.exceptions import WorksheetNotFound


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
        values = await self._worksheet.get(value_render_option="FORMULA")
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

    async def update_from_dataframe(self, df: pd.DataFrame) -> None:
        """
        Update worksheet from a pandas DataFrame.

        Args:
            df (pd.DataFrame): DataFrame to upload to worksheet.
        """
        df = df.fillna("")
        values = [df.columns.tolist(), *df.to_numpy().tolist()]
        await self._worksheet.update(values, raw=False)


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
        self._agcm = gspread_asyncio.AsyncioGspreadClientManager(self._get_creds)

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
        return Credentials.from_service_account_file(
            self.service_account_path,
            scopes=["https://www.googleapis.com/auth/spreadsheets"],
        )

    @property
    @alru_cache
    async def sheet(self) -> gspread_asyncio.AsyncioGspreadSpreadsheet:
        """
        Get the Google Spreadsheet object, using cached instance if available.

        Returns:
            AsyncioGspreadSpreadsheet: The spreadsheet object.
        """
        agc = await self._agcm.authorize()
        return await agc.open_by_url(self.sheet_url)

    async def get_worksheet(self, worksheet_id: int) -> AsyncioGspreadWorksheet | None:
        """
        Get a worksheet by its ID, returned as AsyncioGspreadWorksheet.

        Args:
            worksheet_id (int): The worksheet ID.

        Returns:
            AsyncioGspreadWorksheet | None: The worksheet object, or None if not found.
        """
        sh = await self.sheet
        try:
            ws = await sh.get_worksheet_by_id(worksheet_id)
        except WorksheetNotFound:
            return None
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
        sh = await self.sheet
        all_worksheets = await sh.worksheets()
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
        sh = await self.sheet
        try:
            ws = await sh.worksheet(worksheet_title)
        except WorksheetNotFound:
            ws = await sh.add_worksheet(
                worksheet_title, rows=default_rows, cols=default_cols
            )
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
        sh = await self.sheet
        all_worksheets = await sh.worksheets()
        title_to_ws = {ws.title: ws for ws in all_worksheets}
        result = {}
        for worksheet_title in worksheet_titles:
            ws = title_to_ws.get(worksheet_title)
            if ws is None:
                ws = await sh.add_worksheet(
                    worksheet_title, rows=default_rows, cols=default_cols
                )
            wrapped_ws = AsyncioGspreadWorksheet(ws)
            result[wrapped_ws.title] = wrapped_ws
        return result
