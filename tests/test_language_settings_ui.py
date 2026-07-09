from __future__ import annotations

from types import SimpleNamespace

import pytest

from components.ui_language_settings import (
    AnnouncementLanguageDraft,
    AnnouncementLanguageSettingsView,
    build_announcement_languages_embed,
)
from components.ui_settings_flow import (
    SETTINGS_VIEW_TIMEOUT_SECONDS,
    SettingsTimeoutView,
)
from tests.fakes import FakeInteraction


def test_language_draft_add_remove_and_reset() -> None:
    draft = AnnouncementLanguageDraft.from_saved(["ja"])

    assert draft.language_codes == ["ja"]
    assert draft.remaining_language_codes == ["zh_tw", "en"]

    draft.add_language("zh_tw")
    draft.add_language("ja")
    draft.add_language("unsupported")
    assert draft.language_codes == ["ja", "zh_tw"]
    assert draft.remaining_language_codes == ["en"]

    draft.remove_language("ja")
    assert draft.language_codes == ["zh_tw"]

    draft.reset()
    assert draft.language_codes == ["ja"]


def test_language_embed_shows_order_and_message_count_warning() -> None:
    embed = build_announcement_languages_embed(["ja", "zh_tw", "en"])

    assert embed.title == "Announcement Language Settings"
    assert "1. Japanese" in str(embed.description)
    assert "2. Traditional Chinese" in str(embed.description)
    assert "3. English" in str(embed.description)
    assert "Each selected language sends one public announcement message" in str(
        embed.description
    )


def test_announcement_language_view_uses_shared_settings_timeout() -> None:
    view = AnnouncementLanguageSettingsView(guild_id=111, language_codes=["ja"])

    assert isinstance(view, SettingsTimeoutView)
    assert view.timeout == SETTINGS_VIEW_TIMEOUT_SECONDS


def child_with_type(view: object, child_type: type[object]) -> object:
    return next(child for child in view.children if isinstance(child, child_type))


def child_with_label(view: object, label: str) -> object:
    return next(
        child for child in view.children if getattr(child, "label", None) == label
    )


@pytest.mark.asyncio
async def test_add_language_select_edits_message_with_appended_language() -> None:
    view = AnnouncementLanguageSettingsView(guild_id=111, language_codes=["ja"])
    add_select = next(
        child
        for child in view.children
        if getattr(child, "placeholder", None) == "Add Language"
    )
    add_select._values = ["zh_tw"]  # noqa: SLF001
    interaction = FakeInteraction()

    await add_select.callback(interaction)

    assert interaction.response.edits
    _, kwargs = interaction.response.edits[0]
    assert "Traditional Chinese" in str(kwargs["embed"].description)


@pytest.mark.asyncio
async def test_remove_language_select_blocks_removing_last_language() -> None:
    view = AnnouncementLanguageSettingsView(guild_id=111, language_codes=["ja"])
    remove_select = next(
        child
        for child in view.children
        if getattr(child, "placeholder", None) == "Remove Language"
    )
    remove_select._values = ["ja"]  # noqa: SLF001
    interaction = FakeInteraction()

    await remove_select.callback(interaction)

    assert interaction.response.messages == [
        ("At least one announcement language is required.", {"ephemeral": True})
    ]
    assert interaction.response.edits == []


@pytest.mark.asyncio
async def test_save_button_persists_draft_and_shows_saved_panel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    saved_calls: list[tuple[int, list[str]]] = []

    async def fake_save_announcement_languages(
        guild_id: int,
        values: list[str],
        _logger: object = None,
    ) -> SimpleNamespace:
        saved_calls.append((guild_id, values))
        return SimpleNamespace(guild_id=guild_id, announcement_languages=values)

    monkeypatch.setattr(
        "components.ui_language_settings.save_announcement_languages",
        fake_save_announcement_languages,
    )

    view = AnnouncementLanguageSettingsView(
        guild_id=111,
        language_codes=["ja", "zh_tw"],
    )
    button = child_with_label(view, "Save")
    interaction = FakeInteraction()

    await button.callback(interaction)

    assert saved_calls == [(111, ["ja", "zh_tw"])]
    _, kwargs = interaction.response.edits[0]
    assert kwargs["embed"].title == "Announcement Language Settings Saved"


class FakeMessage:
    def __init__(self) -> None:
        self.edits: list[tuple[tuple[object, ...], dict[str, object]]] = []

    async def edit(self, *args: object, **kwargs: object) -> None:
        self.edits.append((args, kwargs))


@pytest.mark.asyncio
async def test_add_interaction_preserves_rebuilt_view_message_handle_for_timeout() -> (
    None
):
    message = FakeMessage()
    view = AnnouncementLanguageSettingsView(guild_id=111, language_codes=["ja"])
    view.message = message
    add_select = next(
        child
        for child in view.children
        if getattr(child, "placeholder", None) == "Add Language"
    )
    add_select._values = ["zh_tw"]  # noqa: SLF001
    interaction = FakeInteraction()

    await add_select.callback(interaction)

    assert interaction.response.edits
    _, kwargs = interaction.response.edits[0]
    rebuilt_view = kwargs["view"]
    assert rebuilt_view.message is message

    await rebuilt_view.on_timeout()
    assert message.edits
    _, timeout_kwargs = message.edits[0]
    assert "Traditional Chinese" not in str(timeout_kwargs["embed"].description)
    assert "Japanese" in str(timeout_kwargs["embed"].description)
    assert timeout_kwargs["view"].message is message


@pytest.mark.asyncio
async def test_add_interaction_stops_old_view_before_replacing_view() -> None:
    view = AnnouncementLanguageSettingsView(guild_id=111, language_codes=["ja"])
    add_select = next(
        child
        for child in view.children
        if getattr(child, "placeholder", None) == "Add Language"
    )
    add_select._values = ["zh_tw"]  # noqa: SLF001
    interaction = FakeInteraction()

    await add_select.callback(interaction)

    assert view.is_finished()


@pytest.mark.asyncio
async def test_cancel_discards_draft_and_shows_saved_languages() -> None:
    view = AnnouncementLanguageSettingsView(guild_id=111, language_codes=["ja"])
    view.draft.add_language("en")
    button = child_with_label(view, "Cancel")
    interaction = FakeInteraction()

    await button.callback(interaction)

    _, kwargs = interaction.response.edits[0]
    assert "English" not in str(kwargs["embed"].description)
    assert "Japanese" in str(kwargs["embed"].description)


@pytest.mark.asyncio
async def test_timeout_discards_draft_and_shows_saved_languages() -> None:
    message = FakeMessage()
    view = AnnouncementLanguageSettingsView(guild_id=111, language_codes=["ja"])
    view.message = message
    view.draft.add_language("en")

    await view.on_timeout()

    assert message.edits
    _, kwargs = message.edits[0]
    assert "English" not in str(kwargs["embed"].description)
    assert "Japanese" in str(kwargs["embed"].description)
