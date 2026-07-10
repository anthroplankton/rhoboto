from __future__ import annotations

import calendar
import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from utils.announcement_languages import (
    RenderedAnnouncement,
    get_announcement_languages,
)
from utils.message_templates import (
    MessageTemplateNotFoundError,
    render_message_template,
)

if TYPE_CHECKING:
    import logging

JST = ZoneInfo("Asia/Tokyo")
MAX_SHORTHAND_DATE_DISTANCE_DAYS = 183


class ShiftTimelineParseError(ValueError):
    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__("\n".join(errors))


@dataclass(frozen=True)
class ShiftTimelineInput:
    day_number: str
    event_date: str
    submission_deadline_at: str
    draft_shift_proposal_at: str
    final_shift_notice_at: str


@dataclass(frozen=True)
class ShiftTimelineValues:
    day_number: int | None
    event_date: date | None
    submission_deadline_at: datetime | None
    draft_shift_proposal_at: datetime | None
    final_shift_notice_at: datetime | None


@dataclass(frozen=True)
class ShiftTimelineEventDateParts:
    month: int
    day: int
    weekday: str
    month_name: str | None = None


@dataclass(frozen=True)
class ShiftTimelineMilestoneParts:
    day: str
    weekday: str
    hour: str


WEEKDAYS: dict[str, tuple[str, ...]] = {
    "ja": ("月", "火", "水", "木", "金", "土", "日"),
    "zh_tw": ("一", "二", "三", "四", "五", "六", "日"),
    "en": ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"),
}


FULL_DATE_PATTERN = re.compile(
    r"^(?P<year>\d{4})(?P<sep>[-/])(?P<month>\d{1,2})(?P=sep)(?P<day>\d{1,2})$"
)
FULL_DATETIME_PATTERN = re.compile(
    r"^(?P<year>\d{4})(?P<sep>[-/])(?P<month>\d{1,2})(?P=sep)"
    r"(?P<day>\d{1,2})\s+(?P<hour>\d{1,2})$"
)
SHORT_DATETIME_PATTERN = re.compile(
    r"^(?P<month>\d{1,2})/(?P<day>\d{1,2})\s+(?P<hour>\d{1,2})$"
)


def normalize_input(value: str) -> str:
    return unicodedata.normalize("NFKC", value).strip()


def parse_shift_timeline_input(
    value: ShiftTimelineInput,
    *,
    existing_event_date: date | None,
) -> ShiftTimelineValues:
    errors: list[str] = []
    raw_event_date = normalize_input(value.event_date)

    day_number = _parse_day_number(value.day_number, errors)
    event_date = _parse_event_date(raw_event_date, errors)
    event_date_for_shorthand = event_date if raw_event_date else existing_event_date
    submission_deadline_at = _parse_milestone(
        "Submission Deadline",
        value.submission_deadline_at,
        event_date_for_shorthand,
        errors,
    )
    draft_shift_proposal_at = _parse_milestone(
        "Draft Shift Proposal",
        value.draft_shift_proposal_at,
        event_date_for_shorthand,
        errors,
    )
    final_shift_notice_at = _parse_milestone(
        "Final Shift Notice",
        value.final_shift_notice_at,
        event_date_for_shorthand,
        errors,
    )
    if errors:
        raise ShiftTimelineParseError(errors)
    return ShiftTimelineValues(
        day_number=day_number,
        event_date=event_date,
        submission_deadline_at=submission_deadline_at,
        draft_shift_proposal_at=draft_shift_proposal_at,
        final_shift_notice_at=final_shift_notice_at,
    )


def _parse_day_number(raw_value: str, errors: list[str]) -> int | None:
    value = normalize_input(raw_value)
    if not value:
        return None
    if not value.isdigit():
        errors.append("Day Number must be a positive integer.")
        return None
    day_number = int(value)
    if day_number <= 0:
        errors.append("Day Number must be a positive integer.")
        return None
    return day_number


def _parse_event_date(raw_value: str, errors: list[str]) -> date | None:
    value = normalize_input(raw_value)
    if not value:
        return None
    match = FULL_DATE_PATTERN.fullmatch(value)
    if match is None:
        errors.append("Event Date must use YYYY-M-D or YYYY/M/D.")
        return None
    try:
        return date(
            int(match.group("year")),
            int(match.group("month")),
            int(match.group("day")),
        )
    except ValueError:
        errors.append("Event Date is not a valid calendar date.")
        return None


def _parse_milestone(
    label: str,
    raw_value: str,
    event_date: date | None,
    errors: list[str],
) -> datetime | None:
    value = normalize_input(raw_value)
    if not value:
        return None
    full_match = FULL_DATETIME_PATTERN.fullmatch(value)
    if full_match is not None:
        return _datetime_from_match(label, full_match, errors)
    short_match = SHORT_DATETIME_PATTERN.fullmatch(value)
    if short_match is not None:
        if event_date is None:
            errors.append(f"{label} shorthand M/D HH requires Event Date.")
            return None
        return _datetime_from_short_match(label, short_match, event_date, errors)
    errors.append(f"{label} must use YYYY-M-D HH, YYYY/M/D HH, or M/D HH.")
    return None


def _datetime_from_match(
    label: str,
    match: re.Match[str],
    errors: list[str],
) -> datetime | None:
    hour = int(match.group("hour"))
    if hour not in range(24):
        errors.append(f"{label} hour must be 0-23.")
        return None
    try:
        local_value = datetime(
            int(match.group("year")),
            int(match.group("month")),
            int(match.group("day")),
            hour,
            tzinfo=JST,
        )
    except ValueError:
        errors.append(f"{label} is not a valid date and hour.")
        return None
    return local_value.astimezone(UTC)


def _datetime_from_short_match(
    label: str,
    match: re.Match[str],
    event_date: date,
    errors: list[str],
) -> datetime | None:
    hour = int(match.group("hour"))
    if hour not in range(24):
        errors.append(f"{label} hour must be 0-23.")
        return None
    month = int(match.group("month"))
    day = int(match.group("day"))
    candidates: list[datetime] = []
    for year in (event_date.year - 1, event_date.year, event_date.year + 1):
        try:
            candidates.append(datetime(year, month, day, hour, tzinfo=JST))
        except ValueError:
            continue
    if not candidates:
        errors.append(f"{label} is not a valid date and hour.")
        return None

    local_value = min(candidates, key=lambda item: abs(item.date() - event_date))
    if abs(local_value.date() - event_date).days > MAX_SHORTHAND_DATE_DISTANCE_DAYS:
        errors.append(f"{label} is too far from Event Date.")
        return None
    return local_value.astimezone(UTC)


def as_jst(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(JST)


def format_iso_hour(value: datetime) -> str:
    local_value = as_jst(value)
    return local_value.strftime("%Y-%m-%d %H:00 JST")


def _weekday_token(language: str, weekday_index: int) -> str:
    return WEEKDAYS.get(language, WEEKDAYS["en"])[weekday_index]


def _build_event_date_parts(
    language: str,
    value: date | None,
) -> ShiftTimelineEventDateParts | None:
    if value is None:
        return None
    return ShiftTimelineEventDateParts(
        month=value.month,
        day=value.day,
        weekday=_weekday_token(language, value.weekday()),
        month_name=calendar.month_abbr[value.month] if language == "en" else None,
    )


def _build_milestone_parts(
    language: str,
    value: datetime | None,
) -> ShiftTimelineMilestoneParts | None:
    if value is None:
        return None
    local_value = as_jst(value)
    return ShiftTimelineMilestoneParts(
        day=f"{local_value.day:02d}",
        weekday=_weekday_token(language, local_value.date().weekday()),
        hour=f"{local_value.hour:02d}",
    )


def build_shift_timeline_template_values(  # noqa: PLR0913
    language: str,
    *,
    day_number: int | None,
    event_date: date | None,
    recruitment_time_range: str,
    submission_deadline_at: datetime | None,
    draft_shift_proposal_at: datetime | None,
    final_shift_notice_at: datetime | None,
) -> dict[str, object]:
    """Build locale-specific values for Shift Register timeline templates."""
    return {
        "day_number": day_number,
        "event_date": _build_event_date_parts(language, event_date),
        "recruitment_time_range": recruitment_time_range,
        "submission_deadline": _build_milestone_parts(
            language,
            submission_deadline_at,
        ),
        "draft_shift_proposal": _build_milestone_parts(
            language,
            draft_shift_proposal_at,
        ),
        "final_shift_notice": _build_milestone_parts(
            language,
            final_shift_notice_at,
        ),
    }


async def render_shift_timeline_announcement_messages(
    template_key: str,
    guild_id: int,
    logger: logging.Logger | None = None,
    **values: object,
) -> list[RenderedAnnouncement]:
    """Render Shift Register timeline announcements with locale-specific values."""
    languages = await get_announcement_languages(guild_id, logger)
    rendered: list[RenderedAnnouncement] = []
    for language in languages:
        try:
            template_values = build_shift_timeline_template_values(
                language,
                day_number=_optional_int(values.get("day_number")),
                event_date=_optional_date(values.get("event_date")),
                recruitment_time_range=str(values["recruitment_time_range"]),
                submission_deadline_at=_optional_datetime(
                    values.get("submission_deadline_at")
                ),
                draft_shift_proposal_at=_optional_datetime(
                    values.get("draft_shift_proposal_at")
                ),
                final_shift_notice_at=_optional_datetime(
                    values.get("final_shift_notice_at")
                ),
            )
            content = render_message_template(template_key, language, **template_values)
        except MessageTemplateNotFoundError:
            if logger is not None:
                logger.warning(
                    "Missing Shift Register timeline template `%s` for language `%s`.",
                    template_key,
                    language,
                )
            continue
        rendered.append(
            RenderedAnnouncement(
                language=language,
                content=content,
            )
        )
    return rendered


def _optional_int(value: object) -> int | None:
    return value if value.__class__ is int else None


def _optional_date(value: object) -> date | None:
    return value if value.__class__ is date else None


def _optional_datetime(value: object) -> datetime | None:
    return value if value.__class__ is datetime else None
