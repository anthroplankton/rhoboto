from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from string import Formatter
from urllib.parse import urlencode

from utils.structs_base import SubmissionParseResult, UserInfo

DEFAULT_CHANNEL_NAME_FORMAT = "部屋番号【{room_number}】"
CHANNEL_NAME_FORMAT_MAX_LENGTH = 256
CHANNEL_NAME_MAX_LENGTH = 100
EMBED_FIELD_MAX_LENGTH = 1024
X_TEXT_MAX_WEIGHT = 280
DISCORD_BUTTON_URL_MAX_LENGTH = 512
X_INTENT_URL = "https://x.com/intent/tweet"
PEOPLE_VALUES = ("", "1", "2", "3", "4")

_ROOM_NUMBER_PATTERN = re.compile(r"[0-9]{5,6}")
_RECRUITMENT_HASHTAG_PATTERN = re.compile(r"#プロセカ(?:協力|募集)(?!\w)")
_FORMATTER = Formatter()
_BRACE_ERROR = "波括弧の対応を確認してください。"
_FORMAT_SPEC_ERROR = "変換指定や書式指定は使用できません。"
_UNKNOWN_FIELD_ERROR = "使用できない形式項目が含まれています。"
_ROOM_NUMBER_ERROR = "部屋番号は5〜6桁の半角数字で指定してください。"
_CHANNEL_NAME_ERROR = "チャンネル名は1〜100文字にしてください。"
_CHANNEL_FORMAT_LENGTH_ERROR = "チャンネル名形式は256文字以内にしてください。"
_HASHTAG_ERROR = "最後の行に #プロセカ協力 または #プロセカ募集 を含めてください。"
_PREVIEW_LENGTH_ERROR = "募集テンプレのプレビューが長すぎます。"
_X_LENGTH_ERROR = "募集テンプレがXの文字数上限を超えています。"
_URL_LENGTH_ERROR = "募集テンプレの投稿リンクが長すぎます。"


class RoomNumberFormatError(ValueError):
    """Raised when a Room-authored format violates the public grammar."""


@dataclass(frozen=True)
class RecruitmentTemplateRender:
    preview: str
    intent_urls: tuple[str, str, str, str, str]


def parse_room_number_text(content: str) -> str | None:
    normalized = unicodedata.normalize("NFKC", content).strip()
    return normalized if _ROOM_NUMBER_PATTERN.fullmatch(normalized) else None


class RoomNumberParser:
    @classmethod
    def parse_submission(
        cls,
        user_info: UserInfo,
        lines: list[str],
    ) -> SubmissionParseResult[str]:
        del cls, user_info
        return SubmissionParseResult(
            submission=parse_room_number_text("\n".join(lines)),
            invalid_attempts=[],
        )


def _raw_replacement_fields(format_text: str) -> tuple[str, ...]:
    fields: list[str] = []
    index = 0
    while index < len(format_text):
        if format_text[index : index + 2] in {"{{", "}}"}:
            index += 2
            continue
        if format_text[index] != "{":
            index += 1
            continue
        end = format_text.find("}", index + 1)
        if end < 0:
            raise RoomNumberFormatError(_BRACE_ERROR)
        fields.append(format_text[index + 1 : end])
        index = end + 1
    return tuple(fields)


def _render_restricted_format(
    format_text: str,
    *,
    values: dict[str, str],
    required_fields: frozenset[str],
) -> str:
    try:
        parsed = tuple(_FORMATTER.parse(format_text))
    except ValueError as exc:
        raise RoomNumberFormatError(_BRACE_ERROR) from exc

    raw_fields = _raw_replacement_fields(format_text)
    parsed_fields = tuple(field for _, field, _, _ in parsed if field is not None)
    if len(raw_fields) != len(parsed_fields):
        raise RoomNumberFormatError(_BRACE_ERROR)
    if any(":" in field or "!" in field for field in raw_fields):
        raise RoomNumberFormatError(_FORMAT_SPEC_ERROR)
    if any(not field or field not in values for field in parsed_fields):
        raise RoomNumberFormatError(_UNKNOWN_FIELD_ERROR)
    if not required_fields.issubset(parsed_fields):
        required = "、".join(f"{{{field}}}" for field in sorted(required_fields))
        message = f"{required} を1回以上含めてください。"
        raise RoomNumberFormatError(message)

    rendered: list[str] = []
    for literal, field, format_spec, conversion in parsed:
        rendered.append(literal)
        if field is None:
            continue
        if format_spec or conversion is not None:
            raise RoomNumberFormatError(_FORMAT_SPEC_ERROR)
        rendered.append(values[field])
    return "".join(rendered)


def render_channel_name(format_text: str, room_number: str) -> str:
    if parse_room_number_text(room_number) != room_number:
        raise RoomNumberFormatError(_ROOM_NUMBER_ERROR)
    rendered = _render_restricted_format(
        format_text,
        values={"room_number": room_number},
        required_fields=frozenset({"room_number"}),
    )
    if not 1 <= len(rendered) <= CHANNEL_NAME_MAX_LENGTH:
        raise RoomNumberFormatError(_CHANNEL_NAME_ERROR)
    return rendered


def validate_channel_name_format(format_text: str) -> str:
    if len(format_text) > CHANNEL_NAME_FORMAT_MAX_LENGTH:
        raise RoomNumberFormatError(_CHANNEL_FORMAT_LENGTH_ERROR)
    render_channel_name(format_text, "123456")
    return format_text


def is_recruitment_template_candidate(content: str) -> bool:
    stripped = content.strip()
    if not stripped:
        return False
    return _RECRUITMENT_HASHTAG_PATTERN.search(stripped.splitlines()[-1]) is not None


def x_text_weight(content: str) -> int:
    return sum(1 if character.isascii() else 2 for character in content)


def _x_intent_url(content: str) -> str:
    return f"{X_INTENT_URL}?{urlencode({'text': content})}"


def render_recruitment_template(
    content: str,
    room_number: str,
) -> RecruitmentTemplateRender:
    stripped = content.strip()
    if not is_recruitment_template_candidate(stripped):
        raise RoomNumberFormatError(_HASHTAG_ERROR)
    renderings = tuple(
        _render_restricted_format(
            stripped,
            values={"room_number": room_number, "people": people},
            required_fields=frozenset({"room_number"}),
        )
        for people in PEOPLE_VALUES
    )
    if len(renderings[0]) > EMBED_FIELD_MAX_LENGTH:
        raise RoomNumberFormatError(_PREVIEW_LENGTH_ERROR)
    if any(x_text_weight(rendered) > X_TEXT_MAX_WEIGHT for rendered in renderings):
        raise RoomNumberFormatError(_X_LENGTH_ERROR)
    urls = tuple(_x_intent_url(rendered) for rendered in renderings)
    if any(len(url) > DISCORD_BUTTON_URL_MAX_LENGTH for url in urls):
        raise RoomNumberFormatError(_URL_LENGTH_ERROR)
    return RecruitmentTemplateRender(
        preview=renderings[0],
        intent_urls=urls,
    )
