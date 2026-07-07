from __future__ import annotations

import asyncio

import pytest

from models.guild_language_settings import GuildLanguageSettings
from utils.announcement_languages import (
    DEFAULT_ANNOUNCEMENT_LANGUAGES,
    SUPPORTED_ANNOUNCEMENT_LANGUAGE_LABELS,
    RenderedAnnouncement,
    get_announcement_languages,
    normalize_announcement_languages,
    render_announcement_messages_for_languages,
    save_announcement_languages,
)
from utils.db import close_db, init_db
from utils.message_templates import MessageTemplateNotFoundError


@pytest.mark.parametrize(
    ("raw_values", "expected"),
    [
        (["ja", "zh_tw", "en"], ["ja", "zh_tw", "en"]),
        (["ja", "ja", "en"], ["ja", "en"]),
        (["unsupported", "zh_tw"], ["zh_tw"]),
        ([], ["ja"]),
        ([None, 123, "unsupported"], ["ja"]),
        (None, ["ja"]),
        (123, ["ja"]),
    ],
)
def test_normalize_announcement_languages(
    raw_values: object,
    expected: list[str],
) -> None:
    assert normalize_announcement_languages(raw_values) == expected


def test_supported_language_labels_are_complete() -> None:
    assert DEFAULT_ANNOUNCEMENT_LANGUAGES == ("ja",)
    assert SUPPORTED_ANNOUNCEMENT_LANGUAGE_LABELS == {
        "ja": "Japanese",
        "zh_tw": "Traditional Chinese",
        "en": "English",
    }


@pytest.mark.asyncio
async def test_get_announcement_languages_defaults_without_row() -> None:
    db_url = "sqlite://:memory:"
    await asyncio.wait_for(init_db(db_url), timeout=3)
    try:
        assert await get_announcement_languages(1001) == ["ja"]
    finally:
        await asyncio.wait_for(close_db(db_url), timeout=3)


@pytest.mark.asyncio
async def test_save_and_get_announcement_languages_preserves_order() -> None:
    db_url = "sqlite://:memory:"
    await asyncio.wait_for(init_db(db_url), timeout=3)
    try:
        saved = await save_announcement_languages(1001, ["zh_tw", "ja", "en"])
        fetched = await GuildLanguageSettings.get(guild_id=1001)

        assert saved.id == fetched.id
        assert fetched.announcement_languages == ["zh_tw", "ja", "en"]
        assert await get_announcement_languages(1001) == ["zh_tw", "ja", "en"]
    finally:
        await asyncio.wait_for(close_db(db_url), timeout=3)


def test_render_announcement_messages_for_languages_preserves_order() -> None:
    messages = render_announcement_messages_for_languages(
        "team.guide",
        ["ja", "zh_tw", "en"],
        bot="@Rhoboto",
        sheet_url="https://sheet.example",
    )

    assert [message.language for message in messages] == ["ja", "zh_tw", "en"]
    assert all("@Rhoboto" in message.content for message in messages)
    assert all("https://sheet.example" in message.content for message in messages)


def test_render_announcement_messages_for_languages_skips_missing_template(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_render_message_template(
        key: str,
        locale: str,
        **values: object,
    ) -> str:
        if locale == "zh_tw":
            raise MessageTemplateNotFoundError(key, locale, None)
        return f"{locale}:{values['bot']}"

    monkeypatch.setattr(
        "utils.announcement_languages.render_message_template",
        fake_render_message_template,
    )

    messages = render_announcement_messages_for_languages(
        "team.guide",
        ["ja", "zh_tw", "en"],
        bot="@Rhoboto",
        sheet_url="https://sheet.example",
    )

    assert messages == [
        RenderedAnnouncement(language="ja", content="ja:@Rhoboto"),
        RenderedAnnouncement(language="en", content="en:@Rhoboto"),
    ]


def test_render_announcement_messages_for_languages_returns_empty_when_all_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_render_message_template(
        key: str,
        locale: str,
        **_: object,
    ) -> str:
        raise MessageTemplateNotFoundError(key, locale, None)

    monkeypatch.setattr(
        "utils.announcement_languages.render_message_template",
        fake_render_message_template,
    )

    assert (
        render_announcement_messages_for_languages(
            "team.guide",
            ["ja", "en"],
            bot="@Rhoboto",
            sheet_url="https://sheet.example",
        )
        == []
    )


@pytest.mark.parametrize("languages", [[], ["unsupported"], ["unsupported", 123]])
def test_render_announcement_messages_for_languages_does_not_fallback(
    languages: list[object],
) -> None:
    assert (
        render_announcement_messages_for_languages(
            "team.guide",
            languages,
            bot="@Rhoboto",
            sheet_url="https://sheet.example",
        )
        == []
    )
