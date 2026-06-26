from __future__ import annotations

from enum import StrEnum

from google.auth.exceptions import (
    DefaultCredentialsError,
    GoogleAuthError,
    RefreshError,
    TransportError,
)
from gspread.exceptions import (
    APIError,
    GSpreadException,
    NoValidUrlKeyFound,
    SpreadsheetNotFound,
    WorksheetNotFound,
)
from requests.exceptions import RequestException


class GoogleSheetsErrorKind(StrEnum):
    PERMISSION = "permission"
    QUOTA = "quota"
    INVALID_URL = "invalid_url"
    MISSING_WORKSHEET = "missing_worksheet"
    TRANSIENT = "transient"
    UNKNOWN = "unknown"


class GoogleSheetsError(Exception):
    """Domain error for safe Google Sheets failures."""

    def __init__(
        self,
        kind: GoogleSheetsErrorKind,
        user_message: str,
        *,
        operation: str | None = None,
    ) -> None:
        super().__init__(user_message)
        self.kind = kind
        self.user_message = user_message
        self.operation = operation


GOOGLE_SHEETS_EXTERNAL_EXCEPTIONS = (
    GSpreadException,
    GoogleAuthError,
    RequestException,
    TimeoutError,
    ConnectionError,
    OSError,
    ValueError,
)

_SAFE_MESSAGES = {
    GoogleSheetsErrorKind.PERMISSION: (
        "Check the sheet sharing settings and service account access."
    ),
    GoogleSheetsErrorKind.QUOTA: (
        "Google Sheets is rate-limiting requests. Try again later."
    ),
    GoogleSheetsErrorKind.INVALID_URL: (
        "Check the Google Sheet link and save the settings again."
    ),
    GoogleSheetsErrorKind.MISSING_WORKSHEET: (
        "A configured worksheet could not be found. Reopen settings and save "
        "the worksheet configuration."
    ),
    GoogleSheetsErrorKind.TRANSIENT: (
        "Google Sheets is temporarily unavailable. Try again later."
    ),
    GoogleSheetsErrorKind.UNKNOWN: ("Check the sheet settings and try again."),
}

_PERMISSION_STATUSES = {"PERMISSION_DENIED", "UNAUTHENTICATED"}
_QUOTA_STATUSES = {"RESOURCE_EXHAUSTED", "RATE_LIMIT_EXCEEDED"}
_MISSING_STATUSES = {"NOT_FOUND"}
_TRANSIENT_STATUSES = {
    "ABORTED",
    "CANCELLED",
    "DEADLINE_EXCEEDED",
    "INTERNAL",
    "UNAVAILABLE",
}
HTTP_UNAUTHORIZED = 401
HTTP_FORBIDDEN = 403
HTTP_NOT_FOUND = 404
HTTP_TOO_MANY_REQUESTS = 429
TRANSIENT_STATUS_CODES = {408, 500, 502, 503, 504}


def classify_google_sheets_exception(
    exc: Exception, *, operation: str | None = None
) -> GoogleSheetsError:
    if isinstance(exc, GoogleSheetsError):
        return exc

    kind = _classify_kind(exc)
    error = GoogleSheetsError(kind, _SAFE_MESSAGES[kind], operation=operation)
    error.__cause__ = exc
    return error


def _classify_kind(exc: Exception) -> GoogleSheetsErrorKind:
    kind = GoogleSheetsErrorKind.UNKNOWN
    if isinstance(exc, WorksheetNotFound):
        kind = GoogleSheetsErrorKind.MISSING_WORKSHEET
    elif isinstance(exc, (SpreadsheetNotFound, NoValidUrlKeyFound)):
        kind = GoogleSheetsErrorKind.INVALID_URL
    elif isinstance(exc, (DefaultCredentialsError, RefreshError, PermissionError)):
        kind = GoogleSheetsErrorKind.PERMISSION
    elif isinstance(exc, TransportError):
        kind = GoogleSheetsErrorKind.TRANSIENT
    elif isinstance(exc, APIError):
        kind = _classify_api_error(exc)
    elif isinstance(exc, GoogleAuthError):
        kind = GoogleSheetsErrorKind.PERMISSION
    elif isinstance(exc, (RequestException, TimeoutError, ConnectionError)):
        kind = GoogleSheetsErrorKind.TRANSIENT
    return kind


def _classify_api_error(exc: APIError) -> GoogleSheetsErrorKind:
    status_code = _api_status_code(exc)
    status = _api_status(exc)

    if (
        status_code in {HTTP_UNAUTHORIZED, HTTP_FORBIDDEN}
        or status in _PERMISSION_STATUSES
    ):
        return GoogleSheetsErrorKind.PERMISSION
    if status_code == HTTP_TOO_MANY_REQUESTS or status in _QUOTA_STATUSES:
        return GoogleSheetsErrorKind.QUOTA
    if status_code == HTTP_NOT_FOUND or status in _MISSING_STATUSES:
        return GoogleSheetsErrorKind.MISSING_WORKSHEET
    if status_code in TRANSIENT_STATUS_CODES or status in _TRANSIENT_STATUSES:
        return GoogleSheetsErrorKind.TRANSIENT
    return GoogleSheetsErrorKind.UNKNOWN


def _api_status_code(exc: APIError) -> int | None:
    response_status = getattr(getattr(exc, "response", None), "status_code", None)
    if isinstance(response_status, int):
        return response_status

    payload = exc.args[0] if exc.args else None
    if isinstance(payload, dict):
        code = payload.get("code")
        if isinstance(code, int):
            return code
    return None


def _api_status(exc: APIError) -> str:
    payload = exc.args[0] if exc.args else None
    if not isinstance(payload, dict):
        return ""
    status = payload.get("status")
    return status.upper() if isinstance(status, str) else ""
