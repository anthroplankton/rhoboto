from __future__ import annotations

# ruff: noqa: RUF001, SLF001
from dataclasses import replace
from datetime import datetime, timedelta
from types import MappingProxyType

import discord
import pytest

from utils import shift_notice_messages
from utils.announcement_languages import normalize_announcement_languages
from utils.shift_notice import (
    JST,
    ShiftNoticeCaseKind,
    ShiftNoticeFrame,
    ShiftNoticeFrameState,
    ShiftNoticePerson,
    ShiftNoticeSnapshot,
)
from utils.shift_notice_messages import (
    ShiftNoticeImageTooLargeError,
    ShiftNoticeMessageError,
    ShiftNoticeMessageSpec,
    build_failure_message,
    build_normal_message,
)

TARGET_BOUNDARY = datetime(2026, 8, 2, 2, tzinfo=JST)
IMAGE_BYTES = b"png"


def make_person(label: str, *candidate_member_ids: int) -> ShiftNoticePerson:
    key = (
        ("member", candidate_member_ids[0])
        if len(candidate_member_ids) == 1
        else ("label", label)
    )
    return ShiftNoticePerson(key, label, candidate_member_ids)


def make_frame(
    state: ShiftNoticeFrameState,
    *,
    civil_start: datetime,
    event_hour: int,
    people: tuple[ShiftNoticePerson, ...] = (),
) -> ShiftNoticeFrame:
    return ShiftNoticeFrame(
        civil_start=civil_start,
        event_hour=event_hour,
        source_id=None
        if state in {ShiftNoticeFrameState.CUT, ShiftNoticeFrameState.OUTSIDE}
        else 1,
        state=state,
        lanes=(*people, *(None for _ in range(5 - len(people)))),
    )


def make_snapshot(  # noqa: PLR0913
    *,
    case: ShiftNoticeCaseKind = ShiftNoticeCaseKind.TRANSITION,
    previous_state: ShiftNoticeFrameState = ShiftNoticeFrameState.ACTIVE_STAFFED,
    next_state: ShiftNoticeFrameState = ShiftNoticeFrameState.ACTIVE_STAFFED,
    ending: tuple[ShiftNoticePerson, ...] = (),
    continuing: tuple[ShiftNoticePerson, ...] = (),
    starting: tuple[ShiftNoticePerson, ...] = (),
    target_boundary: datetime = TARGET_BOUNDARY,
    previous_event_hour: int = 25,
    next_event_hour: int = 26,
) -> ShiftNoticeSnapshot:
    return ShiftNoticeSnapshot(
        target_boundary=target_boundary,
        case=case,
        previous=make_frame(
            previous_state,
            civil_start=target_boundary - timedelta(hours=1),
            event_hour=previous_event_hour,
            people=(*ending, *continuing)[:5],
        ),
        next=make_frame(
            next_state,
            civil_start=target_boundary,
            event_hour=next_event_hour,
            people=(*continuing, *starting)[:5],
        ),
        ending=ending,
        continuing=continuing,
        starting=starting,
        cumulative_hours=MappingProxyType({}),
        remaining_hours=MappingProxyType({}),
        cut_window=None,
    )


def build_normal(
    snapshot: ShiftNoticeSnapshot,
    languages: list[str] | tuple[str, ...] = ("en",),
) -> ShiftNoticeMessageSpec:
    return build_normal_message(
        snapshot,
        IMAGE_BYTES,
        languages,
        upload_limit=len(IMAGE_BYTES),
    )


def utf16_length(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


@pytest.mark.filterwarnings("ignore:'count' is passed as positional argument")
def test_normal_message_preserves_language_member_and_image_shape() -> None:
    unresolved_label = "**raw name** @everyone `<@123456789012345678>"
    snapshot = make_snapshot(
        ending=(make_person("Unique", 101),),
        continuing=(make_person("Duplicate", 201, 202),),
        starting=(make_person(unresolved_label),),
    )
    languages = normalize_announcement_languages(["zh_tw", "ja", "en"])

    result = build_normal(snapshot, languages)

    assert isinstance(result, ShiftNoticeMessageSpec)
    assert isinstance(result.embeds, tuple)
    assert result.image_bytes == IMAGE_BYTES
    assert result.filename == "shift-handoff.png"
    assert [embed.title for embed in result.embeds] == [
        "🕑 26時｜換班資訊",
        "🕑 26時｜シフト交代インフォ",
        "🕑 26:00｜Shift Handoff Info",
    ]
    assert all(embed.timestamp == TARGET_BOUNDARY for embed in result.embeds)
    assert [field.name for field in result.embeds[0].fields] == [
        "⏹️ 結束",
        "⏩ 繼續",
        "▶️ 開始",
    ]
    assert [field.name for field in result.embeds[1].fields] == [
        "⏹️ 終了",
        "⏩ 継続",
        "▶️ 開始",
    ]
    assert [field.name for field in result.embeds[2].fields] == [
        "⏹️ Ending",
        "⏩ Continuing",
        "▶️ Starting",
    ]
    expected_values = [
        "<@101>",
        "<@201>、<@202>",
        "\\*\\*raw name\\*\\* @\u200beveryone \\`<@\u200b123456789012345678>",
    ]
    for embed in result.embeds:
        assert [field.value for field in embed.fields] == expected_values
        assert all(field.inline is False for field in embed.fields)
        assert len(embed.fields) <= 25
    assert result.embeds[0].image.url == "attachment://shift-handoff.png"
    assert all(embed.image.url is None for embed in result.embeds[1:])


def test_build_normal_message_uses_helper_ja_fallback_and_civil_clock() -> None:
    target_boundary = datetime(2026, 8, 2, 1, tzinfo=JST)
    snapshot = make_snapshot(
        target_boundary=target_boundary,
        previous_event_hour=24,
        next_event_hour=25,
    )

    result = build_normal(snapshot, normalize_announcement_languages([]))

    assert len(result.embeds) == 1
    assert result.embeds[0].title == "🕐 25時｜シフト交代インフォ"
    assert result.embeds[0].timestamp == target_boundary


@pytest.mark.parametrize(
    ("previous_state", "next_state", "case", "expected_names"),
    [
        (
            ShiftNoticeFrameState.ACTIVE_EMPTY,
            ShiftNoticeFrameState.ACTIVE_EMPTY,
            ShiftNoticeCaseKind.TRANSITION,
            ["⏹️ Ending", "⏩ Continuing", "▶️ Starting"],
        ),
        (
            ShiftNoticeFrameState.ACTIVE_STAFFED,
            ShiftNoticeFrameState.CUT,
            ShiftNoticeCaseKind.END,
            ["⏹️ Ending"],
        ),
        (
            ShiftNoticeFrameState.CUT,
            ShiftNoticeFrameState.ACTIVE_STAFFED,
            ShiftNoticeCaseKind.START,
            ["▶️ Starting"],
        ),
        (
            ShiftNoticeFrameState.CUT,
            ShiftNoticeFrameState.CUT,
            ShiftNoticeCaseKind.CUT,
            [],
        ),
        (
            ShiftNoticeFrameState.ACTIVE_STAFFED,
            ShiftNoticeFrameState.OUTSIDE,
            ShiftNoticeCaseKind.END,
            ["⏹️ Ending"],
        ),
    ],
)
def test_fields_follow_frame_applicability_in_fixed_order(
    previous_state: ShiftNoticeFrameState,
    next_state: ShiftNoticeFrameState,
    case: ShiftNoticeCaseKind,
    expected_names: list[str],
) -> None:
    embed = build_normal(
        make_snapshot(
            case=case,
            previous_state=previous_state,
            next_state=next_state,
        )
    ).embeds[0]

    assert [field.name for field in embed.fields] == expected_names
    assert [field.value for field in embed.fields] == ["None"] * len(expected_names)
    assert all(field.inline is False for field in embed.fields)


def test_every_applicable_empty_field_uses_its_localized_value() -> None:
    result = build_normal(
        make_snapshot(
            previous_state=ShiftNoticeFrameState.ACTIVE_EMPTY,
            next_state=ShiftNoticeFrameState.ACTIVE_EMPTY,
        ),
        ["ja", "zh_tw", "en"],
    )

    assert [[field.value for field in embed.fields] for embed in result.embeds] == [
        ["なし", "なし", "なし"],
        ["無", "無", "無"],
        ["None", "None", "None"],
    ]


def test_builder_passes_internal_cut_active_empty_and_outer_end_flags() -> None:
    internal_cut = build_normal(
        make_snapshot(
            case=ShiftNoticeCaseKind.END,
            next_state=ShiftNoticeFrameState.CUT,
        )
    ).embeds[0]
    active_empty = build_normal(
        make_snapshot(
            case=ShiftNoticeCaseKind.START,
            previous_state=ShiftNoticeFrameState.CUT,
            next_state=ShiftNoticeFrameState.ACTIVE_EMPTY,
        )
    ).embeds[0]
    outer_end = build_normal(
        make_snapshot(
            case=ShiftNoticeCaseKind.END,
            next_state=ShiftNoticeFrameState.OUTSIDE,
        )
    ).embeds[0]

    assert internal_cut.description.endswith("No shift is scheduled for 26:00–27:00.")
    assert active_empty.description.endswith(
        "No supporters are assigned to the next shift."
    )
    assert "No shift is scheduled" not in outer_end.description


def test_failure_message_has_only_generic_localized_text_and_timestamp() -> None:
    target_boundary = datetime(2026, 8, 2, 1, tzinfo=JST)

    result = build_failure_message(
        target_boundary,
        25,
        ["ja", "zh_tw", "en"],
    )

    assert result.image_bytes is None
    assert result.filename is None
    assert [embed.title for embed in result.embeds] == [
        "⚠️ 25時｜シフト交代インフォ",
        "⚠️ 25時｜換班資訊",
        "⚠️ 25:00｜Shift Handoff Info",
    ]
    assert [embed.description for embed in result.embeds] == [
        "シフト交代情報を表示できませんでした。\n"
        "管理者は /shift_notice send_latest で再送できます。",
        "無法顯示換班資訊。\n管理員可使用 /shift_notice send_latest 重新發送。",
        "Shift handoff information could not be displayed.\n"
        "Administrators can resend it with /shift_notice send_latest.",
    ]
    assert [embed.footer.text for embed in result.embeds] == [
        "シフト時刻：JST",
        "班次時間：JST",
        "Shift time: JST",
    ]
    assert all(embed.timestamp == target_boundary for embed in result.embeds)
    assert all(not embed.fields for embed in result.embeds)
    assert all(embed.image.url is None for embed in result.embeds)
    text = "\n".join(
        str(part)
        for embed in result.embeds
        for part in (embed.title, embed.description, embed.footer.text)
    )
    assert "敬称略" not in text
    assert "敬稱從略" not in text
    assert "Honorifics omitted" not in text


@pytest.mark.parametrize(
    ("part", "overlong_text"),
    [
        ("title", "😀" * 129),
        ("description", "😀" * 2049),
        ("footer", "😀" * 1025),
    ],
)
def test_builder_rejects_utf16_component_overflow(
    monkeypatch: pytest.MonkeyPatch,
    part: str,
    overlong_text: str,
) -> None:
    original = shift_notice_messages.render_message_template

    def fake_render(key: str, locale: str, **values: object) -> str:
        if key == f"shift.notice.{part}":
            return overlong_text
        return original(key, locale, **values)

    monkeypatch.setattr(shift_notice_messages, "render_message_template", fake_render)

    with pytest.raises(ShiftNoticeMessageError):
        build_failure_message(TARGET_BOUNDARY, 26, ["en"])


@pytest.mark.parametrize(
    ("part", "maximum_text"),
    [
        ("title", "😀" * 128),
        ("description", "😀" * 2048),
        ("footer", "😀" * 1024),
    ],
)
def test_builder_accepts_exact_utf16_component_limits(
    monkeypatch: pytest.MonkeyPatch,
    part: str,
    maximum_text: str,
) -> None:
    def fake_render(key: str, locale: str, **values: object) -> str:
        del locale, values
        return maximum_text if key == f"shift.notice.{part}" else "x"

    monkeypatch.setattr(shift_notice_messages, "render_message_template", fake_render)

    result = build_failure_message(TARGET_BOUNDARY, 26, ["en"])

    assert len(result.embeds) == 1


def test_field_name_value_and_count_limits_use_utf16_units() -> None:
    embed = discord.Embed(title="x")
    embed.add_field(name="😀" * 128, value="😀" * 512, inline=False)
    for index in range(24):
        embed.add_field(name=str(index), value="x", inline=False)
    shift_notice_messages._validate_embeds((embed,))

    overlong_name = discord.Embed(title="x")
    overlong_name.add_field(name="😀" * 129, value="x")
    with pytest.raises(ShiftNoticeMessageError):
        shift_notice_messages._validate_embeds((overlong_name,))

    overlong_value = discord.Embed(title="x")
    overlong_value.add_field(name="x", value="😀" * 513)
    with pytest.raises(ShiftNoticeMessageError):
        shift_notice_messages._validate_embeds((overlong_value,))

    embed.add_field(name="26", value="x", inline=False)
    with pytest.raises(ShiftNoticeMessageError):
        shift_notice_messages._validate_embeds((embed,))


def test_aggregate_embed_and_embed_count_limits_are_atomic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    description_units = 2000

    def fake_render(key: str, locale: str, **values: object) -> str:
        del locale, values
        return "x" * description_units if key.endswith(".description") else ""

    monkeypatch.setattr(shift_notice_messages, "render_message_template", fake_render)

    result = build_failure_message(TARGET_BOUNDARY, 26, ["en", "en", "en"])
    assert sum(utf16_length(embed.description or "") for embed in result.embeds) == 6000

    description_units = 2001
    with pytest.raises(ShiftNoticeMessageError):
        build_failure_message(TARGET_BOUNDARY, 26, ["en", "en", "en"])

    description_units = 1
    ten = build_failure_message(TARGET_BOUNDARY, 26, ["en"] * 10)
    assert len(ten.embeds) == 10
    with pytest.raises(ShiftNoticeMessageError):
        build_failure_message(TARGET_BOUNDARY, 26, ["en"] * 11)


def test_missing_requested_language_is_not_silently_dropped() -> None:
    with pytest.raises(ShiftNoticeMessageError):
        build_normal(make_snapshot(), ["ja", "fr"])


def test_upload_limit_is_checked_before_rendering(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_render(*args: object, **kwargs: object) -> str:
        raise AssertionError((args, kwargs))

    monkeypatch.setattr(shift_notice_messages, "render_message_template", fail_render)

    with pytest.raises(ShiftNoticeImageTooLargeError):
        build_normal_message(
            make_snapshot(),
            b"abcd",
            ["en"],
            upload_limit=3,
        )
    assert issubclass(ShiftNoticeImageTooLargeError, ShiftNoticeMessageError)


def test_duplicate_candidate_expansion_is_never_collapsed_split_or_truncated() -> None:
    candidate_ids = tuple(100_000_000_000_000_000 + index for index in range(46))
    person = make_person("Duplicate", *candidate_ids)
    safe_result = build_normal(
        make_snapshot(
            case=ShiftNoticeCaseKind.END,
            next_state=ShiftNoticeFrameState.CUT,
            ending=(person,),
        )
    )
    fields = safe_result.embeds[0].fields

    assert len(fields) == 1
    assert fields[0].value.count("<@") == 46
    assert fields[0].value.count("、") == 45

    overflow = replace(
        person,
        candidate_member_ids=(*candidate_ids, 100_000_000_000_000_046),
    )
    with pytest.raises(ShiftNoticeMessageError):
        build_normal(
            make_snapshot(
                case=ShiftNoticeCaseKind.END,
                next_state=ShiftNoticeFrameState.CUT,
                ending=(overflow,),
            )
        )
