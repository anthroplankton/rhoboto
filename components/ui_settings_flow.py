from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from discord.ui import View

from components.ui_google_sheets_errors import send_google_sheets_error
from utils.google_sheets_errors import GoogleSheetsError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from discord import Embed, Interaction, Message

    from utils.manager_base import ManagerBase


SETTINGS_VIEW_TIMEOUT_SECONDS: Final[float] = 180.0


def disable_view_items(view: View) -> None:
    for item in view.children:
        item.disabled = True


class SettingsTimeoutView(View):
    """Base view for ephemeral settings panels that disable on timeout."""

    def __init__(self, *, timeout: float = SETTINGS_VIEW_TIMEOUT_SECONDS) -> None:
        super().__init__(timeout=timeout)
        self.message: Message | None = None

    def build_timeout_edit_kwargs(self) -> dict[str, object]:
        disable_view_items(self)
        return {"view": self}

    async def on_timeout(self) -> None:
        edit_kwargs = self.build_timeout_edit_kwargs()
        self.stop()
        if self.message is not None:
            await self.message.edit(**edit_kwargs)


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


def initial_setup_content(feature_display_name: str) -> str:
    return (
        f"{feature_display_name} is not yet configured for this channel. "
        "Click below to set up."
    )


def stale_setup_content(feature_display_name: str) -> str:
    return (
        f"{feature_display_name} is already configured for this channel. "
        "Here are the current settings."
    )


async def send_settings_view_followup(
    interaction: Interaction,
    *,
    view: View,
    content: str | None = None,
    embed: Embed | None = None,
) -> None:
    message = await interaction.followup.send(
        content=content,
        embed=embed,
        view=view,
        ephemeral=True,
        wait=True,
    )
    attach_settings_view_message(view, message)


async def send_current_panel_followup(
    interaction: Interaction,
    panel: SettingsPanel,
    *,
    content: str | None = None,
) -> None:
    await send_settings_view_followup(
        interaction,
        content=content,
        embed=panel.embed,
        view=panel.view,
    )


def attach_settings_view_message(view: View, message: Message) -> None:
    if isinstance(view, SettingsTimeoutView):
        view.message = message


def prepare_replacement_settings_view(
    current_view: View,
    replacement_view: View,
) -> View:
    if isinstance(current_view, SettingsTimeoutView):
        if current_view.message is not None:
            attach_settings_view_message(replacement_view, current_view.message)
        current_view.stop()
    return replacement_view


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
