from __future__ import annotations

import logging

import pytest
from tortoise.exceptions import DBConnectionError

from bot import config
from components.ui_storage_errors import (
    STORAGE_REPAIR_REACTION,
    mark_storage_message_failure,
    send_storage_error,
)
from tests.fakes import FakeInteraction
from utils.storage_errors import (
    StorageError,
    StorageErrorKind,
    StorageOperationContext,
)


class FakeMessage:
    id = 333

    def __init__(self) -> None:
        self.added_reactions: list[str] = []
        self.removed_reactions: list[tuple[str, object]] = []

    async def add_reaction(self, emoji: str) -> None:
        self.added_reactions.append(emoji)

    async def remove_reaction(self, emoji: str, user: object) -> None:
        self.removed_reactions.append((emoji, user))


class RaisingFollowup:
    async def send(self, *_: object, **__: object) -> None:
        message = "secondary delivery failed"
        raise RuntimeError(message)


class RecordingLogger:
    def __init__(self) -> None:
        self.warnings: list[tuple[tuple[object, ...], dict[str, object]]] = []
        self.exceptions: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def warning(self, *args: object, **kwargs: object) -> None:
        self.warnings.append((args, kwargs))

    def exception(self, *args: object, **kwargs: object) -> None:
        self.exceptions.append((args, kwargs))


def chained_database_storage_error() -> StorageError:
    error = StorageError(
        StorageErrorKind.DATABASE_UNAVAILABLE,
        log_hint="database_connection_failed",
    )
    error.__cause__ = DBConnectionError("private database host")
    return error


@pytest.mark.asyncio
async def test_send_storage_error_before_defer_uses_initial_response() -> None:
    interaction = FakeInteraction()
    context = StorageOperationContext(operation="settings_save")
    error = StorageError(StorageErrorKind.DATABASE_UNAVAILABLE)

    await send_storage_error(
        interaction,
        error,
        context=context,
        reference_id="STG-12345678",
        log=RecordingLogger(),
    )

    assert interaction.response.messages == [
        (
            "The bot could not complete this action right now. Try again later "
            "or contact the bot maintainer. Reference: `STG-12345678`",
            {"ephemeral": True},
        )
    ]
    assert interaction.followup.messages == []


@pytest.mark.asyncio
async def test_send_storage_error_logs_safe_storage_fields(
    caplog: pytest.LogCaptureFixture,
) -> None:
    interaction = FakeInteraction()
    context = StorageOperationContext(
        operation="settings_save",
        feature_name="team_register",
        guild_id=111,
        channel_id=222,
    )
    log = logging.getLogger("tests.ui_storage_errors.send")
    caplog.set_level(logging.WARNING, logger=log.name)

    await send_storage_error(
        interaction,
        chained_database_storage_error(),
        context=context,
        reference_id="STG-12345678",
        log=log,
    )

    assert "STG-12345678" in caplog.text
    assert "database_unavailable" in caplog.text
    assert "database_connection_failed" in caplog.text
    assert "DBConnectionError" in caplog.text
    assert "private database host" not in caplog.text


@pytest.mark.asyncio
async def test_send_storage_error_logs_initial_response_delivery_failure() -> None:
    interaction = FakeInteraction()
    context = StorageOperationContext(operation="settings_save")
    error = StorageError(StorageErrorKind.DATABASE_UNAVAILABLE)
    log = RecordingLogger()

    async def raise_send_message(*_: object, **__: object) -> None:
        message = "initial delivery failed"
        raise RuntimeError(message)

    interaction.response.send_message = raise_send_message

    await send_storage_error(
        interaction,
        error,
        context=context,
        reference_id="STG-12345678",
        log=log,
    )

    assert len(log.exceptions) == 1
    assert "STG-12345678" in str(log.exceptions[0][0])


@pytest.mark.asyncio
async def test_send_storage_error_after_defer_uses_followup_safe_google_copy() -> None:
    interaction = FakeInteraction()
    await interaction.response.defer(ephemeral=True)
    context = StorageOperationContext(operation="settings_open")
    error = StorageError(StorageErrorKind.GOOGLE_SHEETS_ACCESS)

    await send_storage_error(
        interaction,
        error,
        context=context,
        reference_id="STG-12345678",
        log=RecordingLogger(),
    )

    assert interaction.response.messages == []
    assert interaction.followup.messages == [
        (
            "The bot cannot access the configured Google Sheet. Check the sheet "
            "sharing settings and saved sheet link. If it still fails, contact "
            "the bot maintainer. Reference: `STG-12345678`",
            {"ephemeral": True},
        )
    ]


@pytest.mark.asyncio
async def test_send_storage_error_logs_secondary_delivery_failure() -> None:
    interaction = FakeInteraction()
    await interaction.response.defer(ephemeral=True)
    interaction.followup = RaisingFollowup()
    context = StorageOperationContext(operation="settings_save")
    error = StorageError(StorageErrorKind.DATABASE_UNAVAILABLE)
    log = RecordingLogger()

    await send_storage_error(
        interaction,
        error,
        context=context,
        reference_id="STG-12345678",
        log=log,
    )

    assert len(log.exceptions) == 1
    assert "STG-12345678" in str(log.exceptions[0][0])


@pytest.mark.asyncio
async def test_mark_storage_failure_updates_reactions_and_logs_warning() -> None:
    message = FakeMessage()
    bot_user = object()
    context = StorageOperationContext(
        operation="shift_register_update",
        feature_name="shift_register",
        guild_id=111,
        channel_id=222,
    )
    error = StorageError(
        StorageErrorKind.GOOGLE_SHEETS_ACCESS,
        log_hint="google_api_permission",
    )
    log = RecordingLogger()

    await mark_storage_message_failure(
        message,
        bot_user,
        error,
        context=context,
        reference_id="STG-12345678",
        log=log,
    )

    assert message.removed_reactions == [(config.PROCESSING_EMOJI, bot_user)]
    assert message.added_reactions == [
        config.WARNING_EMOJI,
        STORAGE_REPAIR_REACTION,
    ]
    assert len(log.warnings) == 1
    assert "exc_info" not in log.warnings[0][1]


@pytest.mark.asyncio
async def test_mark_storage_failure_logs_safe_storage_fields(
    caplog: pytest.LogCaptureFixture,
) -> None:
    message = FakeMessage()
    context = StorageOperationContext(
        operation="shift_register_update",
        feature_name="shift_register",
        guild_id=111,
        channel_id=222,
        message_id=333,
    )
    log = logging.getLogger("tests.ui_storage_errors.mark")
    caplog.set_level(logging.WARNING, logger=log.name)

    await mark_storage_message_failure(
        message,
        None,
        chained_database_storage_error(),
        context=context,
        reference_id="STG-12345678",
        log=log,
    )

    assert "STG-12345678" in caplog.text
    assert "database_unavailable" in caplog.text
    assert "database_connection_failed" in caplog.text
    assert "DBConnectionError" in caplog.text
    assert "private database host" not in caplog.text
