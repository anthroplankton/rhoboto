from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

    from utils.shift_register_structs import Shift
    from utils.structs_base import UserInfo

ENCORE_SUPPORTER_SLOT = "encore"
HONSO_SUPPORTER_SLOTS: tuple[str, str, str] = ("honso_1", "honso_2", "honso_3")
STANDBY_SUPPORTER_SLOT = "standby"

# Stable supporter-slot order used for history, rendering, and shortage counts.
SUPPORTER_SLOT_PRIORITY: tuple[str, ...] = (
    ENCORE_SUPPORTER_SLOT,
    *HONSO_SUPPORTER_SLOTS,
    STANDBY_SUPPORTER_SLOT,
)
SUPPORTER_CAPACITY = len(SUPPORTER_SLOT_PRIORITY)
MAIN_SUPPORTER_CAPACITY = len(HONSO_SUPPORTER_SLOTS) + 1
DRAFT_USERNAME_SUFFIX_PATTERN = re.compile(r"⟨@([a-z0-9._]{2,32})⟩$")


def hour_label(hour: int) -> str:
    """Return the 30-hour slot label for a slot index (e.g. ``4`` -> ``"4-5"``)."""
    return f"{hour}-{hour + 1}"


@dataclass(frozen=True)
class DraftTeamProfile:
    """Team values used only for Shift Draft scheduling."""

    main_isv: float | None
    main_power: float | None
    encore_isv: float | None = None
    encore_power: float | None = None
    has_encore_role: bool = False

    @property
    def has_encore_team(self) -> bool:
        """Whether either Encore Team value is present."""
        return self.encore_isv is not None or self.encore_power is not None

    def encore_isv_above(self, power_threshold: float) -> float | None:
        """Return effective Encore ISV when role and Power permit Encore."""
        if not self.has_encore_role:
            return None
        if self.has_encore_team:
            isv, power = self.encore_isv, self.encore_power
        else:
            isv, power = self.main_isv, self.main_power
        if isv is None or power is None or power <= power_threshold:
            return None
        return isv


def build_draft_display_names(
    shifts: Sequence[Shift],
    *,
    runner: UserInfo | None = None,
) -> dict[str, str]:
    """Return reversible Draft names keyed by Discord username."""
    identities_by_username = {shift.username: shift for shift in shifts}
    if runner is not None:
        identities_by_username[runner.username] = runner
    identities = list(identities_by_username.values())
    counts = Counter(identity.display_name for identity in identities)
    return {
        identity.username: (
            f"{identity.display_name} ⟨@{identity.username}⟩"
            if counts[identity.display_name] > 1
            or DRAFT_USERNAME_SUFFIX_PATTERN.search(identity.display_name)
            else identity.display_name
        )
        for identity in identities
    }


def _encore_rank(
    isv: float,
    previous_slot: str | None,
    load: int,
    total_availability: int,
    username: str,
) -> tuple[float, int, int, int, str]:
    continuity = (
        0
        if previous_slot == ENCORE_SUPPORTER_SLOT
        else 1
        if previous_slot is not None
        else 2
    )
    return (-isv, continuity, load, total_availability, username)


def _main_rank(
    isv: float | None,
    load: int,
    total_availability: int,
    username: str,
    *,
    was_scheduled: bool,
) -> tuple[bool, float, bool, int, int, str]:
    return (
        isv is None,
        -(isv or 0),
        not was_scheduled,
        load,
        total_availability,
        username,
    )


def _standby_rank(
    isv: float | None,
    previous_slot: str | None,
    load: int,
    username: str,
) -> tuple[bool, float, int, int, str]:
    continuity = (
        0
        if previous_slot == STANDBY_SUPPORTER_SLOT
        else 1
        if previous_slot is not None
        else 2
    )
    return (isv is not None, isv or 0, continuity, load, username)


def _honso_placement_rank(
    main_rank: tuple[bool, float, bool, int, int, str],
    previous_slot: str | None,
) -> tuple[bool, float, int, int, int, str]:
    continuity = (
        0
        if previous_slot in HONSO_SUPPORTER_SLOTS
        else 1
        if previous_slot is not None
        else 2
    )
    return (*main_rank[:2], continuity, *main_rank[3:])


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
    def assign(  # noqa: C901, PLR0912, PLR0915
        shifts: Iterable[Shift],
        hours: Sequence[int],
        *,
        team_profiles: Mapping[str, DraftTeamProfile] | None = None,
        encore_power_threshold: float = 0,
        runner: UserInfo | None = None,
    ) -> DraftSchedule:
        """Build a draft schedule from availability.

        Only people available in a given hour are eligible for that hour. The
        runner is pinned separately and never competes for a supporter
        slot. Effective ISV ranks candidates first; continuity, accumulated load,
        availability, and username break ties in that order. Encore remains empty
        without an eligible Team profile.

        Args:
            shifts (Iterable[Shift]): Availability, one entry per person.
            hours (Sequence[int]): Recruitment hour slots to schedule, in order.
            team_profiles (Mapping[str, DraftTeamProfile] | None): Team values by
                username. Missing profiles rank after known Main ISV values.
            encore_power_threshold (float): Strict minimum Power for Encore.
            runner (UserInfo | None): Discord identity to pin to every hour.

        Returns:
            DraftSchedule: The resulting per-hour assignments.
        """
        profiles = team_profiles or {}
        all_shifts = list(shifts)
        canonical_display_names = build_draft_display_names(
            all_shifts,
            runner=runner,
        )
        candidates = [
            shift
            for shift in all_shifts
            if runner is None or shift.username != runner.username
        ]
        display_names = {
            shift.username: canonical_display_names[shift.username]
            for shift in candidates
        }
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
        previous_hour: int | None = None
        for hour in hours:
            if previous_hour is not None and hour != previous_hour + 1:
                previous_supporters_by_slot = dict.fromkeys(
                    SUPPORTER_SLOT_PRIORITY,
                    None,
                )
            available = [shift for shift in candidates if hour in shift]
            supporter_usernames_by_slot: dict[str, str] = {}
            used: set[str] = set()
            previous_slot_by_username = {
                username: slot
                for slot, username in previous_supporters_by_slot.items()
                if username is not None
            }

            ranked_encore: list[tuple[tuple[float, int, int, int, str], Shift]] = []
            main_ranks: dict[str, tuple[bool, float, bool, int, int, str]] = {}
            for shift in available:
                profile = profiles.get(shift.username)
                isv = (
                    profile.encore_isv_above(encore_power_threshold)
                    if profile is not None
                    else None
                )
                if isv is not None:
                    ranked_encore.append(
                        (
                            _encore_rank(
                                isv,
                                previous_slot_by_username.get(shift.username),
                                load[shift.username],
                                total_availability[shift.username],
                                shift.username,
                            ),
                            shift,
                        )
                    )
                main_ranks[shift.username] = _main_rank(
                    profile.main_isv if profile is not None else None,
                    load[shift.username],
                    total_availability[shift.username],
                    shift.username,
                    was_scheduled=shift.username in previous_slot_by_username,
                )
            if ranked_encore:
                encore = min(ranked_encore, key=lambda item: item[0])[1]
                supporter_usernames_by_slot[ENCORE_SUPPORTER_SLOT] = encore.username
                used.add(encore.username)

            ranked_main = sorted(
                (main_ranks[shift.username], shift)
                for shift in available
                if shift.username not in used
            )
            selected_main = [
                shift for _, shift in ranked_main[:MAIN_SUPPORTER_CAPACITY]
            ]
            standby: Shift | None = None
            if len(selected_main) == MAIN_SUPPORTER_CAPACITY:
                ranked_standby = []
                for shift in selected_main:
                    profile = profiles.get(shift.username)
                    ranked_standby.append(
                        (
                            _standby_rank(
                                profile.main_isv if profile is not None else None,
                                previous_slot_by_username.get(shift.username),
                                load[shift.username],
                                shift.username,
                            ),
                            shift,
                        )
                    )
                standby = min(ranked_standby, key=lambda item: item[0])[1]
                selected_main.remove(standby)

            remaining_honso = {shift.username: shift for shift in selected_main}
            for slot in HONSO_SUPPORTER_SLOTS:
                holder = previous_supporters_by_slot[slot]
                if holder in remaining_honso:
                    supporter_usernames_by_slot[slot] = holder
                    used.add(holder)
                    del remaining_honso[holder]

            remaining = iter(
                shift
                for _, shift in sorted(
                    (
                        _honso_placement_rank(
                            main_ranks[shift.username],
                            previous_slot_by_username.get(shift.username),
                        ),
                        shift,
                    )
                    for shift in remaining_honso.values()
                )
            )
            for slot in HONSO_SUPPORTER_SLOTS:
                if slot in supporter_usernames_by_slot:
                    continue
                chosen = next(remaining, None)
                if chosen is None:
                    break
                supporter_usernames_by_slot[slot] = chosen.username
                used.add(chosen.username)

            if standby is not None:
                supporter_usernames_by_slot[STANDBY_SUPPORTER_SLOT] = standby.username
                used.add(standby.username)

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
            previous_hour = hour

        return DraftSchedule(
            runner=(canonical_display_names[runner.username] if runner else None),
            hours=list(hours),
            assignments=assignments,
            display_names=display_names,
        )
