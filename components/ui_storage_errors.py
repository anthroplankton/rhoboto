from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from bot import config
from utils.reactions import transition_processing_reaction
from utils.storage_errors import (
    StorageError,
    StorageOperationContext,
    generate_error_reference,
    storage_error_content,
)

if TYPE_CHECKING:
    from types import TracebackType

    from discord import Interaction, Message
    from discord.abc import Snowflake


STORAGE_FAILURE_REACTION = config.WARNING_EMOJI
STORAGE_REPAIR_REACTION = "🛠️"
STORAGE_FAILURE_REACTIONS = (
    STORAGE_FAILURE_REACTION,
    STORAGE_REPAIR_REACTION,
)

logger = logging.getLogger(__name__)


async def send_storage_error(
    interaction: Interaction,
    error: StorageError,
    *,
    context: StorageOperationContext,
    reference_id: str | None = None,
    log: logging.Logger | None = None,
) -> None:
    reference = reference_id or generate_error_reference()
    active_logger = log or logger
    active_logger.warning(
        (
            "Storage action failed. reference=%s operation=%s feature=%s "
            "guild=%s channel=%s kind=%s hint=%s"
        ),
        reference,
        context.operation,
        context.feature_name,
        context.guild_id,
        context.channel_id,
        error.kind.value,
        error.log_hint,
        **_safe_storage_log_kwargs(error),
    )
    content = storage_error_content(error, reference_id=reference)
    try:
        if interaction.response.is_done():
            await interaction.followup.send(content, ephemeral=True)
        else:
            await interaction.response.send_message(content, ephemeral=True)
    except Exception:
        active_logger.exception(
            "Failed to deliver storage error response. reference=%s",
            reference,
        )


async def mark_storage_message_failure(  # noqa: PLR0913
    message: Message,
    bot_user: Snowflake | None,
    error: StorageError,
    *,
    context: StorageOperationContext,
    reference_id: str | None = None,
    log: logging.Logger | None = None,
) -> None:
    reference = reference_id or generate_error_reference()
    active_logger = log or logger
    message_id = context.message_id or getattr(message, "id", None)
    active_logger.warning(
        (
            "Storage message action failed. reference=%s operation=%s "
            "feature=%s guild=%s channel=%s message=%s kind=%s hint=%s"
        ),
        reference,
        context.operation,
        context.feature_name,
        context.guild_id,
        context.channel_id,
        message_id,
        error.kind.value,
        error.log_hint,
        **_safe_storage_log_kwargs(error),
    )
    await transition_processing_reaction(
        message,
        STORAGE_FAILURE_REACTIONS,
        processing_emoji=config.PROCESSING_EMOJI,
        user=bot_user,
        log=active_logger,
    )


def _safe_storage_exc_info(
    error: StorageError,
) -> tuple[type[BaseException], BaseException, TracebackType | None] | None:
    cause = error.__cause__
    if cause is None:
        return None
    names = []
    traceback = None
    while cause is not None:
        names.append(type(cause).__name__)
        if cause.__traceback__ is not None:
            traceback = cause.__traceback__
        cause = cause.__cause__
    safe_error = RuntimeError(f"Storage exception chain: {' -> '.join(names)}")
    return RuntimeError, safe_error, traceback


def _safe_storage_log_kwargs(error: StorageError) -> dict[str, object]:
    exc_info = _safe_storage_exc_info(error)
    return {} if exc_info is None else {"exc_info": exc_info}
