from __future__ import annotations

from datetime import UTC, date, datetime
from types import SimpleNamespace

import pytest

from utils.message_templates import render_message_template
from utils.shift_register_timeline import (
    ShiftTimelineInput,
    ShiftTimelineParseError,
    build_shift_timeline_template_values,
    format_iso_hour,
    parse_shift_timeline_input,
    render_shift_timeline_announcement_messages,
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


def test_timeline_iso_formatter_renders_jst_for_settings_ui() -> None:
    value = datetime(2026, 8, 12, 12, tzinfo=UTC)

    assert format_iso_hour(value) == "2026-08-12 21:00 JST"


def test_build_shift_timeline_template_values_formats_ja_structured_values() -> None:
    values = build_shift_timeline_template_values(
        "ja",
        day_number=2,
        event_date=date(2026, 8, 12),
        recruitment_time_range="4-28",
        submission_deadline_at=datetime(2026, 8, 12, 12, tzinfo=UTC),
        draft_shift_proposal_at=datetime(2026, 8, 13, 11, tzinfo=UTC),
        final_shift_notice_at=datetime(2026, 8, 14, 9, tzinfo=UTC),
    )

    assert values["day_number"] == 2
    assert values["recruitment_time_range"] == "4-28"
    assert values["event_date"].month == 8
    assert values["event_date"].day == 12
    assert values["event_date"].weekday == "水"
    assert values["submission_deadline"].day == "12"
    assert values["submission_deadline"].weekday == "水"
    assert values["submission_deadline"].hour == "21"
    assert values["draft_shift_proposal"].day == "13"
    assert values["draft_shift_proposal"].weekday == "木"
    assert values["draft_shift_proposal"].hour == "20"
    assert values["final_shift_notice"].day == "14"
    assert values["final_shift_notice"].weekday == "金"
    assert values["final_shift_notice"].hour == "18"
    assert "title" not in values
    assert "deadline_processing_note" not in values


def test_build_shift_timeline_template_values_formats_zh_tw_structured_values() -> None:
    values = build_shift_timeline_template_values(
        "zh_tw",
        day_number=1,
        event_date=date(2026, 7, 4),
        recruitment_time_range="4-10・14-20・24-28",
        submission_deadline_at=datetime(2026, 7, 20, 12, tzinfo=UTC),
        draft_shift_proposal_at=None,
        final_shift_notice_at=None,
    )

    assert values["event_date"].month == 7
    assert values["event_date"].day == 4
    assert values["event_date"].weekday == "六"
    assert values["submission_deadline"].day == "20"
    assert values["submission_deadline"].weekday == "一"
    assert values["submission_deadline"].hour == "21"
    assert values["draft_shift_proposal"] is None
    assert values["final_shift_notice"] is None


def test_build_shift_timeline_template_values_formats_en_structured_values() -> None:
    values = build_shift_timeline_template_values(
        "en",
        day_number=None,
        event_date=date(2026, 8, 12),
        recruitment_time_range="4-28",
        submission_deadline_at=None,
        draft_shift_proposal_at=None,
        final_shift_notice_at=datetime(2026, 8, 14, 9, tzinfo=UTC),
    )

    assert values["day_number"] is None
    assert values["event_date"].month_name == "Aug"
    assert values["event_date"].day == 12
    assert values["event_date"].weekday == "Wed"
    assert values["submission_deadline"] is None
    assert values["draft_shift_proposal"] is None
    assert values["final_shift_notice"].day == "14"
    assert values["final_shift_notice"].weekday == "Fri"
    assert values["final_shift_notice"].hour == "18"


def test_build_shift_timeline_template_values_pads_only_milestone_day_and_hour() -> (
    None
):
    values = build_shift_timeline_template_values(
        "ja",
        day_number=None,
        event_date=date(2026, 8, 4),
        recruitment_time_range="4-28",
        submission_deadline_at=datetime(2026, 8, 7, 16, tzinfo=UTC),
        draft_shift_proposal_at=None,
        final_shift_notice_at=None,
    )

    assert values["event_date"].month == 8
    assert values["event_date"].day == 4
    assert values["submission_deadline"].day == "08"
    assert values["submission_deadline"].weekday == "土"
    assert values["submission_deadline"].hour == "01"


def test_shift_runtime_templates_trim_jinja_block_lines() -> None:
    values = {
        "day_number": 2,
        "event_date": SimpleNamespace(
            month=8,
            month_name="Aug",
            day=12,
            weekday="水",
        ),
        "recruitment_time_range": "4-28",
        "submission_deadline": SimpleNamespace(day=12, weekday="水", hour=21),
        "draft_shift_proposal": SimpleNamespace(day=13, weekday="木", hour=20),
        "final_shift_notice": SimpleNamespace(day=14, weekday="金", hour=18),
    }

    auto_content = render_message_template(
        "shift.auto_guide.description",
        "ja",
        bot="@Rhoboto",
        sheet_url="https://docs.google.com/spreadsheets/d/example#gid=123",
        **values,
    )
    timeline_content = render_message_template("shift.timeline", "ja", **values)

    assert "\n\n\n" not in auto_content
    assert "\n\n\n" not in timeline_content

    lines = auto_content.splitlines()
    heading_index = next(
        index
        for index, line in enumerate(lines)
        if line.startswith("### 募集時間") and "4-28" in line
    )
    assert lines[heading_index + 1].startswith("- 募集締切")

    deadline_index = next(
        index for index, line in enumerate(lines) if line.startswith("- 募集締切")
    )
    assert lines[deadline_index + 1].startswith("- 仮シフト提示")
    assert lines[deadline_index + 2].startswith("- 確定シフト提示")

    timeline_lines = timeline_content.splitlines()
    timeline_heading_index = next(
        index
        for index, line in enumerate(timeline_lines)
        if line.startswith("### 募集時間") and "4-28" in line
    )
    assert timeline_lines[timeline_heading_index + 1].startswith("- 募集締切")


@pytest.mark.asyncio
async def test_render_shift_timeline_messages_uses_language_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, dict[str, object]] = {}

    async def fake_get_announcement_languages(
        _guild_id: int,
        _logger: object = None,
    ) -> list[str]:
        return ["ja", "en"]

    def fake_render_message_template(
        template_key: str,
        language: str,
        **values: object,
    ) -> str:
        assert "bot" not in values
        captured[language] = values
        return f"{template_key}:{language}\nbody\n"

    monkeypatch.setattr(
        "utils.shift_register_timeline.get_announcement_languages",
        fake_get_announcement_languages,
    )
    monkeypatch.setattr(
        "utils.shift_register_timeline.render_message_template",
        fake_render_message_template,
    )

    rendered = await render_shift_timeline_announcement_messages(
        "shift.timeline",
        111,
        None,
        day_number=2,
        event_date=date(2026, 8, 12),
        recruitment_time_range="4-28",
        submission_deadline_at=datetime(2026, 8, 12, 12, tzinfo=UTC),
        draft_shift_proposal_at=None,
        final_shift_notice_at=datetime(2026, 8, 14, 9, tzinfo=UTC),
    )

    assert [item.language for item in rendered] == ["ja", "en"]
    assert rendered[0].content == "shift.timeline:ja\nbody\n"
    assert captured["ja"]["event_date"].weekday == "水"
    assert captured["ja"]["submission_deadline"].weekday == "水"
    assert captured["ja"]["submission_deadline"].hour == "21"
    assert captured["en"]["event_date"].weekday == "Wed"
    assert captured["en"]["event_date"].month_name == "Aug"
    assert captured["en"]["submission_deadline"].weekday == "Wed"
    assert captured["en"]["submission_deadline"].hour == "21"
    assert captured["en"]["draft_shift_proposal"] is None
    assert "\n\n\n" not in rendered[1].content
