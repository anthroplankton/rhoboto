from __future__ import annotations

from dataclasses import dataclass

import pytest

from utils.shift_schedule_role import (
    DuplicateScheduleRoleGroup,
    ScheduleRoleLabelMatch,
    ScheduleRolePlan,
    ScheduleRoleResolution,
    ScheduleRoleUpdateMode,
    plan_schedule_role_update,
    resolve_schedule_role_label_matches,
    resolve_schedule_role_labels,
)


@dataclass(frozen=True)
class FakeMember:
    id: int
    name: str
    display_name: str


def test_label_matches_use_exact_username_suffix_without_fallback() -> None:
    result = resolve_schedule_role_label_matches(
        (
            "Alias ⟨@alice_one⟩",
            "Alias ⟨@missing_user⟩",
        ),
        (
            FakeMember(1, "alice_one", "Alice"),
            FakeMember(2, "other_user", "Alias ⟨@missing_user⟩"),
        ),
    )

    assert result == (
        ScheduleRoleLabelMatch("Alias ⟨@alice_one⟩", (1,)),
        ScheduleRoleLabelMatch("Alias ⟨@missing_user⟩", ()),
    )


def test_label_matches_use_exact_display_names_in_member_order() -> None:
    result = resolve_schedule_role_label_matches(
        ("Bob", "Alice"),
        (
            FakeMember(3, "alice_three", "Alice"),
            FakeMember(1, "bob_user", "Bob"),
            FakeMember(2, "alice_two", "Alice"),
        ),
    )

    assert result == (
        ScheduleRoleLabelMatch("Bob", (1,)),
        ScheduleRoleLabelMatch("Alice", (3, 2)),
    )


def test_label_matches_preserve_unresolved_and_first_label_occurrence() -> None:
    result = resolve_schedule_role_label_matches(
        ("Missing", "Bob", "Missing", "Bob"),
        (FakeMember(1, "bob_user", "Bob"),),
    )

    assert result == (
        ScheduleRoleLabelMatch("Missing", ()),
        ScheduleRoleLabelMatch("Bob", (1,)),
    )


def test_different_labels_may_match_the_same_member() -> None:
    result = resolve_schedule_role_label_matches(
        ("Alice ⟨@alice_one⟩", "Alice"),
        (FakeMember(1, "alice_one", "Alice"),),
    )

    assert result == (
        ScheduleRoleLabelMatch("Alice ⟨@alice_one⟩", (1,)),
        ScheduleRoleLabelMatch("Alice", (1,)),
    )


def test_resolver_uses_two_mutually_exclusive_exact_paths() -> None:
    members = (
        FakeMember(1, "alice_one", "Alice"),
        FakeMember(2, "alice_two", "Alice"),
        FakeMember(3, "bob_user", "Bob"),
    )

    result = resolve_schedule_role_labels(
        (
            "Alias ⟨@alice_one⟩",
            "Alias ⟨@missing_user⟩",
            "Alice",
            "Bob",
            "Bob ⟨@bob_user⟩",
            "bob_user",
        ),
        members,
    )

    assert result.unique_member_ids == (1, 3)
    assert result.duplicate_groups == (DuplicateScheduleRoleGroup("Alice", (1, 2)),)
    assert result.unresolved_labels == (
        "Alias ⟨@missing_user⟩",
        "bob_user",
    )


def test_resolver_deduplicates_labels_without_duplicate_member_ids() -> None:
    result = resolve_schedule_role_labels(
        (
            "Alice ⟨@alice_one⟩",
            "Alice",
            "Alice",
            "Alice ⟨@alice_one⟩",
        ),
        (
            FakeMember(1, "alice_one", "Alice"),
            FakeMember(2, "alice_two", "Alice"),
        ),
    )

    assert result.unique_member_ids == (1,)
    assert result.duplicate_groups == (DuplicateScheduleRoleGroup("Alice", (1, 2)),)
    assert result.duplicate_member_ids == (1, 2)


@pytest.mark.parametrize(
    ("mode", "expected_removals"),
    [
        (ScheduleRoleUpdateMode.ADD_ONLY, ()),
        (ScheduleRoleUpdateMode.REPLACE, (3,)),
    ],
)
def test_schedule_role_plan_handles_duplicate_preview(
    mode: ScheduleRoleUpdateMode,
    expected_removals: tuple[int, ...],
) -> None:
    resolution = ScheduleRoleResolution(
        unique_member_ids=(1,),
        duplicate_groups=(DuplicateScheduleRoleGroup("Alice", (1, 2)),),
        unresolved_labels=(),
    )

    plan = plan_schedule_role_update(
        resolution,
        (1, 2, 3),
        mode=mode,
        include_duplicates=None,
    )

    assert plan == ScheduleRolePlan(
        add_member_ids=(),
        already_member_ids=(1,),
        remove_member_ids=expected_removals,
    )


def test_schedule_role_plan_includes_or_skips_duplicate_members() -> None:
    resolution = ScheduleRoleResolution(
        unique_member_ids=(1,),
        duplicate_groups=(DuplicateScheduleRoleGroup("Alice", (1, 2)),),
        unresolved_labels=(),
    )

    assert plan_schedule_role_update(
        resolution,
        (1, 2, 3),
        mode=ScheduleRoleUpdateMode.REPLACE,
        include_duplicates=True,
    ) == ScheduleRolePlan((), (1, 2), (3,))
    assert plan_schedule_role_update(
        resolution,
        (1, 2, 3),
        mode=ScheduleRoleUpdateMode.REPLACE,
        include_duplicates=False,
    ) == ScheduleRolePlan((), (1,), (2, 3))


def test_schedule_role_plan_add_only_and_empty_replace() -> None:
    resolution = ScheduleRoleResolution((), (), ())

    assert plan_schedule_role_update(
        resolution,
        (1, 2),
        mode=ScheduleRoleUpdateMode.ADD_ONLY,
        include_duplicates=False,
    ) == ScheduleRolePlan((), (), ())
    assert plan_schedule_role_update(
        resolution,
        (1, 2),
        mode=ScheduleRoleUpdateMode.REPLACE,
        include_duplicates=False,
    ) == ScheduleRolePlan((), (), (1, 2))
