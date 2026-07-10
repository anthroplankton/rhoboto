from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from utils.shift_register_structs import Shift

ENCORE_LANE = "encore"
HASHIRI_LANES: tuple[str, str, str] = ("hashiri_1", "hashiri_2", "hashiri_3")
STANDBY_LANE = "standby"

# Fill priority for the non-runner seats each hour: Encore first, then the three
# main (本走) seats, then standby. When fewer people are available than seats, the
# lower-priority lanes (standby first) are the ones that stay empty.
PRIORITY_LANES: tuple[str, ...] = (ENCORE_LANE, *HASHIRI_LANES, STANDBY_LANE)
NON_RUNNER_CAPACITY = len(PRIORITY_LANES)


def hour_label(hour: int) -> str:
    """Return the 30-hour slot label for a slot index (e.g. ``4`` -> ``"4-5"``)."""
    return f"{hour}-{hour + 1}"


@dataclass
class HourShiftAssignment:
    """The people assigned to each lane for a single recruitment hour.

    Attributes:
        hour (int): The 30-hour slot index this assignment covers.
        lane_usernames (dict[str, str]): Lane key -> assigned username. Lanes that
            could not be filled are absent from the mapping.
        unassigned_usernames (list[str]): Usernames that were available this hour
            but had no seat left (more people than seats).
    """

    hour: int
    lane_usernames: dict[str, str] = field(default_factory=dict)
    unassigned_usernames: list[str] = field(default_factory=list)

    @property
    def filled(self) -> int:
        """Number of seats actually filled this hour."""
        return len(self.lane_usernames)

    @property
    def shortage(self) -> int:
        """Number of non-runner seats left empty this hour."""
        return NON_RUNNER_CAPACITY - self.filled


@dataclass
class DraftSchedule:
    """A full draft schedule produced from shift entries.

    Attributes:
        runner (str | None): The runner nickname pinned to every hour, or None.
        hours (list[int]): The recruitment hour slots covered, in order.
        assignments (list[HourShiftAssignment]): Per-hour lane assignments.
        display_names (dict[str, str]): Username -> display name for the pool.
    """

    runner: str | None
    hours: list[int]
    assignments: list[HourShiftAssignment]
    display_names: dict[str, str]

    def display_for(self, assignment: HourShiftAssignment, lane: str) -> str:
        """Return the display name filling ``lane`` this hour, or ``""`` if empty."""
        username = assignment.lane_usernames.get(lane)
        if username is None:
            return ""
        return self.display_names.get(username, username)

    @property
    def total_shortage(self) -> int:
        """Total number of empty non-runner seats across all hours."""
        return sum(assignment.shortage for assignment in self.assignments)

    def shortage_labels(self) -> list[str]:
        """Return ``"<hour> (缺 <n>)"`` labels for hours with empty seats."""
        return [
            f"{hour_label(assignment.hour)} (缺 {assignment.shortage})"
            for assignment in self.assignments
            if assignment.shortage
        ]

    def unassigned_labels(self) -> list[str]:
        """Return ``"<hour>: name, name"`` labels for hours with unseated people."""
        labels: list[str] = []
        for assignment in self.assignments:
            if not assignment.unassigned_usernames:
                continue
            names = "、".join(
                self.display_names.get(username, username)
                for username in assignment.unassigned_usernames
            )
            labels.append(f"{hour_label(assignment.hour)}: {names}")
        return labels


class ShiftScheduler:
    """Assign shift entries into runner/encore/main/standby lanes per hour."""

    @staticmethod
    def assign(
        shifts: Iterable[Shift],
        hours: Sequence[int],
        *,
        runner: str | None = None,
    ) -> DraftSchedule:
        """Build a draft schedule from availability.

        Only people available in a given hour are eligible for that hour. The
        runner nickname is pinned to its own lane and never competes for a seat.
        For each hour, whoever held a lane the previous hour keeps it when still
        available (continuity), then remaining lanes are filled least-loaded (and
        scarcest) first so total hours stay balanced. Ties break on username, so
        the same input always yields the same schedule.

        Args:
            shifts (Iterable[Shift]): Availability, one entry per person.
            hours (Sequence[int]): Recruitment hour slots to schedule, in order.
            runner (str | None): Runner nickname to pin to every hour.

        Returns:
            DraftSchedule: The resulting per-hour assignments.
        """
        candidates = [shift for shift in shifts if shift.display_name != runner]
        display_names = {shift.username: shift.display_name for shift in candidates}
        total_availability = {
            shift.username: sum(1 for hour in hours if hour in shift)
            for shift in candidates
        }
        load = dict.fromkeys(display_names, 0)
        previous: dict[str, str | None] = dict.fromkeys(PRIORITY_LANES, None)

        assignments: list[HourShiftAssignment] = []
        for hour in hours:
            available = [shift for shift in candidates if hour in shift]
            available_usernames = {shift.username for shift in available}
            active_lanes = PRIORITY_LANES[: min(len(available), NON_RUNNER_CAPACITY)]

            lane_usernames: dict[str, str] = {}
            used: set[str] = set()

            # 1) Continuity: keep last hour's holder when still available.
            for lane in active_lanes:
                holder = previous[lane]
                if holder in available_usernames and holder not in used:
                    lane_usernames[lane] = holder
                    used.add(holder)

            # 2) Fill remaining lanes, least-loaded then scarcest first.
            remaining = sorted(
                (shift for shift in available if shift.username not in used),
                key=lambda shift: (
                    load[shift.username],
                    total_availability[shift.username],
                    shift.username,
                ),
            )
            fill = iter(remaining)
            for lane in active_lanes:
                if lane not in lane_usernames:
                    chosen = next(fill)
                    lane_usernames[lane] = chosen.username
                    used.add(chosen.username)

            # 3) Update load and lane history for the next hour.
            for lane in PRIORITY_LANES:
                holder = lane_usernames.get(lane)
                previous[lane] = holder
                if holder is not None:
                    load[holder] += 1

            unassigned = [
                shift.username for shift in available if shift.username not in used
            ]
            assignments.append(HourShiftAssignment(hour, lane_usernames, unassigned))

        return DraftSchedule(
            runner=runner,
            hours=list(hours),
            assignments=assignments,
            display_names=display_names,
        )
