from __future__ import annotations

import pytest
from discord import Embed
from discord.ui import Button, View

from components.ui_settings_flow import (
    SETTINGS_VIEW_TIMEOUT_SECONDS,
    SettingsPanel,
    SettingsTimeoutView,
    attach_settings_view_message,
    initial_setup_content,
    prepare_replacement_settings_view,
    send_current_panel_followup,
    send_settings_view_followup,
    settings_description,
    settings_title,
    stale_setup_content,
)
from tests.fakes import FakeInteraction


class FakeMessage:
    def __init__(self) -> None:
        self.edits: list[tuple[tuple[object, ...], dict[str, object]]] = []

    async def edit(self, *args: object, **kwargs: object) -> None:
        self.edits.append((args, kwargs))


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


def test_initial_setup_content_prompts_user_to_set_up() -> None:
    assert initial_setup_content("Team Register") == (
        "Team Register is not yet configured for this channel. Click below to set up."
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


def test_settings_timeout_view_uses_shared_timeout() -> None:
    view = SettingsTimeoutView()

    assert view.timeout == SETTINGS_VIEW_TIMEOUT_SECONDS
    assert SETTINGS_VIEW_TIMEOUT_SECONDS == 180.0


@pytest.mark.asyncio
async def test_settings_timeout_view_disables_children_and_edits_message() -> None:
    view = SettingsTimeoutView()
    view.add_item(Button(label="Edit"))
    message = FakeMessage()
    attach_settings_view_message(view, message)

    await view.on_timeout()

    assert view.is_finished()
    assert all(child.disabled for child in view.children)
    assert message.edits == [((), {"view": view})]


@pytest.mark.asyncio
async def test_prepare_replacement_view_stops_old_and_transfers_message() -> None:
    current_view = SettingsTimeoutView()
    replacement_view = SettingsTimeoutView()
    message = FakeMessage()
    attach_settings_view_message(current_view, message)

    prepared = prepare_replacement_settings_view(current_view, replacement_view)

    assert prepared is replacement_view
    assert current_view.is_finished()
    assert replacement_view.message is message


@pytest.mark.asyncio
async def test_send_settings_view_followup_attaches_message_to_timeout_view() -> None:
    interaction = FakeInteraction()
    view = SettingsTimeoutView()

    await send_settings_view_followup(
        interaction,
        content="Setup Team Register",
        view=view,
    )

    content, kwargs = interaction.followup.messages[0]
    assert content == "Setup Team Register"
    assert kwargs["embed"] is None
    assert kwargs["ephemeral"] is True
    assert kwargs["wait"] is True
    assert kwargs["view"] is view
    assert view.message is interaction.followup.sent_message_objects[0]


@pytest.mark.asyncio
async def test_send_current_panel_followup_attaches_message_to_timeout_view() -> None:
    interaction = FakeInteraction()
    view = SettingsTimeoutView()
    panel = SettingsPanel(embed=Embed(title="Team Register Settings"), view=view)

    await send_current_panel_followup(interaction, panel)

    assert interaction.followup.messages[0][1]["wait"] is True
    assert view.message is interaction.followup.sent_message_objects[0]
