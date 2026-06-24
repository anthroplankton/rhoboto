from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from discord import HTTPException

if TYPE_CHECKING:
    from discord import Message
    from discord.abc import Snowflake

logger = logging.getLogger(__name__)


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
