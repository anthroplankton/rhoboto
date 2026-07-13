from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest
from tortoise.exceptions import DBConnectionError

from utils import manager_base as manager_base_module
from utils.google_sheets_errors import GoogleSheetsError, GoogleSheetsErrorKind
from utils.manager_base import ManagerBase
from utils.shift_register_structs import EntryWorksheetMetadata
from utils.storage_errors import StorageError, StorageErrorKind
from utils.structs_base import GoogleSheetsMetadata, WorksheetMetadata

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


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


class StagedEnsureGoogleSheet:
    sheet_url = "https://docs.google.com/spreadsheets/d/shift-ensure/edit"

    def __init__(
        self,
        *,
        lookup_error: GoogleSheetsError | None = None,
        create_error_title: str | None = None,
    ) -> None:
        self.lookup_error = lookup_error
        self.create_error_title = create_error_title
        self.created_titles: list[str] = []

    async def get_or_create_worksheets(
        self,
        worksheet_titles: list[str],
        *,
        creation_status: object | None = None,
    ) -> dict[str, SimpleNamespace]:
        if self.lookup_error is not None:
            raise self.lookup_error

        worksheets = {}
        for index, title in enumerate(worksheet_titles, start=1):
            if title == self.create_error_title:
                raise GoogleSheetsError(
                    GoogleSheetsErrorKind.TRANSIENT,
                    "private create failure",
                )
            worksheet = SimpleNamespace(id=index, title=title)
            worksheets[title] = worksheet
            self.created_titles.append(title)
            if creation_status is not None:
                creation_status.created = True
        return worksheets


class StagedEnsureManager(ManagerBase[object, GoogleSheetsMetadata]):
    SheetConfigType = object
    GoogleSheetsMetadataType = GoogleSheetsMetadata

    def __init__(
        self,
        sheet: StagedEnsureGoogleSheet,
        *,
        save_error: Exception | None = None,
    ) -> None:
        super().__init__(SimpleNamespace(), "service.json")
        self._google_sheet = sheet  # type: ignore[assignment]
        self.save_error = save_error
        self.saved_metadata: list[GoogleSheetsMetadata] = []

    async def upsert_sheet_config(self, metadata: GoogleSheetsMetadata) -> None:
        self.saved_metadata.append(metadata)
        if self.save_error is not None:
            raise self.save_error


def missing_metadata(*titles: str) -> GoogleSheetsMetadata:
    return GoogleSheetsMetadata(
        StagedEnsureGoogleSheet.sheet_url,
        [WorksheetMetadata(None, title, None) for title in titles],
    )


def complete_metadata(*titles: str) -> GoogleSheetsMetadata:
    return GoogleSheetsMetadata(
        StagedEnsureGoogleSheet.sheet_url,
        [
            WorksheetMetadata(
                index,
                title,
                SimpleNamespace(id=index, title=title),
            )
            for index, title in enumerate(titles, start=1)
        ],
    )


@pytest.mark.asyncio
async def test_upsert_sheet_config_merges_explicit_extra_defaults() -> None:
    update_or_create = AsyncMock(return_value=(SimpleNamespace(), False))
    manager = ManagerBase(SimpleNamespace(), "service.json")
    manager.SheetConfigType = SimpleNamespace(update_or_create=update_or_create)
    metadata = GoogleSheetsMetadata(
        "https://docs.google.com/spreadsheets/d/shift-settings/edit",
        [EntryWorksheetMetadata(1, "Entry", None)],
    )

    await manager.upsert_sheet_config(
        metadata,
        extra_defaults={"final_schedule_anchor_cell": "A1"},
    )

    defaults = update_or_create.await_args.kwargs["defaults"]
    assert defaults == {
        "sheet_url": metadata.sheet_url,
        "entry_worksheet_id": 1,
        "final_schedule_anchor_cell": "A1",
    }


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


def test_spreadsheet_transaction_key_classifies_invalid_url() -> None:
    with pytest.raises(GoogleSheetsError) as exc_info:
        manager_base_module.spreadsheet_transaction_key("not a Google Sheet URL")

    error = exc_info.value
    assert error.kind is GoogleSheetsErrorKind.INVALID_URL
    assert error.user_message == (
        "Check the Google Sheet link and save the settings again."
    )
    assert isinstance(error.__cause__, ValueError)


@pytest.mark.asyncio
async def test_invalid_spreadsheet_transaction_url_enters_no_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    @asynccontextmanager
    async def recording_lock(_key: object) -> AsyncIterator[None]:
        events.append("lock")
        yield

    monkeypatch.setattr(
        manager_base_module,
        "SPREADSHEET_TRANSACTION_LOCK",
        recording_lock,
    )

    with pytest.raises(GoogleSheetsError):
        async with manager_base_module.spreadsheet_transaction(
            recording_lock,
            channel_id=22,
            sheet_url="not a Google Sheet URL",
        ):
            events.append("body")

    assert events == []


@pytest.mark.asyncio
async def test_combined_ensure_marks_later_creation_failure_partial() -> None:
    sheet = StagedEnsureGoogleSheet(create_error_title="Draft")
    manager = StagedEnsureManager(sheet)

    with pytest.raises(StorageError) as exc_info:
        await manager.ensure_worksheets_and_upsert_sheet_config(
            missing_metadata("Entry", "Draft", "Final Schedule")
        )

    assert sheet.created_titles == ["Entry"]
    assert manager.saved_metadata == []
    assert exc_info.value.kind is StorageErrorKind.PARTIAL_SUCCESS
    assert isinstance(exc_info.value.__cause__, StorageError)
    assert exc_info.value.__cause__.kind is StorageErrorKind.GOOGLE_SHEETS_TRANSIENT


@pytest.mark.asyncio
async def test_combined_ensure_is_noop_when_all_worksheets_are_resolved() -> None:
    sheet = StagedEnsureGoogleSheet(
        lookup_error=GoogleSheetsError(
            GoogleSheetsErrorKind.TRANSIENT,
            "worksheet lookup should not run",
        )
    )
    manager = StagedEnsureManager(sheet)
    metadata = complete_metadata("Entry", "Draft", "Final Schedule")

    ensured = await manager.ensure_worksheets_and_upsert_sheet_config(metadata)

    assert ensured is metadata
    assert sheet.created_titles == []
    assert manager.saved_metadata == []


@pytest.mark.asyncio
async def test_combined_ensure_marks_config_failure_after_creation_partial() -> None:
    save_error = DBConnectionError("private database")
    sheet = StagedEnsureGoogleSheet()
    manager = StagedEnsureManager(sheet, save_error=save_error)

    with pytest.raises(StorageError) as exc_info:
        await manager.ensure_worksheets_and_upsert_sheet_config(
            missing_metadata("Entry")
        )

    assert sheet.created_titles == ["Entry"]
    assert len(manager.saved_metadata) == 1
    assert exc_info.value.kind is StorageErrorKind.PARTIAL_SUCCESS
    assert isinstance(exc_info.value.__cause__, StorageError)
    assert exc_info.value.__cause__.kind is StorageErrorKind.DATABASE_UNAVAILABLE
    assert exc_info.value.__cause__.__cause__ is save_error


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_stage", ["lookup", "first_create"])
async def test_combined_ensure_preserves_error_before_completed_creation(
    failure_stage: str,
) -> None:
    raw_error = GoogleSheetsError(
        GoogleSheetsErrorKind.TRANSIENT,
        "private pre-create failure",
    )
    sheet = StagedEnsureGoogleSheet(
        lookup_error=raw_error if failure_stage == "lookup" else None,
        create_error_title="Entry" if failure_stage == "first_create" else None,
    )
    manager = StagedEnsureManager(sheet)

    with pytest.raises(GoogleSheetsError) as exc_info:
        await manager.ensure_worksheets_and_upsert_sheet_config(
            missing_metadata("Entry", "Draft")
        )

    if failure_stage == "lookup":
        assert exc_info.value is raw_error
    else:
        assert exc_info.value.kind is GoogleSheetsErrorKind.TRANSIENT
    assert sheet.created_titles == []
    assert manager.saved_metadata == []
