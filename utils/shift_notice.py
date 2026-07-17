from __future__ import annotations

import itertools as it
import re
import unicodedata
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta
from enum import StrEnum
from types import MappingProxyType
from typing import TYPE_CHECKING, Literal
from zoneinfo import ZoneInfo

from utils.google_sheets_urls import extract_google_sheet_id
from utils.shift_final import parse_a1_cell
from utils.shift_register_structs import RecruitmentTimeRanges
from utils.structs_base import WorksheetContractError

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from utils.shift_final import A1Cell
    from utils.shift_register_structs import HourRange
    from utils.shift_schedule_role import ScheduleRoleLabelMatch

JST = ZoneInfo("Asia/Tokyo")
_HOUR = timedelta(hours=1)
_MINUTE_PATTERN = re.compile(r"[0-9]+")
_CUT_SIDE_TARGET = 3
_CUT_ROW_CAPACITY = 7


class ShiftNoticeCaseKind(StrEnum):
    START = "start"
    TRANSITION = "transition"
    END = "end"
    CUT = "cut"


class ShiftNoticeFrameState(StrEnum):
    OUTSIDE = "outside"
    CUT = "cut"
    ACTIVE_EMPTY = "active_empty"
    ACTIVE_STAFFED = "active_staffed"


type CanonicalPersonKey = tuple[Literal["member"], int] | tuple[Literal["label"], str]


@dataclass(frozen=True)
class ShiftNoticePerson:
    key: CanonicalPersonKey
    schedule_label: str
    candidate_member_ids: tuple[int, ...]


@dataclass(frozen=True)
class ShiftNoticeFrame:
    civil_start: datetime
    event_hour: int
    source_id: int | None
    state: ShiftNoticeFrameState
    lanes: tuple[ShiftNoticePerson | None, ...]

    def __post_init__(self) -> None:
        if len(self.lanes) != 5:  # noqa: PLR2004
            msg = "Shift Notice frames require exactly five lanes."
            raise ValueError(msg)


@dataclass(frozen=True)
class ShiftNoticeCutWindow:
    rows: tuple[ShiftNoticeFrame, ...]
    truncated_before: bool
    truncated_after: bool


@dataclass(frozen=True)
class ShiftNoticeSnapshot:
    target_boundary: datetime
    case: ShiftNoticeCaseKind
    previous: ShiftNoticeFrame
    next: ShiftNoticeFrame
    ending: tuple[ShiftNoticePerson, ...]
    continuing: tuple[ShiftNoticePerson, ...]
    starting: tuple[ShiftNoticePerson, ...]
    cumulative_hours: Mapping[CanonicalPersonKey, int]
    remaining_hours: Mapping[CanonicalPersonKey, int]
    cut_window: ShiftNoticeCutWindow | None


@dataclass(frozen=True)
class ShiftNoticeSnapshotPlan:
    missing_source_ids: frozenset[int]
    snapshot: ShiftNoticeSnapshot | None


@dataclass(frozen=True)
class ShiftNoticeSourceRecord:
    id: int
    feature_channel_id: int
    channel_id: int
    is_enabled: bool
    created_at: datetime
    sheet_url: str
    final_schedule_worksheet_id: int
    final_schedule_anchor_cell: str
    event_date: date | None
    recruitment_time_ranges: object


@dataclass(frozen=True)
class ShiftNoticeSource:
    id: int
    feature_channel_id: int
    channel_id: int
    is_enabled: bool
    created_at: datetime
    sheet_url: str
    spreadsheet_id: str
    final_schedule_worksheet_id: int
    final_schedule_anchor_cell: A1Cell
    event_date: date
    recruitment_time_ranges: tuple[HourRange, ...]
    first_hour: int
    end_hour: int

    def civil_start(self, event_hour: int) -> datetime:
        return civil_start(self.event_date, event_hour)

    def event_hour(self, value: datetime) -> int:
        _require_jst(value)
        hour, remainder = divmod(value - civil_start(self.event_date, 0), _HOUR)
        if remainder:
            msg = "Civil time must start on an event-hour boundary."
            raise ValueError(msg)
        return hour


@dataclass(frozen=True)
class ShiftNoticeOverlapLoss:
    civil_start: datetime
    civil_end: datetime
    losing_source_id: int
    winning_source_id: int


@dataclass(frozen=True)
class ShiftNoticeCatalog:
    complete_sources: tuple[ShiftNoticeSource, ...]
    incomplete_sources: tuple[ShiftNoticeSourceRecord, ...]
    slot_owners: Mapping[datetime, int | None]
    envelope_start: datetime | None
    envelope_end: datetime | None
    overlap_losses: tuple[ShiftNoticeOverlapLoss, ...]


def parse_minute_of_hour(raw: str) -> int:
    if not isinstance(raw, str):
        msg = "Invalid minute of hour."
        raise TypeError(msg)
    normalized = unicodedata.normalize("NFKC", raw).strip()
    if _MINUTE_PATTERN.fullmatch(normalized) is None:
        msg = "Invalid minute of hour."
        raise ValueError(msg)
    minute = int(normalized)
    if not 0 <= minute <= 59:  # noqa: PLR2004
        msg = "Invalid minute of hour."
        raise ValueError(msg)
    return minute


def civil_start(event_date: date, event_hour: int) -> datetime:
    return datetime.combine(event_date, time.min, tzinfo=JST) + timedelta(
        hours=event_hour
    )


def scheduled_tick_for_boundary(boundary: datetime, minute: int) -> datetime:
    _require_jst(boundary)
    _require_minute(minute)
    if minute == 0:
        return boundary
    return boundary - _HOUR + timedelta(minutes=minute)


def boundary_for_scheduled_tick(tick: datetime, minute: int) -> datetime:
    _require_jst(tick)
    _require_minute(minute)
    if minute == 0:
        return tick
    return tick + timedelta(minutes=60 - minute)


def latest_reached_boundary(
    now: datetime,
    minute: int,
    envelope_start: datetime,
    envelope_end: datetime,
) -> datetime | None:
    _require_jst(now)
    _require_jst(envelope_start)
    _require_jst(envelope_end)
    _require_minute(minute)
    if envelope_start > envelope_end:
        msg = "Envelope start must not follow its end."
        raise ValueError(msg)

    latest_tick = now.replace(minute=minute, second=0, microsecond=0)
    if latest_tick > now:
        latest_tick -= _HOUR
    latest_boundary = boundary_for_scheduled_tick(latest_tick, minute)
    if latest_boundary < envelope_start:
        return None
    return min(latest_boundary, envelope_end)


def build_source_catalog(
    records: Sequence[ShiftNoticeSourceRecord],
) -> ShiftNoticeCatalog:
    complete_sources: list[ShiftNoticeSource] = []
    incomplete_sources: list[ShiftNoticeSourceRecord] = []
    for record in sorted(records, key=lambda item: (item.created_at, item.id)):
        source = _complete_source(record)
        if source is None:
            incomplete_sources.append(record)
        else:
            complete_sources.append(source)

    if not complete_sources:
        return ShiftNoticeCatalog(
            complete_sources=(),
            incomplete_sources=tuple(incomplete_sources),
            slot_owners={},
            envelope_start=None,
            envelope_end=None,
            overlap_losses=(),
        )

    hulls = tuple(
        (
            source,
            source.civil_start(source.first_hour),
            source.civil_start(source.end_hour),
        )
        for source in complete_sources
    )
    envelope_start = min(start for _, start, _ in hulls)
    envelope_end = max(end for _, _, end in hulls)

    tier_one_owners: dict[datetime, int] = {}
    unit_losses: list[ShiftNoticeOverlapLoss] = []
    for source in complete_sources:
        for hour_range in source.recruitment_time_ranges:
            for event_hour in range(hour_range.start, hour_range.end):
                slot = source.civil_start(event_hour)
                winning_source_id = tier_one_owners.get(slot)
                if winning_source_id is None:
                    tier_one_owners[slot] = source.id
                    continue
                unit_losses.append(
                    ShiftNoticeOverlapLoss(
                        civil_start=slot,
                        civil_end=slot + _HOUR,
                        losing_source_id=source.id,
                        winning_source_id=winning_source_id,
                    )
                )

    slot_owners: dict[datetime, int | None] = {}
    slot = envelope_start
    while slot < envelope_end:
        owner = tier_one_owners.get(slot)
        if owner is None:
            owner = next(
                (
                    source.id
                    for source, hull_start, hull_end in hulls
                    if hull_start <= slot < hull_end
                ),
                None,
            )
        slot_owners[slot] = owner
        slot += _HOUR

    return ShiftNoticeCatalog(
        complete_sources=tuple(complete_sources),
        incomplete_sources=tuple(incomplete_sources),
        slot_owners=slot_owners,
        envelope_start=envelope_start,
        envelope_end=envelope_end,
        overlap_losses=_merge_overlap_losses(unit_losses),
    )


def project_source_frames(
    source: ShiftNoticeSource,
    worksheet_values: Sequence[Sequence[object]],
    label_matches: Mapping[str, ScheduleRoleLabelMatch],
) -> dict[datetime, ShiftNoticeFrame]:
    frames: dict[datetime, ShiftNoticeFrame] = {}
    first_row = source.final_schedule_anchor_cell.row - 1
    first_column = source.final_schedule_anchor_cell.column - 1
    for event_hour in range(source.first_hour, source.end_hour):
        row_index = first_row + event_hour - source.first_hour
        row = worksheet_values[row_index] if row_index < len(worksheet_values) else ()
        values = tuple(
            row[column] if column < len(row) else ""
            for column in range(first_column, first_column + 6)
        )
        if any(
            value not in (None, "") and not isinstance(value, str) for value in values
        ):
            raise WorksheetContractError(log_hint="final_schedule_role_value_not_text")

        runner, *lane_values = values
        if runner in (None, ""):
            state = ShiftNoticeFrameState.CUT
            lanes: tuple[ShiftNoticePerson | None, ...] = (None,) * 5
        else:
            lanes = tuple(
                None if label in (None, "") else _person_for_label(label, label_matches)
                for label in lane_values
            )
            state = (
                ShiftNoticeFrameState.ACTIVE_STAFFED
                if any(lanes)
                else ShiftNoticeFrameState.ACTIVE_EMPTY
            )
        start = source.civil_start(event_hour)
        frames[start] = ShiftNoticeFrame(
            civil_start=start,
            event_hour=event_hour,
            source_id=source.id,
            state=state,
            lanes=lanes,
        )
    return frames


def align_next_honso(
    previous: ShiftNoticeFrame,
    next: ShiftNoticeFrame,  # noqa: A002
) -> ShiftNoticeFrame:
    original = next.lanes[1:4]
    _, best = min(
        enumerate(it.permutations(original)),
        key=lambda item: (
            *_honso_alignment_cost(previous.lanes[1:4], item[1], original),
            item[0],
        ),
    )
    return replace(next, lanes=(next.lanes[0], *best, next.lanes[4]))


def plan_snapshot(
    catalog: ShiftNoticeCatalog,
    loaded_frames: Mapping[datetime, ShiftNoticeFrame],
    target_boundary: datetime,
) -> ShiftNoticeSnapshotPlan:
    missing_source_ids: set[int] = set()
    previous = _selected_frame(
        catalog,
        loaded_frames,
        target_boundary - _HOUR,
        missing_source_ids,
    )
    next_frame = _selected_frame(
        catalog,
        loaded_frames,
        target_boundary,
        missing_source_ids,
    )
    if missing_source_ids:
        return ShiftNoticeSnapshotPlan(frozenset(missing_source_ids), None)
    if previous is None or next_frame is None:
        msg = "Boundary frames must be available after source planning."
        raise RuntimeError(msg)

    next_frame = align_next_honso(previous, next_frame)
    previous_people = _unique_people(previous)
    next_people = _unique_people(next_frame)
    previous_keys = {person.key for person in previous_people}
    next_keys = {person.key for person in next_people}
    case = _classify_boundary(previous, next_frame)

    cumulative_hours = {
        person.key: _consecutive_hours(
            catalog,
            loaded_frames,
            previous.civil_start,
            -_HOUR,
            person.key,
            missing_source_ids,
        )
        for person in previous_people
    }
    remaining_hours = {
        person.key: _consecutive_hours(
            catalog,
            loaded_frames,
            next_frame.civil_start,
            _HOUR,
            person.key,
            missing_source_ids,
        )
        for person in next_people
    }

    cut_window = None
    if case is ShiftNoticeCaseKind.CUT:
        current_cut = (
            next_frame if next_frame.state is ShiftNoticeFrameState.CUT else previous
        )
        cut_window = _plan_cut_window(
            catalog,
            loaded_frames,
            current_cut,
            missing_source_ids,
        )

    if missing_source_ids:
        return ShiftNoticeSnapshotPlan(frozenset(missing_source_ids), None)
    return ShiftNoticeSnapshotPlan(
        missing_source_ids=frozenset(),
        snapshot=ShiftNoticeSnapshot(
            target_boundary=target_boundary,
            case=case,
            previous=previous,
            next=next_frame,
            ending=tuple(
                person for person in previous_people if person.key not in next_keys
            ),
            continuing=tuple(
                person for person in previous_people if person.key in next_keys
            ),
            starting=tuple(
                person for person in next_people if person.key not in previous_keys
            ),
            cumulative_hours=MappingProxyType(cumulative_hours),
            remaining_hours=MappingProxyType(remaining_hours),
            cut_window=cut_window,
        ),
    )


def _person_for_label(
    label: str,
    label_matches: Mapping[str, ScheduleRoleLabelMatch],
) -> ShiftNoticePerson:
    match = label_matches.get(label)
    member_ids = () if match is None else match.member_ids
    key: CanonicalPersonKey = (
        ("member", member_ids[0]) if len(member_ids) == 1 else ("label", label)
    )
    return ShiftNoticePerson(
        key=key,
        schedule_label=label,
        candidate_member_ids=member_ids,
    )


def _honso_alignment_cost(
    previous: tuple[ShiftNoticePerson | None, ...],
    current: tuple[ShiftNoticePerson | None, ...],
    original: tuple[ShiftNoticePerson | None, ...],
) -> tuple[int, int, int]:
    previous_keys = tuple(person.key if person else None for person in previous)
    current_keys = tuple(person.key if person else None for person in current)
    shared_keys = set(previous_keys) & set(current_keys) - {None}
    changes = sum(
        previous_keys.index(key) != current_keys.index(key) for key in shared_keys
    )
    distance = sum(
        abs(previous_keys.index(key) - current_keys.index(key)) for key in shared_keys
    )
    original_changes = sum(
        person is not None and person != original_person
        for person, original_person in zip(current, original, strict=True)
    )
    return changes, distance, original_changes


def _selected_frame(
    catalog: ShiftNoticeCatalog,
    loaded_frames: Mapping[datetime, ShiftNoticeFrame],
    start: datetime,
    missing_source_ids: set[int],
) -> ShiftNoticeFrame | None:
    if (
        catalog.envelope_start is None
        or catalog.envelope_end is None
        or start < catalog.envelope_start
        or start >= catalog.envelope_end
    ):
        return _inactive_frame(catalog, start, ShiftNoticeFrameState.OUTSIDE)

    source_id = catalog.slot_owners.get(start)
    if source_id is None:
        return _inactive_frame(catalog, start, ShiftNoticeFrameState.CUT)
    frame = loaded_frames.get(start)
    if frame is None or frame.source_id != source_id:
        missing_source_ids.add(source_id)
        return None
    return frame


def _inactive_frame(
    catalog: ShiftNoticeCatalog,
    start: datetime,
    state: ShiftNoticeFrameState,
) -> ShiftNoticeFrame:
    event_hour = start.hour
    for source in catalog.complete_sources:
        candidate = source.event_hour(start)
        if 0 <= candidate <= 30:  # noqa: PLR2004
            event_hour = candidate
            break
    return ShiftNoticeFrame(
        civil_start=start,
        event_hour=event_hour,
        source_id=None,
        state=state,
        lanes=(None,) * 5,
    )


def _unique_people(frame: ShiftNoticeFrame) -> tuple[ShiftNoticePerson, ...]:
    people: dict[CanonicalPersonKey, ShiftNoticePerson] = {}
    for person in frame.lanes:
        if person is not None:
            people.setdefault(person.key, person)
    return tuple(people.values())


def _classify_boundary(
    previous: ShiftNoticeFrame,
    next_frame: ShiftNoticeFrame,
) -> ShiftNoticeCaseKind:
    previous_active = previous.state in {
        ShiftNoticeFrameState.ACTIVE_EMPTY,
        ShiftNoticeFrameState.ACTIVE_STAFFED,
    }
    next_active = next_frame.state in {
        ShiftNoticeFrameState.ACTIVE_EMPTY,
        ShiftNoticeFrameState.ACTIVE_STAFFED,
    }
    if previous_active and next_active:
        return ShiftNoticeCaseKind.TRANSITION
    if previous_active:
        return ShiftNoticeCaseKind.END
    if next_active:
        return ShiftNoticeCaseKind.START
    return ShiftNoticeCaseKind.CUT


def _consecutive_hours(  # noqa: PLR0913
    catalog: ShiftNoticeCatalog,
    loaded_frames: Mapping[datetime, ShiftNoticeFrame],
    start: datetime,
    step: timedelta,
    person_key: CanonicalPersonKey,
    missing_source_ids: set[int],
) -> int:
    hours = 0
    current = start
    while True:
        frame = _selected_frame(
            catalog,
            loaded_frames,
            current,
            missing_source_ids,
        )
        if frame is None or frame.state not in {
            ShiftNoticeFrameState.ACTIVE_EMPTY,
            ShiftNoticeFrameState.ACTIVE_STAFFED,
        }:
            return hours
        if person_key not in {person.key for person in frame.lanes if person}:
            return hours
        hours += 1
        current += step


def _plan_cut_window(
    catalog: ShiftNoticeCatalog,
    loaded_frames: Mapping[datetime, ShiftNoticeFrame],
    current: ShiftNoticeFrame,
    missing_source_ids: set[int],
) -> ShiftNoticeCutWindow | None:
    before, before_closed = _cut_side(
        catalog,
        loaded_frames,
        current.civil_start,
        -_HOUR,
        _CUT_SIDE_TARGET,
        missing_source_ids,
    )
    after, after_closed = _cut_side(
        catalog,
        loaded_frames,
        current.civil_start,
        _HOUR,
        _CUT_SIDE_TARGET,
        missing_source_ids,
    )
    if missing_source_ids:
        return None

    if before_closed and len(before) < _CUT_SIDE_TARGET and not after_closed:
        after, after_closed = _cut_side(
            catalog,
            loaded_frames,
            current.civil_start,
            _HOUR,
            _CUT_ROW_CAPACITY - 1 - len(before),
            missing_source_ids,
        )
    elif after_closed and len(after) < _CUT_SIDE_TARGET and not before_closed:
        before, before_closed = _cut_side(
            catalog,
            loaded_frames,
            current.civil_start,
            -_HOUR,
            _CUT_ROW_CAPACITY - 1 - len(after),
            missing_source_ids,
        )
    if missing_source_ids:
        return None

    truncated_before = _cut_edge_continues(
        catalog,
        loaded_frames,
        current.civil_start - _HOUR * (len(before) + 1),
        missing_source_ids,
        edge_closed=before_closed,
    )
    truncated_after = _cut_edge_continues(
        catalog,
        loaded_frames,
        current.civil_start + _HOUR * (len(after) + 1),
        missing_source_ids,
        edge_closed=after_closed,
    )
    if missing_source_ids:
        return None
    return ShiftNoticeCutWindow(
        rows=(*reversed(before), current, *after),
        truncated_before=truncated_before,
        truncated_after=truncated_after,
    )


def _cut_side(  # noqa: PLR0913
    catalog: ShiftNoticeCatalog,
    loaded_frames: Mapping[datetime, ShiftNoticeFrame],
    current_start: datetime,
    step: timedelta,
    limit: int,
    missing_source_ids: set[int],
) -> tuple[list[ShiftNoticeFrame], bool]:
    frames: list[ShiftNoticeFrame] = []
    for distance in range(1, limit + 1):
        frame = _selected_frame(
            catalog,
            loaded_frames,
            current_start + step * distance,
            missing_source_ids,
        )
        if frame is None:
            return frames, False
        if frame.state is not ShiftNoticeFrameState.CUT:
            return frames, True
        frames.append(frame)
    return frames, False


def _cut_edge_continues(
    catalog: ShiftNoticeCatalog,
    loaded_frames: Mapping[datetime, ShiftNoticeFrame],
    probe_start: datetime,
    missing_source_ids: set[int],
    *,
    edge_closed: bool,
) -> bool:
    if edge_closed:
        return False
    frame = _selected_frame(
        catalog,
        loaded_frames,
        probe_start,
        missing_source_ids,
    )
    return frame is not None and frame.state is ShiftNoticeFrameState.CUT


def _complete_source(record: ShiftNoticeSourceRecord) -> ShiftNoticeSource | None:
    raw_ranges = record.recruitment_time_ranges
    worksheet_id = record.final_schedule_worksheet_id
    if (
        record.event_date is None
        or raw_ranges is None
        or raw_ranges == []
        or not isinstance(record.sheet_url, str)
        or worksheet_id.__class__ is not int
        or worksheet_id <= 0
    ):
        return None

    try:
        ranges = tuple(RecruitmentTimeRanges.from_json(raw_ranges).ranges.ranges)
        spreadsheet_id = extract_google_sheet_id(record.sheet_url)
        anchor = parse_a1_cell(record.final_schedule_anchor_cell)
    except (TypeError, ValueError):
        return None
    if not ranges:
        return None

    return ShiftNoticeSource(
        id=record.id,
        feature_channel_id=record.feature_channel_id,
        channel_id=record.channel_id,
        is_enabled=record.is_enabled,
        created_at=record.created_at,
        sheet_url=record.sheet_url,
        spreadsheet_id=spreadsheet_id,
        final_schedule_worksheet_id=worksheet_id,
        final_schedule_anchor_cell=anchor,
        event_date=record.event_date,
        recruitment_time_ranges=ranges,
        first_hour=ranges[0].start,
        end_hour=ranges[-1].end,
    )


def _merge_overlap_losses(
    losses: list[ShiftNoticeOverlapLoss],
) -> tuple[ShiftNoticeOverlapLoss, ...]:
    ordered = sorted(
        losses,
        key=lambda loss: (
            loss.losing_source_id,
            loss.winning_source_id,
            loss.civil_start.date(),
            loss.civil_start,
        ),
    )
    merged: list[ShiftNoticeOverlapLoss] = []
    for loss in ordered:
        if merged:
            previous = merged[-1]
            if (
                previous.losing_source_id == loss.losing_source_id
                and previous.winning_source_id == loss.winning_source_id
                and previous.civil_start.date() == loss.civil_start.date()
                and previous.civil_end == loss.civil_start
            ):
                merged[-1] = ShiftNoticeOverlapLoss(
                    civil_start=previous.civil_start,
                    civil_end=loss.civil_end,
                    losing_source_id=loss.losing_source_id,
                    winning_source_id=loss.winning_source_id,
                )
                continue
        merged.append(loss)
    return tuple(
        sorted(
            merged,
            key=lambda loss: (
                loss.civil_start,
                loss.losing_source_id,
                loss.winning_source_id,
            ),
        )
    )


def _require_jst(value: datetime) -> None:
    if value.tzinfo != JST:
        msg = "Datetime must be aware and use JST."
        raise ValueError(msg)


def _require_minute(minute: int) -> None:
    if minute.__class__ is not int or not 0 <= minute <= 59:  # noqa: PLR2004
        msg = "Invalid minute of hour."
        raise ValueError(msg)
