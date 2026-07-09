from __future__ import annotations

import secrets
from dataclasses import dataclass
from enum import StrEnum

from google.auth.exceptions import DefaultCredentialsError, RefreshError
from tortoise.exceptions import DBConnectionError, IntegrityError, OperationalError

from utils.google_sheets_errors import GoogleSheetsError, GoogleSheetsErrorKind


class StorageErrorKind(StrEnum):
    DATABASE_UNAVAILABLE = "database_unavailable"
    DATABASE_WRITE = "database_write"
    GOOGLE_SHEETS_ACCESS = "google_sheets_access"
    GOOGLE_SHEETS_QUOTA = "google_sheets_quota"
    GOOGLE_SHEETS_INVALID_URL = "google_sheets_invalid_url"
    GOOGLE_SHEETS_MISSING_WORKSHEET = "google_sheets_missing_worksheet"
    GOOGLE_SHEETS_TRANSIENT = "google_sheets_transient"
    GOOGLE_SHEETS_UNKNOWN = "google_sheets_unknown"
    MALFORMED_SHEET = "malformed_sheet"
    PARTIAL_SUCCESS = "partial_success"


@dataclass(frozen=True)
class StorageOperationContext:
    operation: str
    feature_name: str | None = None
    guild_id: int | None = None
    channel_id: int | None = None
    message_id: int | None = None


class StorageError(Exception):
    def __init__(
        self,
        kind: StorageErrorKind,
        *,
        log_hint: str | None = None,
    ) -> None:
        super().__init__(kind.value)
        self.kind = kind
        self.log_hint = log_hint


def generate_error_reference() -> str:
    return f"STG-{secrets.token_hex(4)}"


def classify_storage_exception(exc: Exception) -> StorageError | None:
    if isinstance(exc, StorageError):
        return exc
    if isinstance(exc, GoogleSheetsError):
        error = StorageError(
            _google_sheets_kind(exc.kind),
            log_hint=_google_sheets_log_hint(exc),
        )
        error.__cause__ = exc
        return error
    if isinstance(exc, IntegrityError):
        error = StorageError(StorageErrorKind.DATABASE_WRITE)
        error.__cause__ = exc
        return error
    if isinstance(exc, (DBConnectionError, OperationalError, TimeoutError)):
        error = StorageError(StorageErrorKind.DATABASE_UNAVAILABLE)
        error.__cause__ = exc
        return error
    return None


def partial_success_storage_error(exc: Exception) -> StorageError | None:
    classified_error = classify_storage_exception(exc)
    if classified_error is None:
        return None
    error = StorageError(
        StorageErrorKind.PARTIAL_SUCCESS,
        log_hint=classified_error.log_hint,
    )
    error.__cause__ = classified_error
    return error


def storage_error_content(error: StorageError, *, reference_id: str) -> str:
    template = _STORAGE_ERROR_CONTENT.get(error.kind, _GENERIC_STORAGE_ERROR_CONTENT)
    return template.format(reference_id=reference_id)


def _google_sheets_kind(kind: GoogleSheetsErrorKind) -> StorageErrorKind:
    return {
        GoogleSheetsErrorKind.PERMISSION: StorageErrorKind.GOOGLE_SHEETS_ACCESS,
        GoogleSheetsErrorKind.QUOTA: StorageErrorKind.GOOGLE_SHEETS_QUOTA,
        GoogleSheetsErrorKind.INVALID_URL: StorageErrorKind.GOOGLE_SHEETS_INVALID_URL,
        GoogleSheetsErrorKind.MISSING_WORKSHEET: (
            StorageErrorKind.GOOGLE_SHEETS_MISSING_WORKSHEET
        ),
        GoogleSheetsErrorKind.TRANSIENT: StorageErrorKind.GOOGLE_SHEETS_TRANSIENT,
        GoogleSheetsErrorKind.UNKNOWN: StorageErrorKind.GOOGLE_SHEETS_UNKNOWN,
    }[kind]


def _google_sheets_log_hint(error: GoogleSheetsError) -> str | None:
    cause = error.__cause__
    if isinstance(cause, (DefaultCredentialsError, RefreshError)):
        return "credential_load_failed"
    if error.kind is GoogleSheetsErrorKind.PERMISSION:
        return "google_api_permission"
    return None


_GENERIC_STORAGE_ERROR_CONTENT = (
    "The bot could not complete this action right now. Try again later or "
    "contact the bot maintainer. Reference: `{reference_id}`"
)

_STORAGE_ERROR_CONTENT = {
    StorageErrorKind.GOOGLE_SHEETS_ACCESS: (
        "The bot cannot access the configured Google Sheet. Check the sheet "
        "sharing settings and saved sheet link. If it still fails, contact "
        "the bot maintainer. Reference: `{reference_id}`"
    ),
    StorageErrorKind.GOOGLE_SHEETS_QUOTA: (
        "Google Sheets is rate-limiting requests. Try again later. "
        "Reference: `{reference_id}`"
    ),
    StorageErrorKind.GOOGLE_SHEETS_INVALID_URL: (
        "Check the Google Sheet link and save the settings again. "
        "Reference: `{reference_id}`"
    ),
    StorageErrorKind.GOOGLE_SHEETS_MISSING_WORKSHEET: (
        "A configured worksheet could not be found. Reopen settings and save "
        "the worksheet configuration. Reference: `{reference_id}`"
    ),
    StorageErrorKind.GOOGLE_SHEETS_TRANSIENT: (
        "Google Sheets is temporarily unavailable. Try again later. "
        "Reference: `{reference_id}`"
    ),
    StorageErrorKind.MALFORMED_SHEET: (
        "The configured Google Sheet could not be processed. Reopen settings "
        "and verify the worksheet configuration. Reference: `{reference_id}`"
    ),
    StorageErrorKind.PARTIAL_SUCCESS: (
        "Some changes may have been saved, but this action could not be completed. "
        "Reopen settings and verify before retrying. Reference: `{reference_id}`"
    ),
}
