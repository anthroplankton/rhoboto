from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

from models.guild_language_settings import GuildLanguageSettings
from utils.message_templates import (
    MessageTemplateNotFoundError,
    render_message_template,
)

if TYPE_CHECKING:
    import logging
    from collections.abc import Sequence


@dataclass(frozen=True)
class AnnouncementLanguage:
    code: str
    label: str


DEFAULT_ANNOUNCEMENT_LANGUAGES = ("ja",)
SUPPORTED_ANNOUNCEMENT_LANGUAGES = (
    AnnouncementLanguage("ja", "Japanese"),
    AnnouncementLanguage("zh_tw", "Traditional Chinese"),
    AnnouncementLanguage("en", "English"),
)
SUPPORTED_ANNOUNCEMENT_LANGUAGE_LABELS = {
    language.code: language.label for language in SUPPORTED_ANNOUNCEMENT_LANGUAGES
}
ANNOUNCEMENT_RENDER_FAILURE_MESSAGE = (
    "No announcement templates could be rendered for this server."
)


@dataclass(frozen=True)
class RenderedAnnouncement:
    language: str
    content: str


def normalize_announcement_languages(
    values: object,
    logger: logging.Logger | None = None,
) -> list[str]:
    normalized = _coerce_announcement_languages(values, logger)
    return normalized or list(DEFAULT_ANNOUNCEMENT_LANGUAGES)


def _coerce_announcement_languages(
    values: object,
    logger: logging.Logger | None = None,
) -> list[str]:
    if isinstance(values, str | bytes | Mapping) or not isinstance(values, Iterable):
        if logger is not None:
            logger.warning("Ignoring invalid announcement languages value: %r", values)
        return []

    supported_codes = set(SUPPORTED_ANNOUNCEMENT_LANGUAGE_LABELS)
    normalized: list[str] = []
    seen: set[str] = set()

    for value in values:
        if not isinstance(value, str):
            if logger is not None:
                logger.warning("Skipping non-string announcement language: %r", value)
            continue
        if value not in supported_codes:
            if logger is not None:
                logger.warning("Skipping unsupported announcement language: %s", value)
            continue
        if value in seen:
            continue
        normalized.append(value)
        seen.add(value)

    return normalized


async def get_announcement_languages(
    guild_id: int,
    logger: logging.Logger | None = None,
) -> list[str]:
    settings = await GuildLanguageSettings.get_or_none(guild_id=guild_id)
    if settings is None:
        return list(DEFAULT_ANNOUNCEMENT_LANGUAGES)
    return normalize_announcement_languages(settings.announcement_languages, logger)


async def save_announcement_languages(
    guild_id: int,
    values: Iterable[object],
    logger: logging.Logger | None = None,
) -> GuildLanguageSettings:
    languages = normalize_announcement_languages(values, logger)
    settings, _ = await GuildLanguageSettings.update_or_create(
        guild_id=guild_id,
        defaults={"announcement_languages": languages},
    )
    return settings


def render_announcement_messages_for_languages(
    template_key: str,
    languages: Sequence[str],
    logger: logging.Logger | None = None,
    **values: object,
) -> list[RenderedAnnouncement]:
    rendered: list[RenderedAnnouncement] = []
    for language in _coerce_announcement_languages(languages, logger):
        try:
            content = render_message_template(template_key, language, **values)
        except MessageTemplateNotFoundError:
            if logger is not None:
                logger.warning(
                    "Missing announcement template `%s` for language `%s`.",
                    template_key,
                    language,
                )
            continue
        rendered.append(RenderedAnnouncement(language=language, content=content))
    return rendered


async def render_announcement_messages(
    template_key: str,
    guild_id: int,
    logger: logging.Logger | None = None,
    **values: object,
) -> list[RenderedAnnouncement]:
    languages = await get_announcement_languages(guild_id, logger)
    return render_announcement_messages_for_languages(
        template_key,
        languages,
        logger,
        **values,
    )
