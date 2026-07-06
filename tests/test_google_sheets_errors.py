from __future__ import annotations

import pandas as pd
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

    def __init__(self, exc: Exception) -> None:
        self.exc = exc

    async def get(self, **_: object) -> list[list[object]]:
        raise self.exc

    async def update(self, _: list[list[object]], **__: object) -> None:
        raise self.exc


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
    manager = RhobotoGspreadClientManager(lambda: object(), gspread_delay=0)

    raw_error = api_error(429, "RESOURCE_EXHAUSTED")

    with pytest.raises(APIError) as exc_info:
        await manager.handle_gspread_error(raw_error, object(), (), {})

    assert exc_info.value is raw_error


@pytest.mark.asyncio
async def test_gspread_client_manager_does_not_retry_transport_errors_forever() -> None:
    manager = RhobotoGspreadClientManager(lambda: object(), gspread_delay=0)

    raw_error = RequestsConnectionError("private transport detail")

    with pytest.raises(RequestsConnectionError) as exc_info:
        await manager.handle_requests_error(raw_error, object(), (), {})

    assert exc_info.value is raw_error


@pytest.mark.asyncio
async def test_worksheet_read_write_raise_domain_errors_without_raw_details() -> None:
    raw_error = api_error(403, "PERMISSION_DENIED", text="secret spreadsheet url")
    worksheet = AsyncioGspreadWorksheet(RaisingWorksheet(raw_error))

    with pytest.raises(GoogleSheetsError) as read_error:
        await worksheet.to_frame()

    with pytest.raises(GoogleSheetsError) as write_error:
        await worksheet.update_from_dataframe(pd.DataFrame({"name": ["alice"]}))

    assert read_error.value.kind is GoogleSheetsErrorKind.PERMISSION
    assert write_error.value.kind is GoogleSheetsErrorKind.PERMISSION
    assert "secret spreadsheet url" not in read_error.value.user_message
    assert "secret spreadsheet url" not in write_error.value.user_message
