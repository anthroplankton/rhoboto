from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from utils.shift_register_structs import Shift

ENCORE_SUPPORTER_SLOT = "encore"
HONSO_SUPPORTER_SLOTS: tuple[str, str, str] = ("honso_1", "honso_2", "honso_3")
STANDBY_SUPPORTER_SLOT = "standby"

# Fill priority for the supporter slots each hour: Encore first, then the three
# 本走 slots, then standby. When fewer people are available than slots, the
# lower-priority slots (standby first) are the ones that stay empty.
SUPPORTER_SLOT_PRIORITY: tuple[str, ...] = (
    ENCORE_SUPPORTER_SLOT,
    *HONSO_SUPPORTER_SLOTS,
    STANDBY_SUPPORTER_SLOT,
)
SUPPORTER_CAPACITY = len(SUPPORTER_SLOT_PRIORITY)


def hour_label(hour: int) -> str:
    """Return the 30-hour slot label for a slot index (e.g. ``4`` -> ``"4-5"``)."""
    return f"{hour}-{hour + 1}"


@dataclass
class HourShiftAssignment:
    """The supporters assigned to each slot for a single recruitment hour.

    Attributes:
        hour (int): The 30-hour slot index this assignment covers.
        supporter_usernames_by_slot (dict[str, str]): Supporter slot -> assigned
            username. Slots that could not be filled are absent from the mapping.
        unassigned_usernames (list[str]): Usernames that were available this hour
            but had no seat left (more people than seats).
    """

    hour: int
    supporter_usernames_by_slot: dict[str, str] = field(default_factory=dict)
    unassigned_usernames: list[str] = field(default_factory=list)

    @property
    def filled(self) -> int:
        """Number of seats actually filled this hour."""
        return len(self.supporter_usernames_by_slot)

    @property
    def shortage(self) -> int:
        """Number of non-runner seats left empty this hour."""
        return SUPPORTER_CAPACITY - self.filled


@dataclass
class DraftSchedule:
    """A full draft schedule produced from shift entries.

    Attributes:
        runner (str | None): The runner nickname pinned to every hour, or None.
        hours (list[int]): The recruitment hour slots covered, in order.
        assignments (list[HourShiftAssignment]): Per-hour supporter assignments.
        display_names (dict[str, str]): Username -> display name for the pool.
    """

    runner: str | None
    hours: list[int]
    assignments: list[HourShiftAssignment]
    display_names: dict[str, str]

    def display_for(
        self,
        assignment: HourShiftAssignment,
        supporter_slot: str,
    ) -> str:
        """Return the display name in ``supporter_slot``, or ``""`` if empty."""
        username = assignment.supporter_usernames_by_slot.get(supporter_slot)
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
    """Assign entries into runner and supporter slots for each hour."""

    @staticmethod
    def assign(
        shifts: Iterable[Shift],
        hours: Sequence[int],
        *,
        runner: str | None = None,
    ) -> DraftSchedule:
        """Build a draft schedule from availability.

        Only people available in a given hour are eligible for that hour. The
        runner nickname is pinned separately and never competes for a supporter
        slot. For each hour, whoever held a slot the previous hour keeps it when still
        available (continuity), then remaining slots are filled least-loaded (and
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
        previous_supporters_by_slot: dict[str, str | None] = dict.fromkeys(
            SUPPORTER_SLOT_PRIORITY,
            None,
        )

        assignments: list[HourShiftAssignment] = []
        for hour in hours:
            available = [shift for shift in candidates if hour in shift]
            available_usernames = {shift.username for shift in available}
            fillable_supporter_slots = SUPPORTER_SLOT_PRIORITY[
                : min(len(available), SUPPORTER_CAPACITY)
            ]

            supporter_usernames_by_slot: dict[str, str] = {}
            used: set[str] = set()

            # 1) Continuity: keep last hour's holder when still available.
            for supporter_slot in fillable_supporter_slots:
                holder = previous_supporters_by_slot[supporter_slot]
                if holder in available_usernames and holder not in used:
                    supporter_usernames_by_slot[supporter_slot] = holder
                    used.add(holder)

            # 2) Fill remaining slots, least-loaded then scarcest first.
            remaining = sorted(
                (shift for shift in available if shift.username not in used),
                key=lambda shift: (
                    load[shift.username],
                    total_availability[shift.username],
                    shift.username,
                ),
            )
            fill = iter(remaining)
            for supporter_slot in fillable_supporter_slots:
                if supporter_slot not in supporter_usernames_by_slot:
                    chosen = next(fill)
                    supporter_usernames_by_slot[supporter_slot] = chosen.username
                    used.add(chosen.username)

            # 3) Update load and slot history for the next hour.
            for supporter_slot in SUPPORTER_SLOT_PRIORITY:
                holder = supporter_usernames_by_slot.get(supporter_slot)
                previous_supporters_by_slot[supporter_slot] = holder
                if holder is not None:
                    load[holder] += 1

            unassigned = [
                shift.username for shift in available if shift.username not in used
            ]
            assignments.append(
                HourShiftAssignment(hour, supporter_usernames_by_slot, unassigned)
            )

        return DraftSchedule(
            runner=runner,
            hours=list(hours),
            assignments=assignments,
            display_names=display_names,
        )
