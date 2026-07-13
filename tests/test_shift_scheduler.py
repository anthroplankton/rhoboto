from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from utils import shift_scheduler
from utils.shift_register_structs import Shift
from utils.shift_scheduler import (
    DRAFT_USERNAME_SUFFIX_PATTERN,
    ENCORE_SUPPORTER_SLOT,
    HONSO_SUPPORTER_SLOTS,
    STANDBY_SUPPORTER_SLOT,
    SUPPORTER_CAPACITY,
    DraftTeamProfile,
    ShiftScheduler,
    build_draft_display_names,
)
from utils.structs_base import UserInfo

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


def make_profile(
    *,
    main_isv: float | None,
    main_power: float | None = 40,
    encore_isv: float | None = None,
    encore_power: float | None = None,
    has_encore_role: bool = False,
) -> DraftTeamProfile:
    return DraftTeamProfile(
        main_isv=main_isv,
        main_power=main_power,
        encore_isv=encore_isv,
        encore_power=encore_power,
        has_encore_role=has_encore_role,
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
    assert hour_four.supporter_usernames_by_slot[HONSO_SUPPORTER_SLOTS[0]] == "a"
    assert ENCORE_SUPPORTER_SLOT not in hour_four.supporter_usernames_by_slot
    assert hour_five.supporter_usernames_by_slot == {}
    assert hour_five.shortage == SUPPORTER_CAPACITY


def test_runner_is_excluded_from_seats_and_recorded() -> None:
    shifts = [
        make_shift("runnerguy", {4}, display_name="Runner"),
        make_shift("a", {4}),
    ]

    schedule = ShiftScheduler.assign(
        shifts,
        [4],
        runner=UserInfo(username="runnerguy", display_name="Runner"),
    )

    assert schedule.runner == "Runner"
    seats = schedule.assignments[0].supporter_usernames_by_slot
    assert "runnerguy" not in seats.values()
    assert seats[HONSO_SUPPORTER_SLOTS[0]] == "a"


def test_runner_user_is_excluded_by_username_and_rendered_canonically() -> None:
    runner = UserInfo(username="runner_user", display_name="Alice")
    shifts = [make_shift("runner_user", {4}, display_name="Alice")]

    schedule = ShiftScheduler.assign(shifts, [4], runner=runner)

    assert schedule.runner == "Alice"
    assert schedule.assignments[0].supporter_usernames_by_slot == {}


def test_external_runner_shares_canonical_name_scope_with_entry_users() -> None:
    runner = UserInfo(username="runner_user", display_name="Alice")
    schedule = ShiftScheduler.assign(
        [make_shift("alice_user", {4}, display_name="Alice")],
        [4],
        runner=runner,
    )

    assert schedule.runner == "Alice ⟨@runner_user⟩"
    assert schedule.display_names == {"alice_user": "Alice ⟨@alice_user⟩"}


def test_without_encore_profiles_fills_four_main_seats_and_benches_surplus() -> None:
    shifts = [make_shift(name, {4}) for name in ("a", "b", "c", "d", "e", "f")]

    schedule = ShiftScheduler.assign(shifts, [4])

    assignment = schedule.assignments[0]
    assert assignment.filled == 4
    assert len(assignment.unassigned_usernames) == 2
    assert assignment.shortage == 1


def test_priority_slots_fill_before_standby_when_short() -> None:
    shifts = [make_shift(name, {4}) for name in ("a", "b", "c")]

    schedule = ShiftScheduler.assign(shifts, [4])

    seats = schedule.assignments[0].supporter_usernames_by_slot
    assert ENCORE_SUPPORTER_SLOT not in seats
    assert set(HONSO_SUPPORTER_SLOTS) <= seats.keys()
    assert STANDBY_SUPPORTER_SLOT not in seats
    assert schedule.assignments[0].shortage == 2


def test_continuity_keeps_same_person_in_same_slot() -> None:
    shifts = [make_shift("p", {4, 5}), make_shift("q", {4, 5})]

    schedule = ShiftScheduler.assign(shifts, [4, 5])

    hour_four = schedule.assignments[0].supporter_usernames_by_slot
    hour_five = schedule.assignments[1].supporter_usernames_by_slot
    for supporter_slot in HONSO_SUPPORTER_SLOTS[:2]:
        assert hour_four[supporter_slot] == hour_five[supporter_slot]


def test_fill_prefers_least_loaded_person() -> None:
    shifts = [
        make_shift("a", {4, 6}),
        make_shift("b", {5, 6}),
        make_shift("c", {6}),
    ]

    schedule = ShiftScheduler.assign(shifts, [4, 5, 6])

    hour_six = schedule.assignments[2].supporter_usernames_by_slot
    # b keeps 本走① from hour 5; c has the lighter load than a.
    assert hour_six[HONSO_SUPPORTER_SLOTS[0]] == "b"
    assert hour_six[HONSO_SUPPORTER_SLOTS[1]] == "c"
    assert hour_six[HONSO_SUPPORTER_SLOTS[2]] == "a"


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

    # Hour 4 seats four without an eligible Encore and benches two; hour 5 is empty.
    assert schedule.total_shortage == SUPPORTER_CAPACITY + 1
    assert any(label.startswith("4-5:") for label in schedule.unassigned_labels())
    assert any(label.startswith("5-6") for label in schedule.shortage_labels())


@pytest.mark.parametrize(
    ("profile", "threshold", "expected"),
    [
        (make_profile(main_isv=200, main_power=40), 35, None),
        (
            make_profile(
                main_isv=200,
                main_power=40,
                encore_isv=250,
                encore_power=50,
            ),
            35,
            None,
        ),
        (
            make_profile(
                main_isv=200,
                main_power=40,
                has_encore_role=True,
            ),
            40,
            None,
        ),
        (
            make_profile(
                main_isv=200,
                main_power=40,
                has_encore_role=True,
            ),
            35,
            200,
        ),
        (
            make_profile(
                main_isv=200,
                main_power=40,
                encore_isv=250,
                encore_power=50,
                has_encore_role=True,
            ),
            45,
            250,
        ),
        (
            make_profile(
                main_isv=200,
                main_power=40,
                encore_isv=250,
                encore_power=None,
                has_encore_role=True,
            ),
            35,
            None,
        ),
    ],
)
def test_encore_isv_requires_role_complete_team_values_and_strict_power(
    profile: DraftTeamProfile,
    threshold: float,
    expected: float | None,
) -> None:
    assert profile.encore_isv_above(threshold) == expected


def test_build_draft_display_names_reserves_username_suffix() -> None:
    shifts = [
        make_shift("alice_one", {4}, display_name="Alice"),
        make_shift("alice_two", {4}, display_name="Alice"),
        make_shift(
            "alice_three",
            {4},
            display_name="Alice ⟨@alice_one⟩",
        ),
        make_shift("bob", {4}, display_name="Bob"),
    ]

    assert build_draft_display_names(shifts) == {
        "alice_one": "Alice ⟨@alice_one⟩",
        "alice_two": "Alice ⟨@alice_two⟩",
        "alice_three": "Alice ⟨@alice_one⟩ ⟨@alice_three⟩",
        "bob": "Bob",
    }
    assert DRAFT_USERNAME_SUFFIX_PATTERN.search("Alice ⟨@alice_one⟩")


def test_scheduler_uses_encore_isv_then_cross_role_continuity() -> None:
    shifts = [make_shift(name, {4, 5}) for name in ("alice", "bob", "carol")]
    profiles = {
        name: make_profile(
            main_isv=main_isv,
            encore_isv=240,
            encore_power=50,
            has_encore_role=True,
        )
        for name, main_isv in (("alice", 180), ("bob", 200), ("carol", 210))
    }

    schedule = ShiftScheduler.assign(
        shifts,
        [4, 5],
        team_profiles=profiles,
        encore_power_threshold=40,
    )

    first, second = schedule.assignments
    assert (
        second.supporter_usernames_by_slot[ENCORE_SUPPORTER_SLOT]
        == (first.supporter_usernames_by_slot[ENCORE_SUPPORTER_SLOT])
    )


def test_previous_honso_beats_new_candidate_for_tied_encore_isv() -> None:
    shifts = [
        make_shift("old_encore", {4}),
        make_shift("incumbent", {4, 5}),
        make_shift("new", {5}),
    ]
    profiles = {
        username: make_profile(
            main_isv=200,
            encore_isv=encore_isv,
            encore_power=50,
            has_encore_role=True,
        )
        for username, encore_isv in {
            "old_encore": 260,
            "incumbent": 240,
            "new": 240,
        }.items()
    }

    schedule = ShiftScheduler.assign(
        shifts,
        [4, 5],
        team_profiles=profiles,
        encore_power_threshold=40,
    )

    first, second = schedule.assignments
    assert first.supporter_usernames_by_slot[ENCORE_SUPPORTER_SLOT] == "old_encore"
    assert "incumbent" in first.supporter_usernames_by_slot.values()
    assert second.supporter_usernames_by_slot[ENCORE_SUPPORTER_SLOT] == "incumbent"


def test_previous_encore_competes_as_continuous_main_supporter() -> None:
    shifts = [
        make_shift("incumbent", {4, 5}),
        make_shift("higher_encore", {5}),
        make_shift("new_main", {5}),
    ]
    profiles = {
        "incumbent": make_profile(
            main_isv=200,
            encore_isv=240,
            encore_power=50,
            has_encore_role=True,
        ),
        "higher_encore": make_profile(
            main_isv=190,
            encore_isv=260,
            encore_power=50,
            has_encore_role=True,
        ),
        "new_main": make_profile(main_isv=200),
    }

    schedule = ShiftScheduler.assign(
        shifts,
        [4, 5],
        team_profiles=profiles,
        encore_power_threshold=40,
    )

    second = schedule.assignments[1].supporter_usernames_by_slot
    assert second[ENCORE_SUPPORTER_SLOT] == "higher_encore"
    assert "incumbent" in second.values()


def test_scheduler_places_lowest_selected_main_isv_in_standby() -> None:
    shifts = [make_shift(name, {4}) for name in "abcde"]
    profiles = {
        name: make_profile(main_isv=isv)
        for name, isv in zip("abcde", (250, 240, 230, 220, 210), strict=True)
    }

    schedule = ShiftScheduler.assign(
        shifts,
        [4],
        team_profiles=profiles,
        encore_power_threshold=35,
    )

    assignment = schedule.assignments[0]
    assert assignment.supporter_usernames_by_slot[STANDBY_SUPPORTER_SLOT] == "d"
    assert assignment.unassigned_usernames == ["e"]


def test_scheduler_keeps_tied_previous_standby_and_honso_columns() -> None:
    shifts = [make_shift(name, {4, 5}) for name in "abcd"]
    profiles = {name: make_profile(main_isv=200) for name in "abcd"}

    schedule = ShiftScheduler.assign(
        shifts,
        [4, 5],
        team_profiles=profiles,
        encore_power_threshold=35,
    )

    first, second = schedule.assignments
    assert (
        second.supporter_usernames_by_slot[STANDBY_SUPPORTER_SLOT]
        == (first.supporter_usernames_by_slot[STANDBY_SUPPORTER_SLOT])
    )
    for supporter_slot in HONSO_SUPPORTER_SLOTS:
        assert (
            second.supporter_usernames_by_slot[supporter_slot]
            == (first.supporter_usernames_by_slot[supporter_slot])
        )


def test_standby_tie_prefers_previous_supporter_over_new_candidate() -> None:
    shifts = [
        make_shift("encore", {4, 5}),
        make_shift("old_a", {4, 5}),
        make_shift("old_b", {4, 5}),
        make_shift("old_c", {4, 5}),
        make_shift("old_standby", {4}),
        make_shift("new", {5}),
    ]
    profiles = {
        name: make_profile(main_isv=200) for name in ("old_a", "old_b", "old_c", "new")
    }
    profiles["old_standby"] = make_profile(main_isv=190)
    profiles["encore"] = make_profile(
        main_isv=200,
        encore_isv=240,
        encore_power=50,
        has_encore_role=True,
    )

    schedule = ShiftScheduler.assign(
        shifts,
        [4, 5],
        team_profiles=profiles,
        encore_power_threshold=40,
    )

    assert (
        schedule.assignments[1].supporter_usernames_by_slot[STANDBY_SUPPORTER_SLOT]
        == "old_a"
    )


def test_honso_placement_rank_uses_full_previous_position_classes() -> None:
    main_rank = (False, -200.0, False, 0, 2, "alice")

    previous_honso = shift_scheduler._honso_placement_rank(  # noqa: SLF001
        main_rank,
        HONSO_SUPPORTER_SLOTS[1],
    )
    previous_encore = shift_scheduler._honso_placement_rank(  # noqa: SLF001
        main_rank,
        ENCORE_SUPPORTER_SLOT,
    )
    not_scheduled = shift_scheduler._honso_placement_rank(  # noqa: SLF001
        main_rank,
        None,
    )

    assert previous_honso < previous_encore < not_scheduled


def test_scheduler_resets_position_continuity_across_hour_gaps() -> None:
    shifts = [
        make_shift("z_incumbent", {4, 20}),
        make_shift("a_new", {20}),
    ]
    profiles = {
        name: make_profile(
            main_isv=200,
            encore_isv=240,
            encore_power=50,
            has_encore_role=True,
        )
        for name in ("z_incumbent", "a_new")
    }

    schedule = ShiftScheduler.assign(
        shifts,
        [4, 20],
        team_profiles=profiles,
        encore_power_threshold=40,
    )

    assert (
        schedule.assignments[1].supporter_usernames_by_slot[ENCORE_SUPPORTER_SLOT]
        == "a_new"
    )


def test_missing_main_isv_is_selected_last_and_becomes_standby() -> None:
    shifts = [make_shift(name, {4}) for name in "abcd"]
    profiles = {
        "a": make_profile(main_isv=230),
        "b": make_profile(main_isv=220),
        "c": make_profile(main_isv=210),
    }

    schedule = ShiftScheduler.assign(
        shifts,
        [4],
        team_profiles=profiles,
        encore_power_threshold=35,
    )

    assert (
        schedule.assignments[0].supporter_usernames_by_slot[STANDBY_SUPPORTER_SLOT]
        == "d"
    )
