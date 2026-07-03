from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from utils.shift_register_timeline import (
    ShiftTimelineInput,
    ShiftTimelineParseError,
    build_shift_info_template_values,
    format_cjk_datetime,
    format_iso_hour,
    parse_shift_timeline_input,
    render_shift_info_announcement_messages,
)


def test_parse_shift_timeline_accepts_full_values_as_jst_and_stores_utc() -> None:
    result = parse_shift_timeline_input(
        ShiftTimelineInput(
            day_number="2",
            event_date="2026-08-12",
            submission_deadline_at="2026-08-12 21",
            draft_shift_proposal_at="2026/08/13 20",
            final_shift_notice_at="2026-08-14 18",
        ),
        existing_event_date=None,
    )

    assert result.day_number == 2
    assert result.event_date == date(2026, 8, 12)
    assert result.submission_deadline_at == datetime(2026, 8, 12, 12, tzinfo=UTC)
    assert result.draft_shift_proposal_at == datetime(2026, 8, 13, 11, tzinfo=UTC)
    assert result.final_shift_notice_at == datetime(2026, 8, 14, 9, tzinfo=UTC)


def test_parse_shift_timeline_infers_shorthand_year_before_event_date() -> None:
    result = parse_shift_timeline_input(
        ShiftTimelineInput(
            day_number="",
            event_date="2027-01-01",
            submission_deadline_at="12/31 23",
            draft_shift_proposal_at="",
            final_shift_notice_at="",
        ),
        existing_event_date=None,
    )

    assert result.submission_deadline_at == datetime(2026, 12, 31, 14, tzinfo=UTC)


def test_parse_shift_timeline_infers_shorthand_year_after_event_date() -> None:
    result = parse_shift_timeline_input(
        ShiftTimelineInput(
            day_number="",
            event_date="2026-12-31",
            submission_deadline_at="1/1 20",
            draft_shift_proposal_at="",
            final_shift_notice_at="",
        ),
        existing_event_date=None,
    )

    assert result.submission_deadline_at == datetime(2027, 1, 1, 11, tzinfo=UTC)


def test_parse_shift_timeline_infers_shorthand_year_by_calendar_date() -> None:
    result = parse_shift_timeline_input(
        ShiftTimelineInput(
            day_number="",
            event_date="2026-01-01",
            submission_deadline_at="7/2 23",
            draft_shift_proposal_at="",
            final_shift_notice_at="",
        ),
        existing_event_date=None,
    )

    assert result.submission_deadline_at == datetime(2026, 7, 2, 14, tzinfo=UTC)


def test_parse_shift_timeline_accepts_exact_183_calendar_days_after_event() -> None:
    result = parse_shift_timeline_input(
        ShiftTimelineInput(
            day_number="",
            event_date="2027-08-30",
            submission_deadline_at="2/29 23",
            draft_shift_proposal_at="",
            final_shift_notice_at="",
        ),
        existing_event_date=None,
    )

    assert result.submission_deadline_at == datetime(2028, 2, 29, 14, tzinfo=UTC)


def test_parse_shift_timeline_requires_event_date_for_shorthand() -> None:
    with pytest.raises(ShiftTimelineParseError) as exc_info:
        parse_shift_timeline_input(
            ShiftTimelineInput(
                day_number="",
                event_date="",
                submission_deadline_at="8/12 21",
                draft_shift_proposal_at="",
                final_shift_notice_at="",
            ),
            existing_event_date=None,
        )

    assert "Submission Deadline" in str(exc_info.value)


@pytest.mark.parametrize("event_date_value", ["2027-01-01", "2028-12-31"])
def test_parse_shift_timeline_rejects_shorthand_leap_day_too_far(
    event_date_value: str,
) -> None:
    with pytest.raises(ShiftTimelineParseError) as exc_info:
        parse_shift_timeline_input(
            ShiftTimelineInput(
                day_number="",
                event_date=event_date_value,
                submission_deadline_at="2/29 10",
                draft_shift_proposal_at="",
                final_shift_notice_at="",
            ),
            existing_event_date=None,
        )

    assert "Submission Deadline" in str(exc_info.value)
    assert "too far" in str(exc_info.value)


@pytest.mark.parametrize(
    ("event_date_value", "milestone_value"),
    [
        ("8/12", "2026-08-12 21"),
        ("2026-13-01", "2026-08-12 21"),
        ("2026-08-12", "21"),
        ("2026-08-12", "2026-08-12"),
        ("2026-08-12", "2026-08-12 24"),
        ("2026-08-12", "2026-08-12 21:30"),
    ],
)
def test_parse_shift_timeline_rejects_invalid_values(
    event_date_value: str,
    milestone_value: str,
) -> None:
    with pytest.raises(ShiftTimelineParseError):
        parse_shift_timeline_input(
            ShiftTimelineInput(
                day_number="",
                event_date=event_date_value,
                submission_deadline_at=milestone_value,
                draft_shift_proposal_at="",
                final_shift_notice_at="",
            ),
            existing_event_date=None,
        )


def test_parse_shift_timeline_keeps_full_milestone_when_date_is_blank() -> None:
    result = parse_shift_timeline_input(
        ShiftTimelineInput(
            day_number="",
            event_date="",
            submission_deadline_at="2026-08-12 21",
            draft_shift_proposal_at="",
            final_shift_notice_at="",
        ),
        existing_event_date=date(2026, 8, 12),
    )

    assert result.day_number is None
    assert result.event_date is None
    assert result.submission_deadline_at == datetime(2026, 8, 12, 12, tzinfo=UTC)


def test_parse_shift_timeline_normalizes_full_width_digits_and_separators() -> None:
    result = parse_shift_timeline_input(
        ShiftTimelineInput(
            day_number="２",  # noqa: RUF001
            event_date="２０２６／０８／１２",  # noqa: RUF001
            submission_deadline_at="８／１２ ２１",  # noqa: RUF001
            draft_shift_proposal_at="",
            final_shift_notice_at="",
        ),
        existing_event_date=None,
    )

    assert result.day_number == 2
    assert result.event_date == date(2026, 8, 12)
    assert result.submission_deadline_at == datetime(2026, 8, 12, 12, tzinfo=UTC)


def test_timeline_datetime_formatters_render_jst() -> None:
    value = datetime(2026, 8, 12, 12, tzinfo=UTC)

    assert format_cjk_datetime(value) == "08月12日 21時"
    assert format_iso_hour(value) == "2026-08-12 21:00 JST"


def test_build_shift_info_template_values_formats_ja_announcement() -> None:
    values = build_shift_info_template_values(
        "ja",
        day_number=2,
        event_date=date(2026, 8, 12),
        recruitment_time_range="4-28",
        submission_deadline_at=datetime(2026, 8, 12, 12, tzinfo=UTC),
        draft_shift_proposal_at=datetime(2026, 8, 13, 11, tzinfo=UTC),
        final_shift_notice_at=datetime(2026, 8, 14, 9, tzinfo=UTC),
        bot="@Rhoboto",
    )

    assert (
        values["title"] == "\U0001f427 **2\u65e5\u76ee\uff088\u670812\u65e5\uff09"
        "\u30b7\u30d5\u30c8\u767b\u9332\u306e\u304a\u77e5\u3089\u305b** "
        "\U0001f427"
    )
    assert values["submission_deadline_line"] == "募集締切　　　 ⇒ 08月12日 21時"
    assert values["draft_shift_proposal_line"] == "仮シフト提示　 ⇒ 08月13日 20時"
    assert values["final_shift_notice_line"] == "確定シフト提示 ⇒ 08月14日 18時"
    assert "@Rhoboto" in values["deadline_processing_note"]


def test_build_shift_info_template_values_formats_en_fallbacks() -> None:
    values = build_shift_info_template_values(
        "en",
        day_number=None,
        event_date=date(2026, 8, 12),
        recruitment_time_range="4-28",
        submission_deadline_at=None,
        draft_shift_proposal_at=None,
        final_shift_notice_at=datetime(2026, 8, 14, 9, tzinfo=UTC),
        bot="@Rhoboto",
    )

    assert values["title"] == "🐧 **August 12 Shift Registration Announcement** 🐧"
    assert values["submission_deadline_line"] == ""
    assert values["draft_shift_proposal_line"] == ""
    assert values["final_shift_notice_line"] == "Final shift notice ⇒ August 14, 18:00"
    assert values["deadline_processing_note"] == ""


@pytest.mark.asyncio
async def test_render_shift_info_announcement_messages_uses_language_specific_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_get_announcement_languages(
        _guild_id: int,
        _logger: object = None,
    ) -> list[str]:
        return ["ja", "en"]

    monkeypatch.setattr(
        "utils.shift_register_timeline.get_announcement_languages",
        fake_get_announcement_languages,
    )

    rendered = await render_shift_info_announcement_messages(
        "shift.info",
        111,
        None,
        day_number=2,
        event_date=date(2026, 8, 12),
        recruitment_time_range="4-28",
        submission_deadline_at=datetime(2026, 8, 12, 12, tzinfo=UTC),
        draft_shift_proposal_at=None,
        final_shift_notice_at=datetime(2026, 8, 14, 9, tzinfo=UTC),
        bot="@Rhoboto",
    )

    assert [item.language for item in rendered] == ["ja", "en"]
    assert (
        "2\u65e5\u76ee\uff088\u670812\u65e5\uff09"
        "\u30b7\u30d5\u30c8\u767b\u9332\u306e\u304a\u77e5\u3089\u305b"
        in rendered[0].content
    )
    assert "募集締切" in rendered[0].content
    assert "Day 2 (August 12) Shift Registration Announcement" in rendered[1].content
    assert "Submission deadline ⇒ August 12, 21:00" in rendered[1].content
    assert "Draft shift proposal" not in rendered[1].content
    assert "@Rhoboto" in rendered[1].content
    assert "\n\n\n" not in rendered[1].content
