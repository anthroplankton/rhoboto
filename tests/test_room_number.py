from urllib.parse import parse_qs, urlsplit

import pytest

from utils.room_number import (
    RoomNumberFormatError,
    RoomNumberParser,
    is_recruitment_template_candidate,
    parse_room_number_text,
    render_channel_name,
    render_recruitment_template,
    validate_channel_name_format,
    x_text_weight,
)
from utils.structs_base import UserInfo


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("12345", "12345"),
        ("123456", "123456"),
        (" 12345 ", "12345"),
        ("１２３４５６", "123456"),  # noqa: RUF001
        ("12345\n", "12345"),
        ("1234", None),
        ("1234567", None),
        ("部屋番号【12345】", None),
        ("123 45", None),
        ("12345\n募集", None),
        ("١٢٣٤٥", None),
    ],
)
def test_parse_room_number_text(content: str, expected: str | None) -> None:
    assert parse_room_number_text(content) == expected


def test_room_number_parser_ignores_nonmatches() -> None:
    result = RoomNumberParser.parse_submission(
        UserInfo(username="alice", display_name="Alice"),
        ["ordinary message"],
    )

    assert result.submission is None
    assert result.invalid_attempts == []


@pytest.mark.parametrize(
    "format_text",
    [
        "部屋番号【{}】",
        "部屋番号【{0}】",
        "部屋番号【{unknown}】",
        "部屋番号【{room_number.x}】",
        "部屋番号【{room_number[0]}】",
        "部屋番号【{room_number!r}】",
        "部屋番号【{room_number:}】",
        "部屋番号【{room_number:>10}】",
        "部屋番号【{room_number】",
        "部屋番号【room_number}】",
        "x" * 95 + "{room_number}",
    ],
)
def test_channel_name_format_rejects_unsupported_grammar(
    format_text: str,
) -> None:
    with pytest.raises(RoomNumberFormatError):
        validate_channel_name_format(format_text)


def test_channel_name_format_renders_repeated_fields_and_escapes() -> None:
    assert (
        render_channel_name(
            "{{部屋}}-{room_number}-{room_number}-{{}}",
            "12345",
        )
        == "{部屋}-12345-12345-{}"
    )


def test_channel_name_format_preserves_authored_unicode() -> None:
    assert render_channel_name("Ａ{room_number}", "12345") == "Ａ12345"  # noqa: RUF001


def test_channel_name_format_enforces_reachable_length_boundaries() -> None:
    assert validate_channel_name_format("{room_number}") == "{room_number}"
    assert len(render_channel_name("x" * 94 + "{room_number}", "123456")) == 100

    with pytest.raises(RoomNumberFormatError, match="1〜100"):
        render_channel_name("x" * 95 + "{room_number}", "123456")


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("{room_number}\n#プロセカ募集", True),
        ("{room_number}\n募集です #プロセカ募集！", True),  # noqa: RUF001
        ("{room_number}\n本文#プロセカ協力", True),
        ("{room_number}\n#プロセカ募集abc", False),
        ("{room_number}\n#プロセカ募集_", False),
        ("{room_number}\n#プロセカ募集\n\n", True),
        ("#プロセカ募集\n本文", False),
        ("   ", False),
    ],
)
def test_recruitment_candidate_uses_final_nonblank_line(
    content: str,
    expected: bool,  # noqa: FBT001
) -> None:
    assert is_recruitment_template_candidate(content) is expected


def test_recruitment_render_uses_empty_and_bare_people_values() -> None:
    rendered = render_recruitment_template(
        "@{people} {room_number} {people}\n#プロセカ募集",
        "12345",
    )

    assert rendered.preview == "@ 12345 \n#プロセカ募集"
    assert [
        parse_qs(urlsplit(url).query)["text"][0] for url in rendered.intent_urls
    ] == [
        "@ 12345 \n#プロセカ募集",
        "@1 12345 1\n#プロセカ募集",
        "@2 12345 2\n#プロセカ募集",
        "@3 12345 3\n#プロセカ募集",
        "@4 12345 4\n#プロセカ募集",
    ]


def test_recruitment_render_allows_missing_people_and_preserves_emoji() -> None:
    template = "👨‍👩‍👧‍👦️ {room_number}\n#プロセカ協力"
    rendered = render_recruitment_template(template, "12345")

    assert rendered.preview == "👨‍👩‍👧‍👦️ 12345\n#プロセカ協力"
    assert all(
        parse_qs(urlsplit(url).query)["text"][0] == rendered.preview
        for url in rendered.intent_urls
    )


def test_recruitment_intent_preserves_unicode_iri_and_round_trips() -> None:
    template = """ベテラン 高速:shrimp:周回
@ {people}

:key:{room_number}
支援者様、アンコ枠様います。

主\uff1a
募\uff1a

いじぺち、SF後放置OK
SF気にしません、謝罪不要です
主のおつさきで解散

#プロセカ協力 #プロセカ募集"""

    rendered = render_recruitment_template(template, "123456")

    assert max(map(len, rendered.intent_urls)) == 185
    assert all("ベテラン" in url for url in rendered.intent_urls)
    assert all("%23プロセカ協力" in url for url in rendered.intent_urls)
    assert [
        parse_qs(urlsplit(url).query)["text"][0] for url in rendered.intent_urls
    ] == [
        template.strip().replace("{people}", people).replace("{room_number}", "123456")
        for people in ("", "1", "2", "3", "4")
    ]


@pytest.mark.parametrize(
    "template",
    [
        "no room\n#プロセカ募集",
        "{}\n#プロセカ募集",
        "{unknown}\n#プロセカ募集",
        "{room_number!r}\n#プロセカ募集",
        "{room_number:}\n#プロセカ募集",
    ],
)
def test_recruitment_render_rejects_unsupported_format(template: str) -> None:
    with pytest.raises(RoomNumberFormatError):
        render_recruitment_template(template, "12345")


def test_x_text_weight_uses_conservative_unicode_weight() -> None:
    assert x_text_weight("abcあ👨‍👩") == 11


def test_recruitment_render_enforces_x_weight_boundary() -> None:
    accepted = render_recruitment_template(
        "a" * 260 + "{room_number}\n#プロセカ募集",
        "123456",
    )
    assert x_text_weight(accepted.preview) == 280

    with pytest.raises(RoomNumberFormatError, match="Xの文字数上限"):
        render_recruitment_template(
            "a" * 261 + "{room_number}\n#プロセカ募集",
            "123456",
        )


def test_recruitment_render_enforces_preview_before_x_weight() -> None:
    with pytest.raises(RoomNumberFormatError, match="Xの文字数上限"):
        render_recruitment_template(
            "a" * 1010 + "{room_number}\n#プロセカ募集",
            "123456",
        )

    with pytest.raises(RoomNumberFormatError, match="プレビュー"):
        render_recruitment_template(
            "a" * 1011 + "{room_number}\n#プロセカ募集",
            "123456",
        )


def test_recruitment_render_enforces_iri_url_boundary() -> None:
    accepted = render_recruitment_template(
        "%" * 154 + "{room_number}\n#プロセカ募集",
        "123456",
    )
    assert len(accepted.intent_urls[0]) == 512

    with pytest.raises(RoomNumberFormatError, match="投稿リンク"):
        render_recruitment_template(
            "%" * 155 + "{room_number}\n#プロセカ募集",
            "123456",
        )
