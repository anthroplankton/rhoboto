from __future__ import annotations

from types import SimpleNamespace

import pytest

from utils.manager_base import ManagerBase
from utils.structs_base import GoogleSheetsMetadata


class FakeGoogleSheet:
    def __init__(self, sheet_url: str, service_account_path: str) -> None:
        self.sheet_url = sheet_url
        self.service_account_path = service_account_path


class RecordingManager(ManagerBase[object, GoogleSheetsMetadata]):
    SheetConfigType = object
    GoogleSheetsMetadataType = GoogleSheetsMetadata

    def __init__(self) -> None:
        super().__init__(SimpleNamespace(), "service.json")
        self.created_sheet_url: str | None = None
        self.persisted_sheet_url: str | None = None
        self.created_titles: list[str] | None = None

    async def create_or_get_worksheets(
        self,
        worksheet_titles: list[str],
    ) -> GoogleSheetsMetadata:
        self.created_titles = worksheet_titles
        assert self._google_sheet is not None
        self.created_sheet_url = self._google_sheet.sheet_url
        return GoogleSheetsMetadata(
            sheet_url=self._google_sheet.sheet_url,
            worksheets=[],
        )

    async def upsert_sheet_config(self, metadata: GoogleSheetsMetadata) -> None:
        self.persisted_sheet_url = metadata.sheet_url


@pytest.mark.asyncio
async def test_upsert_sheet_config_and_worksheets_normalizes_sheet_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("utils.manager_base.GoogleSheet", FakeGoogleSheet)
    manager = RecordingManager()

    metadata = await manager.upsert_sheet_config_and_worksheets(
        "https://docs.google.com/spreadsheets/d/abc/edit?usp=sharing#gid=111",
        ["Team Summary"],
    )

    expected = "https://docs.google.com/spreadsheets/d/abc/edit"
    assert manager.created_titles == ["Team Summary"]
    assert manager.created_sheet_url == expected
    assert manager.persisted_sheet_url == expected
    assert metadata.sheet_url == expected
