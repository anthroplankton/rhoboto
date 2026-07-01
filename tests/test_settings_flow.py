from __future__ import annotations

from discord import Embed
from discord.ui import View

from components.ui_settings_flow import (
    SettingsPanel,
    settings_description,
    settings_title,
    stale_setup_content,
)


def test_settings_title_uses_current_and_saved_forms() -> None:
    assert (
        settings_title("Team Register", is_save_action=False)
        == "Team Register Settings"
    )
    assert (
        settings_title("Team Register", is_save_action=True)
        == "Team Register Settings Saved"
    )


def test_settings_description_uses_current_and_saved_forms() -> None:
    controls = "Use the buttons below to update sheet settings or Encore roles."

    assert settings_description(
        "Team Register",
        controls,
        is_save_action=False,
    ) == (
        "Team Register is configured for this channel. "
        "Use the buttons below to update sheet settings or Encore roles."
    )
    assert settings_description(
        "Team Register",
        "Use the buttons below to edit sheet settings or Encore roles.",
        is_save_action=True,
    ) == (
        "Your Team Register settings were saved. "
        "Use the buttons below to edit sheet settings or Encore roles."
    )


def test_stale_setup_content_is_neutral() -> None:
    assert stale_setup_content("Shift Register") == (
        "Shift Register is already configured for this channel. "
        "Here are the current settings."
    )


def test_settings_panel_holds_embed_and_view() -> None:
    embed = Embed(title="Team Register Settings")
    view = View()

    panel = SettingsPanel(embed=embed, view=view)

    assert panel.embed is embed
    assert panel.view is view
