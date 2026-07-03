from __future__ import annotations

from types import SimpleNamespace

import pytest

from cogs.language import Language
from components.ui_language_settings import AnnouncementLanguageSettingsView
from components.ui_permissions import MISSING_SETTINGS_PERMISSION_MESSAGE
from tests.fakes import FakeInteraction


@pytest.mark.asyncio
async def test_language_settings_announcement_denies_unauthorized_user() -> None:
    subject = SimpleNamespace(logger=SimpleNamespace())
    interaction = FakeInteraction(manage_channels=False)

    await Language.announcement.callback(subject, interaction)

    assert interaction.response.messages == [
        (MISSING_SETTINGS_PERMISSION_MESSAGE, {"ephemeral": True})
    ]
    assert interaction.response.deferred == []


@pytest.mark.asyncio
async def test_language_settings_announcement_sends_ephemeral_panel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_get_announcement_languages(
        guild_id: int,
        _logger: object = None,
    ) -> list[str]:
        assert guild_id == 111
        return ["ja", "zh_tw"]

    monkeypatch.setattr(
        "cogs.language.get_announcement_languages",
        fake_get_announcement_languages,
    )

    subject = SimpleNamespace(logger=SimpleNamespace())
    interaction = FakeInteraction()

    await Language.announcement.callback(subject, interaction)

    assert interaction.response.deferred == [True]
    content, kwargs = interaction.followup.messages[0]
    assert content is None
    assert kwargs["ephemeral"] is True
    assert "Japanese" in str(kwargs["embed"].description)
    assert "Traditional Chinese" in str(kwargs["embed"].description)
    assert isinstance(kwargs["view"], AnnouncementLanguageSettingsView)
