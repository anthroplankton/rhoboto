from __future__ import annotations

import colorsys
import itertools as it
import re
import unicodedata
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from utils.shift_register_structs import (
    DraftWorksheetContent,
    RecruitmentTimeRanges,
    ShiftParser,
)
from utils.shift_scheduler import hour_label

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import date

GOOGLE_SHEETS_MAX_ROWS = 10_000_000
GOOGLE_SHEETS_MAX_COLUMNS = 18_278
FINAL_ANCHOR_MAX_LENGTH = 8
DEFAULT_EVENT_DAY_FORMAT = "{MM}月{DD}日 {dddd_ja} {dddd_en}, {MMMM_en} {DD}"


@dataclass(frozen=True)
class A1Cell:
    row: int
    column: int
    a1: str


@dataclass(frozen=True)
class A1Rectangle:
    start: A1Cell
    end: A1Cell

    @property
    def a1(self) -> str:
        return f"{self.start.a1}:{self.end.a1}"

    def contains(self, cell: A1Cell) -> bool:
        return (
            self.start.row <= cell.row <= self.end.row
            and self.start.column <= cell.column <= self.end.column
        )


class EventDayWriteStatus(StrEnum):
    READY = "ready"
    OMITTED = "omitted"
    FORMAT_IGNORED = "format_ignored"
    INVALID_ANCHOR = "invalid_anchor"
    OVERLAPS_MAIN = "overlaps_main"
    MISSING_EVENT_DATE = "missing_event_date"
    INVALID_FORMAT = "invalid_format"


class FinalScheduleInputError(ValueError):
    """Raised when a Final command input cannot be planned safely."""


class FinalScheduleValidationKind(StrEnum):
    EMPTY = "empty"
    HEADER = "header"
    AXIS = "axis"
    EXTRA_AXIS = "extra_axis"
    ROLE_VALUE = "role_value"


class FinalScheduleValidationError(Exception):
    def __init__(
        self,
        kind: FinalScheduleValidationKind,
        *,
        row: int | None = None,
        column: int | None = None,
        expected: object = None,
        detected: object = None,
    ) -> None:
        super().__init__(kind.value)
        self.kind = kind
        self.row = row
        self.column = column
        self.expected = expected
        self.detected = detected


@dataclass(frozen=True)
class FinalRoleConflict:
    hour: int
    name: str
    roles: tuple[str, ...]


class FinalScheduleConflictError(Exception):
    def __init__(self, conflicts: tuple[FinalRoleConflict, ...]) -> None:
        super().__init__("duplicate final roles")
        self.conflicts = conflicts


@dataclass(frozen=True)
class FinalScheduleRow:
    hour: int
    is_recruitment: bool
    runner: str
    encore: str
    honso: tuple[str, str, str]
    standby: str


@dataclass(frozen=True)
class FinalSchedulePlan:
    rows: tuple[FinalScheduleRow, ...]
    split_colors: dict[str, str]

    @property
    def values(self) -> list[list[str]]:
        return [[row.runner, row.encore, *row.honso, row.standby] for row in self.rows]

    @property
    def runners(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(row.runner for row in self.rows if row.runner))


@dataclass(frozen=True)
class DraftScheduleInspection:
    rows: tuple[FinalScheduleRow, ...]
    issues: tuple[FinalScheduleValidationError, ...]


@dataclass(frozen=True)
class EventDayWritePlan:
    status: EventDayWriteStatus
    anchor: A1Cell | None = None
    value: str | None = None


@dataclass(frozen=True)
class ScheduleUpdateRequest:
    expected_hours: tuple[int, ...]
    recruitment_slots: frozenset[int]
    source_range: str
    main_anchor: A1Cell
    main_range: A1Rectangle
    event_day: EventDayWritePlan
    anchor_to_persist: str | None


_CELL_PATTERN = re.compile(r"([A-Z]+)([1-9][0-9]*)")
_EVENT_DAY_TOKENS = {
    "YYYY": lambda value: f"{value.year:04d}",
    "YY": lambda value: f"{value.year % 100:02d}",
    "M": lambda value: str(value.month),
    "MM": lambda value: f"{value.month:02d}",
    "MMM_en": lambda value: _ENGLISH_MONTHS[value.month - 1][:3],
    "MMMM_en": lambda value: _ENGLISH_MONTHS[value.month - 1],
    "D": lambda value: str(value.day),
    "DD": lambda value: f"{value.day:02d}",
    "Do_en": lambda value: _english_ordinal(value.day),
    "ddd_ja": lambda value: _JAPANESE_WEEKDAYS[value.weekday()],
    "dddd_ja": lambda value: f"{_JAPANESE_WEEKDAYS[value.weekday()]}曜日",
    "ddd_zh_tw": lambda value: _CHINESE_WEEKDAYS[value.weekday()],
    "dddd_zh_tw": lambda value: f"星期{_CHINESE_WEEKDAYS[value.weekday()]}",
    "ddd_en": lambda value: _ENGLISH_WEEKDAYS[value.weekday()][:3],
    "dddd_en": lambda value: _ENGLISH_WEEKDAYS[value.weekday()],
}
_ENGLISH_MONTHS = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)
_JAPANESE_WEEKDAYS = ("月", "火", "水", "木", "金", "土", "日")
_CHINESE_WEEKDAYS = ("一", "二", "三", "四", "五", "六", "日")
_ENGLISH_WEEKDAYS = (
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
)


def parse_a1_cell(value: str) -> A1Cell:
    if not isinstance(value, str):
        raise FinalScheduleInputError
    normalized = unicodedata.normalize("NFKC", value).strip().upper()
    if len(normalized) > FINAL_ANCHOR_MAX_LENGTH:
        raise FinalScheduleInputError
    match = _CELL_PATTERN.fullmatch(normalized)
    if match is None:
        raise FinalScheduleInputError
    column = _column_number(match.group(1))
    row = int(match.group(2))
    if column > GOOGLE_SHEETS_MAX_COLUMNS or row > GOOGLE_SHEETS_MAX_ROWS:
        raise FinalScheduleInputError
    return A1Cell(row=row, column=column, a1=normalized)


def parse_a1_range(value: str) -> A1Rectangle:
    if not isinstance(value, str):
        raise FinalScheduleInputError
    endpoints = unicodedata.normalize("NFKC", value).split(":")
    if len(endpoints) != 2:  # noqa: PLR2004
        raise FinalScheduleInputError
    start, end = (parse_a1_cell(endpoint) for endpoint in endpoints)
    if start.row > end.row or start.column > end.column:
        raise FinalScheduleInputError
    return A1Rectangle(start=start, end=end)


def find_final_schedule_data_range(
    grid: Sequence[Sequence[object]],
) -> A1Rectangle | None:
    min_row: int | None = None
    min_column: int | None = None
    max_row = 0
    max_column = 0

    for row, values in enumerate(grid, start=1):
        for column, value in enumerate(values, start=1):
            if value is None or value == "":
                continue
            min_row = row if min_row is None else min(min_row, row)
            min_column = column if min_column is None else min(min_column, column)
            max_row = max(max_row, row)
            max_column = max(max_column, column)

    if min_row is None or min_column is None:
        return None

    start = A1Cell(
        row=min_row,
        column=min_column,
        a1=f"{_column_label(min_column)}{min_row}",
    )
    end = A1Cell(
        row=max_row,
        column=max_column,
        a1=f"{_column_label(max_column)}{max_row}",
    )
    return A1Rectangle(start=start, end=end)


def format_event_day(value: date, pattern: str) -> str:
    if not isinstance(pattern, str):
        raise FinalScheduleInputError
    if not pattern:
        raise FinalScheduleInputError
    rendered: list[str] = []
    index = 0
    while index < len(pattern):
        character = pattern[index]
        if character == "{":
            if pattern.startswith("{{", index):
                rendered.append("{")
                index += 2
                continue
            end = pattern.find("}", index + 1)
            if end < 0:
                raise FinalScheduleInputError
            token = unicodedata.normalize("NFKC", pattern[index + 1 : end])
            formatter = _EVENT_DAY_TOKENS.get(token)
            if formatter is None:
                raise FinalScheduleInputError
            rendered.append(formatter(value))
            index = end + 1
            continue
        if character == "}":
            if pattern.startswith("}}", index):
                rendered.append("}")
                index += 2
                continue
            raise FinalScheduleInputError
        rendered.append(character)
        index += 1
    return "".join(rendered)


def build_schedule_update_request(  # noqa: PLR0913
    *,
    recruitment_ranges: RecruitmentTimeRanges,
    saved_anchor: str,
    supplied_anchor: str | None,
    event_date: date | None,
    event_day_anchor: str | None,
    event_day_format: str | None,
    # The six inputs are the command's DB-backed and per-run contract.
) -> ScheduleUpdateRequest:
    configured = recruitment_ranges.ranges.ranges
    if not configured:
        raise FinalScheduleInputError
    expected_hours = tuple(range(configured[0].start, configured[-1].end))
    if not expected_hours:
        raise FinalScheduleInputError

    saved = parse_a1_cell(saved_anchor)
    main_anchor = parse_a1_cell(
        saved_anchor if supplied_anchor is None else supplied_anchor
    )
    main_end = _offset_cell(main_anchor, rows=len(expected_hours) - 1, columns=5)
    main_range = A1Rectangle(main_anchor, main_end)
    event_day = _event_day_plan(
        main_range,
        event_date=event_date,
        event_day_anchor=event_day_anchor,
        event_day_format=event_day_format,
    )
    return ScheduleUpdateRequest(
        expected_hours=expected_hours,
        recruitment_slots=frozenset(recruitment_ranges.ranges.slots),
        source_range=f"B2:G{len(expected_hours) + 1}",
        main_anchor=main_anchor,
        main_range=main_range,
        event_day=event_day,
        anchor_to_persist=(
            main_anchor.a1
            if supplied_anchor is not None and main_anchor != saved
            else None
        ),
    )


def build_final_schedule(
    grid: Sequence[Sequence[object]],
    request: ScheduleUpdateRequest,
) -> FinalSchedulePlan:
    inspection = inspect_draft_schedule_rows(
        grid,
        expected_hours=request.expected_hours,
        recruitment_slots=request.recruitment_slots,
    )
    if inspection.issues:
        raise inspection.issues[0]

    rows = list(inspection.rows)
    conflicts = [
        conflict
        for row in rows
        for conflict in _row_conflicts(
            row.hour,
            (row.encore, *row.honso, row.standby),
        )
    ]
    if conflicts:
        raise FinalScheduleConflictError(tuple(conflicts))

    ordered_rows = _order_honso(rows)
    split_names = _split_names(ordered_rows)
    return FinalSchedulePlan(
        rows=tuple(ordered_rows),
        split_colors=_split_palette(split_names),
    )


def inspect_draft_schedule_rows(
    grid: Sequence[Sequence[object]],
    *,
    expected_hours: Sequence[int],
    recruitment_slots: frozenset[int],
) -> DraftScheduleInspection:
    if not grid:
        return DraftScheduleInspection(
            rows=(),
            issues=(
                FinalScheduleValidationError(
                    FinalScheduleValidationKind.EMPTY,
                    expected=tuple(DraftWorksheetContent.COLUMNS),
                    detected=None,
                ),
            ),
        )

    issues: list[FinalScheduleValidationError] = []
    if list(grid[0][:7]) != list(DraftWorksheetContent.COLUMNS):
        issues.append(
            FinalScheduleValidationError(
                FinalScheduleValidationKind.HEADER,
                row=1,
                column=1,
                expected=tuple(DraftWorksheetContent.COLUMNS),
                detected=tuple(grid[0][:7]),
            )
        )

    rows: list[FinalScheduleRow] = []
    for row_index, hour in enumerate(expected_hours, start=1):
        source_row = grid[row_index] if row_index < len(grid) else []
        detected_axis = source_row[0] if source_row else None
        if detected_axis != hour_label(hour):
            issues.append(
                FinalScheduleValidationError(
                    FinalScheduleValidationKind.AXIS,
                    row=row_index + 1,
                    column=1,
                    expected=hour_label(hour),
                    detected=detected_axis,
                )
            )

        values: list[str] = []
        for column in range(1, 7):
            value = source_row[column] if column < len(source_row) else ""
            if value is None or value == "":
                values.append("")
            elif isinstance(value, str):
                values.append(value)
            else:
                issues.append(
                    FinalScheduleValidationError(
                        FinalScheduleValidationKind.ROLE_VALUE,
                        row=row_index + 1,
                        column=column + 1,
                        expected=str,
                        detected=value,
                    )
                )
                values.append("")

        rows.append(
            FinalScheduleRow(
                hour=hour,
                is_recruitment=hour in recruitment_slots,
                runner=values[0],
                encore=values[1],
                honso=(values[2], values[3], values[4]),
                standby=values[5],
            )
        )

    next_row = len(expected_hours) + 1
    if next_row < len(grid):
        next_value = grid[next_row][0] if grid[next_row] else ""
        if next_value in ShiftParser.HOUR_LABELS:
            issues.append(
                FinalScheduleValidationError(
                    FinalScheduleValidationKind.EXTRA_AXIS,
                    row=next_row + 1,
                    column=1,
                    expected="",
                    detected=next_value,
                )
            )

    return DraftScheduleInspection(tuple(rows), tuple(issues))


def _row_conflicts(
    hour: int,
    values: tuple[str, str, str, str, str],
) -> list[FinalRoleConflict]:
    role_names = ("安可", "本走 1", "本走 2", "本走 3", "待機")
    roles_by_name: dict[str, list[str]] = {}
    for role, name in zip(role_names, values, strict=True):
        if name:
            roles_by_name.setdefault(name, []).append(role)
    return [
        FinalRoleConflict(hour=hour, name=name, roles=tuple(roles))
        for name, roles in roles_by_name.items()
        if len(roles) > 1
    ]


def _order_honso(rows: list[FinalScheduleRow]) -> list[FinalScheduleRow]:
    minimum_recruitment_rows = 2
    recruitment_indexes = [
        index for index, row in enumerate(rows) if row.is_recruitment
    ]
    if len(recruitment_indexes) < minimum_recruitment_rows:
        return rows

    states = [
        tuple(dict.fromkeys(it.permutations(rows[index].honso)))
        for index in recruitment_indexes
    ]
    costs: dict[tuple[str, str, str], tuple[tuple[object, ...], tuple[int, ...]]] = {
        state: (
            (
                0,
                0,
                _original_changes(state, rows[recruitment_indexes[0]].honso),
                (state_index,),
            ),
            (state_index,),
        )
        for state_index, state in enumerate(states[0])
    }
    for position in range(1, len(recruitment_indexes)):
        current_row = rows[recruitment_indexes[position]]
        next_costs: dict[
            tuple[str, str, str], tuple[tuple[object, ...], tuple[int, ...]]
        ] = {}
        for state_index, state in enumerate(states[position]):
            candidates = []
            for previous_state, (cost, path) in costs.items():
                transition = _honso_transition_cost(
                    previous_state,
                    state,
                    current_row.honso,
                )
                candidate_cost = (
                    cost[0] + transition[0],
                    cost[1] + transition[1],
                    cost[2] + transition[2],
                    (*path, state_index),
                )
                candidates.append((candidate_cost, (*path, state_index)))
            next_costs[state] = min(candidates, key=lambda item: item[0])
        costs = next_costs

    _, path = min(costs.values(), key=lambda item: item[0])
    for row_index, state_index, row_states in zip(
        recruitment_indexes,
        path,
        states,
        strict=True,
    ):
        row = rows[row_index]
        rows[row_index] = FinalScheduleRow(
            hour=row.hour,
            is_recruitment=row.is_recruitment,
            runner=row.runner,
            encore=row.encore,
            honso=row_states[state_index],
            standby=row.standby,
        )
    return rows


def _honso_transition_cost(
    previous: tuple[str, str, str],
    current: tuple[str, str, str],
    original: tuple[str, str, str],
) -> tuple[int, int, int]:
    changes = 0
    distance = 0
    for person in set(previous) & set(current) - {""}:
        previous_column = previous.index(person)
        current_column = current.index(person)
        if previous_column != current_column:
            changes += 1
        distance += abs(previous_column - current_column)
    return changes, distance, _original_changes(current, original)


def _original_changes(
    current: tuple[str, str, str],
    original: tuple[str, str, str],
) -> int:
    return sum(
        current_column != original_column
        for current_column, original_column in zip(current, original, strict=True)
        if current_column
    )


def _split_names(rows: list[FinalScheduleRow]) -> list[str]:
    appearances: dict[str, list[tuple[int, str]]] = {}
    for index, row in enumerate(rows):
        role_values = [
            ("encore", row.encore),
            *(("honso", value) for value in row.honso),
            ("standby", row.standby),
        ]
        for role, name in role_values:
            if name:
                appearances.setdefault(name, []).append((index, role))

    split_names: list[str] = []
    for name, person_appearances in appearances.items():
        for (previous_index, previous_role), (
            current_index,
            current_role,
        ) in it.pairwise(person_appearances):
            crosses_gap = any(
                not rows[index].is_recruitment
                for index in range(previous_index, current_index + 1)
            )
            if (
                previous_role != current_role
                or current_index != previous_index + 1
                or crosses_gap
            ):
                split_names.append(name)
                break
    return split_names


def _split_palette(names: list[str]) -> dict[str, str]:
    if not names:
        return {}
    hues = [35 + index * 360 / len(names) for index in range(len(names))]
    assigned = {names[0]: hues[0]}
    for name in names[1:]:
        candidate = max(
            (hue for hue in hues if hue not in assigned.values()),
            key=lambda hue: (
                min(
                    _hue_distance(hue, assigned_hue)
                    for assigned_hue in assigned.values()
                ),
                -hues.index(hue),
            ),
        )
        assigned[name] = candidate
    return {name: _hsl_hex(hue) for name, hue in assigned.items()}


def _hue_distance(first: float, second: float) -> float:
    difference = abs(first - second) % 360
    return min(difference, 360 - difference)


def _hsl_hex(hue: float) -> str:
    red, green, blue = colorsys.hls_to_rgb(hue / 360, 0.86, 0.45)
    red, green, blue = (round(channel * 255) for channel in (red, green, blue))
    return f"#{red:02X}{green:02X}{blue:02X}"


def _event_day_plan(
    main_range: A1Rectangle,
    *,
    event_date: date | None,
    event_day_anchor: str | None,
    event_day_format: str | None,
) -> EventDayWritePlan:
    if event_day_anchor is None:
        return EventDayWritePlan(
            EventDayWriteStatus.FORMAT_IGNORED
            if event_day_format is not None
            else EventDayWriteStatus.OMITTED
        )
    try:
        anchor = parse_a1_cell(event_day_anchor)
    except ValueError:
        return EventDayWritePlan(EventDayWriteStatus.INVALID_ANCHOR)
    if main_range.contains(anchor):
        return EventDayWritePlan(EventDayWriteStatus.OVERLAPS_MAIN, anchor=anchor)
    if event_date is None:
        return EventDayWritePlan(EventDayWriteStatus.MISSING_EVENT_DATE, anchor=anchor)
    pattern = DEFAULT_EVENT_DAY_FORMAT if event_day_format is None else event_day_format
    try:
        value = format_event_day(event_date, pattern)
    except ValueError:
        return EventDayWritePlan(EventDayWriteStatus.INVALID_FORMAT, anchor=anchor)
    return EventDayWritePlan(
        EventDayWriteStatus.READY,
        anchor=anchor,
        value=value,
    )


def _offset_cell(cell: A1Cell, *, rows: int, columns: int) -> A1Cell:
    row = cell.row + rows
    column = cell.column + columns
    if row > GOOGLE_SHEETS_MAX_ROWS or column > GOOGLE_SHEETS_MAX_COLUMNS:
        raise FinalScheduleInputError
    a1 = f"{_column_label(column)}{row}"
    return A1Cell(row=row, column=column, a1=a1)


def _column_number(label: str) -> int:
    value = 0
    for character in label:
        value = value * 26 + ord(character) - ord("A") + 1
    return value


def _column_label(column: int) -> str:
    result: list[str] = []
    while column:
        column, remainder = divmod(column - 1, 26)
        result.append(chr(ord("A") + remainder))
    return "".join(reversed(result))


def _english_ordinal(day: int) -> str:
    ordinal_teens_start = 10
    ordinal_teens_end = 14
    suffix = (
        "th"
        if ordinal_teens_start < day % 100 < ordinal_teens_end
        else {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")
    )
    return f"{day}{suffix}"
