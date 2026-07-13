from __future__ import annotations

import logging
import secrets
from typing import TYPE_CHECKING

from bot import config

if TYPE_CHECKING:
    from discord import Interaction

    from utils.structs_base import WorksheetContractError


WORKSHEET_CONTRACT_FAILURE_REACTIONS = (config.WARNING_EMOJI, "📏")
WORKSHEET_CONTRACT_ERROR_CONTENT = (
    "⚠️📏 The configured Google Sheet layout needs correction. Reopen settings, "
    "verify the worksheets, and try again. Reference: `{reference_id}`"
)

logger = logging.getLogger(__name__)


async def send_worksheet_contract_error(
    interaction: Interaction,
    error: WorksheetContractError,
    *,
    operation: str,
    feature_name: str,
    log: logging.Logger | None = None,
) -> None:
    """Send safe worksheet-contract guidance without exposing worksheet values."""
    reference = f"WSC-{secrets.token_hex(4)}"
    active_logger = log or logger
    guild = getattr(interaction, "guild", None)
    channel = getattr(interaction, "channel", None)
    active_logger.warning(
        (
            "Worksheet contract action failed. reference=%s operation=%s "
            "feature=%s guild=%s channel=%s hint=%s"
        ),
        reference,
        operation,
        feature_name,
        getattr(guild, "id", None),
        getattr(channel, "id", None),
        error.log_hint,
    )
    content = WORKSHEET_CONTRACT_ERROR_CONTENT.format(reference_id=reference)
    try:
        if interaction.response.is_done():
            await interaction.followup.send(content, ephemeral=True)
        else:
            await interaction.response.send_message(content, ephemeral=True)
    except Exception:
        active_logger.exception(
            "Failed to deliver worksheet contract error response. reference=%s",
            reference,
        )
