from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from utils.shift_scheduler import DRAFT_USERNAME_SUFFIX_PATTERN

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from discord import Member


class ScheduleRoleUpdateMode(StrEnum):
    ADD_ONLY = "add_only"
    REPLACE = "replace"


@dataclass(frozen=True)
class DuplicateScheduleRoleGroup:
    label: str
    member_ids: tuple[int, ...]


@dataclass(frozen=True)
class ScheduleRoleLabelMatch:
    label: str
    member_ids: tuple[int, ...]


@dataclass(frozen=True)
class ScheduleRoleResolution:
    unique_member_ids: tuple[int, ...]
    duplicate_groups: tuple[DuplicateScheduleRoleGroup, ...]
    unresolved_labels: tuple[str, ...]

    @property
    def duplicate_member_ids(self) -> tuple[int, ...]:
        return _ordered_unique(
            member_id
            for group in self.duplicate_groups
            for member_id in group.member_ids
        )


@dataclass(frozen=True)
class ScheduleRolePlan:
    add_member_ids: tuple[int, ...]
    already_member_ids: tuple[int, ...]
    remove_member_ids: tuple[int, ...]


def _ordered_unique(values: Iterable[int]) -> tuple[int, ...]:
    return tuple(dict.fromkeys(values))


def resolve_schedule_role_label_matches(
    labels: Sequence[str],
    members: Sequence[Member],
) -> tuple[ScheduleRoleLabelMatch, ...]:
    members_by_username = {member.name: member for member in members}
    members_by_display_name: dict[str, list[Member]] = {}
    for member in members:
        members_by_display_name.setdefault(member.display_name, []).append(member)

    matches: list[ScheduleRoleLabelMatch] = []
    for label in dict.fromkeys(labels):
        suffix = DRAFT_USERNAME_SUFFIX_PATTERN.search(label)
        if suffix is not None:
            member = members_by_username.get(suffix.group(1))
            member_ids = () if member is None else (member.id,)
        else:
            member_ids = _ordered_unique(
                member.id for member in members_by_display_name.get(label, [])
            )
        matches.append(ScheduleRoleLabelMatch(label, member_ids))

    return tuple(matches)


def resolve_schedule_role_labels(
    labels: Sequence[str],
    members: Sequence[Member],
) -> ScheduleRoleResolution:
    unique_member_ids: list[int] = []
    seen_unique_member_ids: set[int] = set()
    duplicate_groups: list[DuplicateScheduleRoleGroup] = []
    unresolved_labels: list[str] = []
    for match in resolve_schedule_role_label_matches(labels, members):
        if len(match.member_ids) == 1:
            member_id = match.member_ids[0]
            if member_id not in seen_unique_member_ids:
                unique_member_ids.append(member_id)
                seen_unique_member_ids.add(member_id)
        elif match.member_ids:
            duplicate_groups.append(
                DuplicateScheduleRoleGroup(
                    label=match.label,
                    member_ids=match.member_ids,
                )
            )
        else:
            unresolved_labels.append(match.label)

    return ScheduleRoleResolution(
        unique_member_ids=tuple(unique_member_ids),
        duplicate_groups=tuple(duplicate_groups),
        unresolved_labels=tuple(unresolved_labels),
    )


def plan_schedule_role_update(
    resolution: ScheduleRoleResolution,
    current_member_ids: Sequence[int],
    *,
    mode: ScheduleRoleUpdateMode,
    include_duplicates: bool | None,
) -> ScheduleRolePlan:
    current_ids = _ordered_unique(current_member_ids)
    duplicate_ids = resolution.duplicate_member_ids
    target_ids = resolution.unique_member_ids
    if include_duplicates is True:
        target_ids = _ordered_unique((*target_ids, *duplicate_ids))
    retained_ids = (
        _ordered_unique((*resolution.unique_member_ids, *duplicate_ids))
        if include_duplicates is None
        else target_ids
    )
    current_set = set(current_ids)
    retained_set = set(retained_ids)
    return ScheduleRolePlan(
        add_member_ids=tuple(
            member_id for member_id in target_ids if member_id not in current_set
        ),
        already_member_ids=tuple(
            member_id for member_id in target_ids if member_id in current_set
        ),
        remove_member_ids=(
            tuple(
                member_id for member_id in current_ids if member_id not in retained_set
            )
            if mode is ScheduleRoleUpdateMode.REPLACE
            else ()
        ),
    )
