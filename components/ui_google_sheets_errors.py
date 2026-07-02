from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from bot import config
from utils.reactions import add_reactions_if_possible, remove_reaction_if_present

if TYPE_CHECKING:
    from discord import Interaction, Message
    from discord.abc import Snowflake

    from utils.google_sheets_errors import GoogleSheetsError


GOOGLE_SHEETS_FAILURE_REACTION = "⚠️"
GOOGLE_SHEETS_REPAIR_REACTION = "🛠️"
GOOGLE_SHEETS_FAILURE_REACTIONS = (
    GOOGLE_SHEETS_FAILURE_REACTION,
    GOOGLE_SHEETS_REPAIR_REACTION,
)

logger = logging.getLogger(__name__)


def google_sheets_error_content(error: GoogleSheetsError) -> str:
    return f"Google Sheets could not complete this action. {error.user_message}"


async def send_google_sheets_error(
    interaction: Interaction,
    error: GoogleSheetsError,
    *,
    ephemeral: bool = True,
) -> None:
    content = google_sheets_error_content(error)
    if _interaction_response_done(interaction):
        await interaction.followup.send(content, ephemeral=ephemeral)
    else:
        await interaction.response.send_message(content, ephemeral=ephemeral)


async def mark_google_sheets_message_failure(
    message: Message,
    bot_user: Snowflake | None,
    error: GoogleSheetsError,
    log: logging.Logger | None = None,
) -> None:
    active_logger = log or logger
    active_logger.warning(
        "Google Sheets action failed for message %s with kind %s.",
        getattr(message, "id", "<unknown>"),
        error.kind.value,
    )
    if bot_user is not None:
        await remove_reaction_if_present(
            message,
            config.PROCESSING_EMOJI,
            bot_user,
            log=active_logger,
        )
    await add_reactions_if_possible(
        message,
        GOOGLE_SHEETS_FAILURE_REACTIONS,
        log=active_logger,
    )


def _interaction_response_done(interaction: Interaction) -> bool:
    is_done = getattr(interaction.response, "is_done", None)
    if callable(is_done):
        return bool(is_done())
    return any(
        bool(getattr(interaction.response, attr, None))
        for attr in ("deferred", "messages", "modals", "edits")
    )
