from __future__ import annotations

from types import SimpleNamespace

import pytest
from google.auth.exceptions import DefaultCredentialsError, TransportError
from gspread.exceptions import (
    APIError,
    NoValidUrlKeyFound,
    SpreadsheetNotFound,
    WorksheetNotFound,
)
from requests.exceptions import ConnectionError as RequestsConnectionError

from utils.google_sheets import (
    AsyncioGspreadWorksheet,
    GoogleSheet,
    GridValueUpdate,
    RhobotoGspreadClientManager,
)
from utils.google_sheets_errors import (
    GoogleSheetsError,
    GoogleSheetsErrorKind,
    classify_google_sheets_exception,
)


class FakeResponse:
    def __init__(
        self,
        status_code: int,
        *,
        status: str | None = None,
        text: str = "raw secret sheet url",
    ) -> None:
        self.status_code = status_code
        self.text = text
        self._status = status

    def json(self) -> dict[str, dict[str, object]]:
        return {
            "error": {
                "code": self.status_code,
                "message": self.text,
                "status": self._status or "",
            }
        }


class RaisingWorksheet:
    id = 1
    title = "Worksheet"
    row_count = 100
    col_count = 20

    def __init__(self, exc: Exception) -> None:
        self.exc = exc
        self.agcm = RaisingClientManager()
        self.ws = SimpleNamespace(
            spreadsheet_id="spreadsheet-id",
            client=SimpleNamespace(batch_update=self._raise_batch_update),
        )

    async def batch_get(self, _: list[str], **__: object) -> list[list[list[object]]]:
        raise self.exc

    def _raise_batch_update(self, _: str, __: dict[str, object]) -> None:
        raise self.exc


class RaisingClientManager:
    async def _call(
        self,
        method: object,
        *args: object,
        **kwargs: object,
    ) -> object:
        assert callable(method)
        return method(*args, **kwargs)


class RaisingSpreadsheet:
    def __init__(self, exc: Exception) -> None:
        self.exc = exc

    async def batch_update(self, _: dict[str, object]) -> None:
        raise self.exc


class RaisingGoogleSheet(GoogleSheet):
    def __init__(self, exc: Exception) -> None:
        self.spreadsheet = RaisingSpreadsheet(exc)

    @property
    async def sheet(self) -> RaisingSpreadsheet:
        return self.spreadsheet


def api_error(
    status_code: int, status: str, *, text: str = "private detail"
) -> APIError:
    return APIError(FakeResponse(status_code, status=status, text=text))


@pytest.mark.parametrize(
    ("exc", "expected_kind"),
    [
        (api_error(403, "PERMISSION_DENIED"), GoogleSheetsErrorKind.PERMISSION),
        (api_error(429, "RESOURCE_EXHAUSTED"), GoogleSheetsErrorKind.QUOTA),
        (api_error(404, "NOT_FOUND"), GoogleSheetsErrorKind.MISSING_WORKSHEET),
        (api_error(503, "UNAVAILABLE"), GoogleSheetsErrorKind.TRANSIENT),
        (SpreadsheetNotFound("private url"), GoogleSheetsErrorKind.INVALID_URL),
        (NoValidUrlKeyFound("private url"), GoogleSheetsErrorKind.INVALID_URL),
        (
            WorksheetNotFound("private worksheet"),
            GoogleSheetsErrorKind.MISSING_WORKSHEET,
        ),
        (
            DefaultCredentialsError("private credential path"),
            GoogleSheetsErrorKind.PERMISSION,
        ),
        (TransportError("private transport detail"), GoogleSheetsErrorKind.TRANSIENT),
        (PermissionError("private spreadsheet"), GoogleSheetsErrorKind.PERMISSION),
    ],
)
def test_classify_google_sheets_exception_returns_safe_domain_error(
    exc: Exception,
    expected_kind: GoogleSheetsErrorKind,
) -> None:
    error = classify_google_sheets_exception(exc)

    assert error.kind is expected_kind
    assert "private" not in error.user_message
    assert "secret" not in error.user_message
    assert error.__cause__ is exc


def test_google_sheet_uses_bounded_client_manager() -> None:
    sheet = GoogleSheet("https://sheet.example", "service.json")

    assert isinstance(sheet._agcm, RhobotoGspreadClientManager)  # noqa: SLF001


@pytest.mark.asyncio
async def test_gspread_client_manager_does_not_retry_quota_errors_forever() -> None:
    manager = RhobotoGspreadClientManager(object, gspread_delay=0)

    raw_error = api_error(429, "RESOURCE_EXHAUSTED")

    with pytest.raises(APIError) as exc_info:
        await manager.handle_gspread_error(raw_error, object(), (), {})

    assert exc_info.value is raw_error


@pytest.mark.asyncio
async def test_gspread_client_manager_does_not_retry_transport_errors_forever() -> None:
    manager = RhobotoGspreadClientManager(object, gspread_delay=0)

    raw_error = RequestsConnectionError("private transport detail")

    with pytest.raises(RequestsConnectionError) as exc_info:
        await manager.handle_requests_error(raw_error, object(), (), {})

    assert exc_info.value is raw_error


@pytest.mark.asyncio
async def test_worksheet_read_write_raise_domain_errors_without_raw_details() -> None:
    raw_error = api_error(403, "PERMISSION_DENIED", text="secret spreadsheet url")
    worksheet = AsyncioGspreadWorksheet(RaisingWorksheet(raw_error))

    with pytest.raises(GoogleSheetsError) as read_error:
        await worksheet.batch_get_values(["A1"])

    with pytest.raises(GoogleSheetsError) as write_error:
        await worksheet.batch_update_typed_values(
            [{"range": "A1", "values": [["alice"]]}],
            formula_ranges=set(),
        )

    assert read_error.value.kind is GoogleSheetsErrorKind.PERMISSION
    assert write_error.value.kind is GoogleSheetsErrorKind.PERMISSION
    assert "secret spreadsheet url" not in read_error.value.user_message
    assert "secret spreadsheet url" not in write_error.value.user_message


@pytest.mark.asyncio
async def test_grid_batch_raises_safe_domain_error_without_private_values() -> None:
    raw_error = api_error(403, "PERMISSION_DENIED", text="secret alice@example.com")
    sheet = RaisingGoogleSheet(raw_error)
    mutation = GridValueUpdate.from_values(
        worksheet_id=1,
        start_row=1,
        start_column=1,
        values=[["alice@example.com"]],
    )

    with pytest.raises(GoogleSheetsError) as error:
        await sheet.batch_update_grid([mutation])

    assert error.value.kind is GoogleSheetsErrorKind.PERMISSION
    assert error.value.operation == "update_worksheet"
    assert "alice@example.com" not in error.value.user_message
