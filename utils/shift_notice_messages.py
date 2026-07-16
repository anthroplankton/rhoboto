from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

import discord
from discord.utils import escape_markdown, escape_mentions

from bot import config as bot_config
from utils.message_templates import (
    MessageTemplateNotFoundError,
    render_message_template,
)
from utils.shift_notice import (
    ShiftNoticeFrameState,
    ShiftNoticePerson,
    ShiftNoticeSnapshot,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime

_IMAGE_FILENAME: Final = "shift-handoff.png"
_IMAGE_URL: Final = f"attachment://{_IMAGE_FILENAME}"
_MAX_EMBEDS: Final = 10
_MAX_TITLE_UNITS: Final = 256
_MAX_DESCRIPTION_UNITS: Final = 4096
_MAX_FOOTER_UNITS: Final = 2048
_MAX_FIELD_NAME_UNITS: Final = 256
_MAX_FIELD_VALUE_UNITS: Final = 1024
_MAX_FIELDS: Final = 25
_MAX_AGGREGATE_EMBED_UNITS: Final = 6000
_CLOCK_EMOJIS: Final = (
    "🕛",
    "🕐",
    "🕑",
    "🕒",
    "🕓",
    "🕔",
    "🕕",
    "🕖",
    "🕗",
    "🕘",
    "🕙",
    "🕚",
)
_ACTIVE_STATES: Final = {
    ShiftNoticeFrameState.ACTIVE_EMPTY,
    ShiftNoticeFrameState.ACTIVE_STAFFED,
}


class ShiftNoticeMessageError(RuntimeError):
    pass


class ShiftNoticeImageTooLargeError(ShiftNoticeMessageError):
    pass


@dataclass(frozen=True)
class ShiftNoticeMessageSpec:
    embeds: tuple[discord.Embed, ...]
    image_bytes: bytes | None
    filename: str | None


@dataclass(frozen=True)
class _LocaleStrings:
    ending: str
    continuing: str
    starting: str
    empty: str


_LOCALE_STRINGS: Final = {
    "ja": _LocaleStrings("⏹️ 終了", "⏩ 継続", "▶️ 開始", "なし"),
    "zh_tw": _LocaleStrings("⏹️ 結束", "⏩ 繼續", "▶️ 開始", "無"),
    "en": _LocaleStrings("⏹️ Ending", "⏩ Continuing", "▶️ Starting", "None"),
}


def build_normal_message(
    snapshot: ShiftNoticeSnapshot,
    image_bytes: bytes,
    languages: Sequence[str],
    *,
    upload_limit: int,
) -> ShiftNoticeMessageSpec:
    if len(image_bytes) > upload_limit:
        raise ShiftNoticeImageTooLargeError

    embeds = _build_embeds(
        target_boundary=snapshot.target_boundary,
        boundary_event_hour=snapshot.next.event_hour,
        languages=languages,
        snapshot=snapshot,
    )
    embeds[0].set_image(url=_IMAGE_URL)
    _validate_embeds(embeds)
    return ShiftNoticeMessageSpec(embeds, image_bytes, _IMAGE_FILENAME)


def build_failure_message(
    target_boundary: datetime,
    boundary_event_hour: int,
    languages: Sequence[str],
) -> ShiftNoticeMessageSpec:
    embeds = _build_embeds(
        target_boundary=target_boundary,
        boundary_event_hour=boundary_event_hour,
        languages=languages,
        snapshot=None,
    )
    _validate_embeds(embeds)
    return ShiftNoticeMessageSpec(embeds, None, None)


def _build_embeds(
    *,
    target_boundary: datetime,
    boundary_event_hour: int,
    languages: Sequence[str],
    snapshot: ShiftNoticeSnapshot | None,
) -> tuple[discord.Embed, ...]:
    requested_languages = tuple(languages)
    if not 1 <= len(requested_languages) <= _MAX_EMBEDS:
        raise ShiftNoticeMessageError

    embeds: list[discord.Embed] = []
    for language in requested_languages:
        locale = _locale_strings(language)
        values = _template_values(
            language=language,
            target_boundary=target_boundary,
            boundary_event_hour=boundary_event_hour,
            snapshot=snapshot,
        )
        try:
            title, description, footer = (
                render_message_template(
                    f"shift.notice.{part}",
                    language,
                    **values,
                ).rstrip("\n")
                for part in ("title", "description", "footer")
            )
        except MessageTemplateNotFoundError as exc:
            raise ShiftNoticeMessageError from exc

        embed = discord.Embed(
            title=title,
            description=description,
            timestamp=target_boundary,
            color=bot_config.DEFAULT_EMBED_COLOR,
        )
        embed.set_footer(text=footer)
        if snapshot is not None:
            _add_fields(embed, snapshot, locale)
        embeds.append(embed)
    return tuple(embeds)


def _template_values(
    *,
    language: str,
    target_boundary: datetime,
    boundary_event_hour: int,
    snapshot: ShiftNoticeSnapshot | None,
) -> dict[str, object]:
    is_failure = snapshot is None
    previous_event_hour = (
        boundary_event_hour - 1 if is_failure else snapshot.previous.event_hour
    )
    next_event_hour = boundary_event_hour if is_failure else snapshot.next.event_hour
    return {
        "is_failure": is_failure,
        "case": "failure" if is_failure else snapshot.case.value,
        "previous_event_hour_label": _event_hour_label(
            previous_event_hour,
            language,
        ),
        "next_event_hour_label": _event_hour_label(next_event_hour, language),
        "boundary_event_hour_label": _event_hour_label(
            boundary_event_hour,
            language,
        ),
        "next_event_hour_range_label": _event_hour_range_label(
            next_event_hour,
            language,
        ),
        "emoji": "⚠️" if is_failure else _CLOCK_EMOJIS[target_boundary.hour % 12],
        "next_is_internal_cut": (
            not is_failure and snapshot.next.state is ShiftNoticeFrameState.CUT
        ),
        "next_is_active_empty": (
            not is_failure and snapshot.next.state is ShiftNoticeFrameState.ACTIVE_EMPTY
        ),
    }


def _event_hour_label(event_hour: int, language: str) -> str:
    return f"{event_hour}:00" if language == "en" else f"{event_hour}時"


def _event_hour_range_label(event_hour: int, language: str) -> str:
    if language == "en":
        return f"{event_hour}:00–{event_hour + 1}:00"  # noqa: RUF001
    return f"{event_hour}–{event_hour + 1}時"  # noqa: RUF001


def _locale_strings(language: str) -> _LocaleStrings:
    try:
        return _LOCALE_STRINGS[language]
    except KeyError as exc:
        raise ShiftNoticeMessageError from exc


def _add_fields(
    embed: discord.Embed,
    snapshot: ShiftNoticeSnapshot,
    locale: _LocaleStrings,
) -> None:
    previous_active = snapshot.previous.state in _ACTIVE_STATES
    next_active = snapshot.next.state in _ACTIVE_STATES
    if previous_active:
        embed.add_field(
            name=locale.ending,
            value=_people_value(snapshot.ending, locale.empty),
            inline=False,
        )
    if previous_active and next_active:
        embed.add_field(
            name=locale.continuing,
            value=_people_value(snapshot.continuing, locale.empty),
            inline=False,
        )
    if next_active:
        embed.add_field(
            name=locale.starting,
            value=_people_value(snapshot.starting, locale.empty),
            inline=False,
        )


def _people_value(people: Sequence[ShiftNoticePerson], empty: str) -> str:
    rendered: list[str] = []
    for person in people:
        if person.candidate_member_ids:
            rendered.extend(
                f"<@{member_id}>" for member_id in person.candidate_member_ids
            )
        else:
            rendered.append(escape_markdown(escape_mentions(person.schedule_label)))
    return "、".join(rendered) or empty


def _utf16_length(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def _validate_embeds(embeds: Sequence[discord.Embed]) -> None:
    if not 1 <= len(embeds) <= _MAX_EMBEDS:
        raise ShiftNoticeMessageError

    aggregate_units = 0
    for embed in embeds:
        title = embed.title or ""
        description = embed.description or ""
        footer = embed.footer.text or ""
        if (
            _utf16_length(title) > _MAX_TITLE_UNITS
            or _utf16_length(description) > _MAX_DESCRIPTION_UNITS
            or _utf16_length(footer) > _MAX_FOOTER_UNITS
            or len(embed.fields) > _MAX_FIELDS
        ):
            raise ShiftNoticeMessageError

        aggregate_units += sum(
            _utf16_length(value) for value in (title, description, footer)
        )
        for field in embed.fields:
            if (
                _utf16_length(field.name) > _MAX_FIELD_NAME_UNITS
                or _utf16_length(field.value) > _MAX_FIELD_VALUE_UNITS
            ):
                raise ShiftNoticeMessageError
            aggregate_units += _utf16_length(field.name) + _utf16_length(field.value)

    if aggregate_units > _MAX_AGGREGATE_EMBED_UNITS:
        raise ShiftNoticeMessageError
