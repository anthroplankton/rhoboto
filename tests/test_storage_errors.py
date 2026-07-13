from __future__ import annotations

from google.auth.exceptions import DefaultCredentialsError
from tortoise.exceptions import ConfigurationError, DBConnectionError, IntegrityError

from utils.google_sheets_errors import GoogleSheetsError, GoogleSheetsErrorKind
from utils.storage_errors import (
    StorageError,
    StorageErrorKind,
    StorageOperationContext,
    classify_storage_exception,
    generate_error_reference,
    partial_success_storage_error,
    storage_error_content,
)


def test_generate_error_reference_uses_storage_prefix_and_short_random_suffix() -> None:
    reference = generate_error_reference()

    prefix, suffix = reference.split("-", maxsplit=1)
    assert prefix == "STG"
    assert len(suffix) == 8
    int(suffix, 16)


def test_classify_google_sheets_permission_uses_safe_access_copy() -> None:
    raw_error = GoogleSheetsError(
        GoogleSheetsErrorKind.PERMISSION,
        "Check the sheet sharing settings and service account access.",
        operation="open_spreadsheet",
    )

    error = classify_storage_exception(raw_error)

    assert isinstance(error, StorageError)
    assert error.kind is StorageErrorKind.GOOGLE_SHEETS_ACCESS
    assert error.__cause__ is raw_error
    content = storage_error_content(error, reference_id="STG-12345678")
    assert "configured Google Sheet" in content
    assert "sharing settings" in content
    assert "saved sheet link" in content
    assert "service account" not in content
    assert "credential" not in content.lower()
    assert "STG-12345678" in content


def test_classify_database_operational_failure_uses_generic_copy() -> None:
    raw_error = DBConnectionError("private database host")

    error = classify_storage_exception(raw_error)

    assert isinstance(error, StorageError)
    assert error.kind is StorageErrorKind.DATABASE_UNAVAILABLE
    content = storage_error_content(error, reference_id="STG-12345678")
    assert "could not complete this action" in content
    assert "database" not in content.lower()
    assert "private database host" not in content


def test_classify_integrity_failure_is_storage_write_failure() -> None:
    raw_error = IntegrityError("private constraint")

    error = classify_storage_exception(raw_error)

    assert isinstance(error, StorageError)
    assert error.kind is StorageErrorKind.DATABASE_WRITE


def test_other_orm_exception_is_not_storage_error() -> None:
    raw_error = ConfigurationError("private model bug")

    assert classify_storage_exception(raw_error) is None


def test_partial_success_storage_error_wraps_classified_cause() -> None:
    raw_error = DBConnectionError("private database host")

    error = partial_success_storage_error(raw_error)

    assert error is not None
    assert error.kind is StorageErrorKind.PARTIAL_SUCCESS
    assert isinstance(error.__cause__, StorageError)
    assert error.__cause__.kind is StorageErrorKind.DATABASE_UNAVAILABLE


def test_team_summary_draft_partial_error_discloses_completed_summary() -> None:
    error = StorageError(
        StorageErrorKind.PARTIAL_SUCCESS,
        log_hint="team_summary_refreshed_draft_incomplete",
    )

    content = storage_error_content(error, reference_id="STG-12345678")

    assert "Team Summary was refreshed" in content
    assert "Shift Draft was not completed" in content


def test_storage_operation_context_has_safe_defaults() -> None:
    context = StorageOperationContext(operation="settings_open")

    assert context.operation == "settings_open"
    assert context.feature_name is None
    assert context.guild_id is None
    assert context.channel_id is None
    assert context.message_id is None


def test_google_credential_failure_is_logged_hint_only() -> None:
    raw_error = DefaultCredentialsError("private credential path")
    google_error = GoogleSheetsError(
        GoogleSheetsErrorKind.PERMISSION,
        "Check the sheet sharing settings and service account access.",
        operation="load_credentials",
    )
    google_error.__cause__ = raw_error

    error = classify_storage_exception(google_error)

    assert error is not None
    assert error.log_hint == "credential_load_failed"
    assert (
        "credential"
        not in storage_error_content(
            error,
            reference_id="STG-12345678",
        ).lower()
    )
