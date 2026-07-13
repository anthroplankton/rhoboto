from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from discord import HTTPException

if TYPE_CHECKING:
    from collections.abc import Iterable

    from discord import Message
    from discord.abc import Snowflake

logger = logging.getLogger(__name__)


async def add_reaction_if_possible(
    message: Message,
    emoji: str,
    *,
    log: logging.Logger | None = None,
) -> None:
    """Add a reaction while tolerating Discord API reaction failures."""
    try:
        await message.add_reaction(emoji)
    except HTTPException as exc:
        active_logger = log or logger
        active_logger.debug(
            "Skipped adding reaction %r to message %s: %s",
            emoji,
            getattr(message, "id", "<unknown>"),
            exc,
        )


async def add_reactions_if_possible(
    message: Message,
    emojis: Iterable[str],
    *,
    log: logging.Logger | None = None,
) -> None:
    """Add reactions in order while tolerating Discord API reaction failures."""
    for emoji in emojis:
        await add_reaction_if_possible(message, emoji, log=log)


async def transition_processing_reaction(
    message: Message,
    terminal_emojis: Iterable[str],
    *,
    processing_emoji: str,
    user: Snowflake | None,
    log: logging.Logger | None = None,
) -> None:
    """Add terminal reactions before removing the processing reaction."""
    active_logger = log or logger
    for emoji in terminal_emojis:
        try:
            await add_reaction_if_possible(message, emoji, log=active_logger)
        except Exception:
            active_logger.exception(
                "Failed to deliver terminal reaction %r to message %s.",
                emoji,
                getattr(message, "id", "<unknown>"),
            )
    if user is None:
        return
    try:
        await remove_reaction_if_present(
            message,
            processing_emoji,
            user,
            log=active_logger,
        )
    except Exception:
        active_logger.exception(
            "Failed to remove processing reaction from message %s.",
            getattr(message, "id", "<unknown>"),
        )


async def remove_reaction_if_present(
    message: Message,
    emoji: str,
    user: Snowflake,
    *,
    log: logging.Logger | None = None,
) -> None:
    """Remove a bot reaction while tolerating missing or inaccessible reactions."""
    try:
        await message.remove_reaction(emoji, user)
    except HTTPException as exc:
        active_logger = log or logger
        active_logger.debug(
            "Skipped removing reaction %r from message %s: %s",
            emoji,
            getattr(message, "id", "<unknown>"),
            exc,
        )
