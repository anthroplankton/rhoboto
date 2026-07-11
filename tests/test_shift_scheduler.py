from __future__ import annotations

from typing import TYPE_CHECKING

from utils import shift_scheduler
from utils.shift_register_structs import Shift
from utils.shift_scheduler import (
    ENCORE_SUPPORTER_SLOT,
    HONSO_SUPPORTER_SLOTS,
    STANDBY_SUPPORTER_SLOT,
    SUPPORTER_CAPACITY,
    ShiftScheduler,
)

if TYPE_CHECKING:
    from collections.abc import Iterable


def make_shift(
    username: str,
    slots: Iterable[int],
    *,
    display_name: str | None = None,
) -> Shift:
    return Shift(
        username=username,
        display_name=display_name if display_name is not None else username,
        original_message="",
        slots=set(slots),
    )


def test_supporter_slot_domain_names() -> None:
    assert shift_scheduler.ENCORE_SUPPORTER_SLOT == "encore"
    assert shift_scheduler.HONSO_SUPPORTER_SLOTS == (
        "honso_1",
        "honso_2",
        "honso_3",
    )
    assert shift_scheduler.STANDBY_SUPPORTER_SLOT == "standby"


def test_only_available_people_are_scheduled() -> None:
    schedule = ShiftScheduler.assign([make_shift("a", {4})], [4, 5])

    hour_four, hour_five = schedule.assignments
    assert hour_four.supporter_usernames_by_slot[ENCORE_SUPPORTER_SLOT] == "a"
    assert hour_five.supporter_usernames_by_slot == {}
    assert hour_five.shortage == SUPPORTER_CAPACITY


def test_runner_is_excluded_from_seats_and_recorded() -> None:
    shifts = [
        make_shift("runnerguy", {4}, display_name="Runner"),
        make_shift("a", {4}),
    ]

    schedule = ShiftScheduler.assign(shifts, [4], runner="Runner")

    assert schedule.runner == "Runner"
    seats = schedule.assignments[0].supporter_usernames_by_slot
    assert "runnerguy" not in seats.values()
    assert seats[ENCORE_SUPPORTER_SLOT] == "a"


def test_fills_five_seats_and_benches_surplus() -> None:
    shifts = [make_shift(name, {4}) for name in ("a", "b", "c", "d", "e", "f")]

    schedule = ShiftScheduler.assign(shifts, [4])

    assignment = schedule.assignments[0]
    assert assignment.filled == SUPPORTER_CAPACITY
    assert len(assignment.unassigned_usernames) == 1
    assert assignment.shortage == 0


def test_priority_slots_fill_before_standby_when_short() -> None:
    shifts = [make_shift(name, {4}) for name in ("a", "b", "c")]

    schedule = ShiftScheduler.assign(shifts, [4])

    seats = schedule.assignments[0].supporter_usernames_by_slot
    assert ENCORE_SUPPORTER_SLOT in seats
    assert HONSO_SUPPORTER_SLOTS[0] in seats
    assert HONSO_SUPPORTER_SLOTS[1] in seats
    assert HONSO_SUPPORTER_SLOTS[2] not in seats
    assert STANDBY_SUPPORTER_SLOT not in seats
    assert schedule.assignments[0].shortage == 2


def test_continuity_keeps_same_person_in_same_slot() -> None:
    shifts = [make_shift("p", {4, 5}), make_shift("q", {4, 5})]

    schedule = ShiftScheduler.assign(shifts, [4, 5])

    hour_four = schedule.assignments[0].supporter_usernames_by_slot
    hour_five = schedule.assignments[1].supporter_usernames_by_slot
    for supporter_slot in (ENCORE_SUPPORTER_SLOT, HONSO_SUPPORTER_SLOTS[0]):
        assert hour_four[supporter_slot] == hour_five[supporter_slot]


def test_fill_prefers_least_loaded_person() -> None:
    shifts = [
        make_shift("a", {4, 6}),
        make_shift("b", {5, 6}),
        make_shift("c", {6}),
    ]

    schedule = ShiftScheduler.assign(shifts, [4, 5, 6])

    hour_six = schedule.assignments[2].supporter_usernames_by_slot
    # b keeps the encore slot (held it at hour 5); of the remaining, c has the
    # lighter load (0 vs a's 1) so it takes the higher-priority 本走 slot.
    assert hour_six[ENCORE_SUPPORTER_SLOT] == "b"
    assert hour_six[HONSO_SUPPORTER_SLOTS[0]] == "c"
    assert hour_six[HONSO_SUPPORTER_SLOTS[1]] == "a"


def test_assignment_is_deterministic() -> None:
    shifts = [make_shift(name, {4, 5, 6}) for name in "abcdefg"]

    first = ShiftScheduler.assign(shifts, [4, 5, 6])
    second = ShiftScheduler.assign(shifts, [4, 5, 6])

    assert [a.supporter_usernames_by_slot for a in first.assignments] == [
        a.supporter_usernames_by_slot for a in second.assignments
    ]


def test_shortage_and_unassigned_labels() -> None:
    shifts = [make_shift(name, {4}) for name in ("a", "b", "c", "d", "e", "f")]

    schedule = ShiftScheduler.assign(shifts, [4, 5])

    # Hour 4 seats all five and benches one; hour 5 has nobody.
    assert schedule.total_shortage == SUPPORTER_CAPACITY
    assert any(label.startswith("4-5:") for label in schedule.unassigned_labels())
    assert any(label.startswith("5-6") for label in schedule.shortage_labels())
