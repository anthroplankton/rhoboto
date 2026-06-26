from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from discord import Interaction

MISSING_SETTINGS_PERMISSION_MESSAGE = "You do not have permission to use this action."


def has_settings_permissions(interaction: Interaction) -> bool:
    """Return whether the interaction user may mutate feature settings."""
    if interaction.guild is None:
        return False

    permissions = getattr(interaction.user, "guild_permissions", None)
    return bool(
        getattr(permissions, "administrator", False)
        and getattr(permissions, "manage_channels", False)
    )


async def require_settings_permissions(interaction: Interaction) -> bool:
    """Send an ephemeral denial message when a settings callback is unauthorized."""
    if has_settings_permissions(interaction):
        return True

    await interaction.response.send_message(
        MISSING_SETTINGS_PERMISSION_MESSAGE,
        ephemeral=True,
    )
    return False
