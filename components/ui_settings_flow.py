from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from components.ui_google_sheets_errors import send_google_sheets_error
from utils.google_sheets_errors import GoogleSheetsError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from discord import Embed, Interaction
    from discord.ui import View

    from utils.manager_base import ManagerBase


@dataclass(frozen=True)
class SettingsPanel:
    embed: Embed
    view: View


def settings_title(feature_display_name: str, *, is_save_action: bool) -> str:
    suffix = "Settings Saved" if is_save_action else "Settings"
    return f"{feature_display_name} {suffix}"


def settings_description(
    feature_display_name: str,
    controls_description: str,
    *,
    is_save_action: bool,
) -> str:
    prefix = (
        f"Your {feature_display_name} settings were saved."
        if is_save_action
        else f"{feature_display_name} is configured for this channel."
    )
    return f"{prefix} {controls_description}"


def stale_setup_content(feature_display_name: str) -> str:
    return (
        f"{feature_display_name} is already configured for this channel. "
        "Here are the current settings."
    )


async def send_current_panel_followup(
    interaction: Interaction,
    panel: SettingsPanel,
    *,
    content: str | None = None,
) -> None:
    await interaction.followup.send(
        content=content,
        embed=panel.embed,
        view=panel.view,
        ephemeral=True,
    )


async def send_stale_setup_panel_if_configured(
    interaction: Interaction,
    manager: ManagerBase,
    *,
    feature_display_name: str,
    build_current_panel: Callable[[object], Awaitable[SettingsPanel]],
) -> bool:
    sheet_config = await manager.get_fresh_sheet_config()
    if sheet_config is None:
        return False

    await interaction.response.defer(ephemeral=True)
    try:
        panel = await build_current_panel(sheet_config)
    except GoogleSheetsError as exc:
        await send_google_sheets_error(interaction, exc)
        return True

    await send_current_panel_followup(
        interaction,
        panel,
        content=stale_setup_content(feature_display_name),
    )
    return True
