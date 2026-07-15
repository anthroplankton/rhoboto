from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Final

from discord import AllowedMentions, Member, Object, Role, User
from discord.utils import format_dt

from models.admin_notifications import AdminNotificationMilestoneKind
from utils.announcement_languages import (
    render_announcement_messages_for_languages,
)
from utils.google_sheets_urls import google_sheet_url_with_gid

if TYPE_CHECKING:
    from collections.abc import Sequence

    from discord import Guild, TextChannel

    from models.shift_register import ShiftRegisterConfig


MIN_REMINDER_LEAD_MINUTES: Final = 1
MAX_REMINDER_LEAD_MINUTES: Final = 1440
MAX_MENTION_TARGETS: Final = 25
MAX_DISCORD_CONTENT_UNITS: Final = 2000


class ReminderLeadTimeError(ValueError):
    pass


class MentionSelectionError(ValueError):
    pass


class ReminderMessageError(RuntimeError):
    pass


@dataclass(frozen=True)
class MilestoneSpec:
    timeline_field: str
    worksheet_id_field: str
    template_key: str


MILESTONE_SPECS: Final = {
    AdminNotificationMilestoneKind.SUBMISSION_DEADLINE: MilestoneSpec(
        timeline_field="submission_deadline_at",
        worksheet_id_field="entry_worksheet_id",
        template_key="admin_notifications.shift.submission_deadline",
    ),
    AdminNotificationMilestoneKind.DRAFT_SHIFT_PROPOSAL: MilestoneSpec(
        timeline_field="draft_shift_proposal_at",
        worksheet_id_field="draft_worksheet_id",
        template_key="admin_notifications.shift.draft_shift_proposal",
    ),
    AdminNotificationMilestoneKind.FINAL_SHIFT_NOTICE: MilestoneSpec(
        timeline_field="final_shift_notice_at",
        worksheet_id_field="final_schedule_worksheet_id",
        template_key="admin_notifications.shift.final_shift_notice",
    ),
}


def parse_reminder_lead_minutes(raw: str) -> int:
    normalized = unicodedata.normalize("NFKC", raw).strip()
    if not normalized.isascii() or not normalized.isdecimal():
        raise ReminderLeadTimeError
    value = int(normalized)
    if not MIN_REMINDER_LEAD_MINUTES <= value <= MAX_REMINDER_LEAD_MINUTES:
        raise ReminderLeadTimeError
    return value


def milestone_datetime(
    shift_register: ShiftRegisterConfig,
    kind: AdminNotificationMilestoneKind,
) -> datetime | None:
    value = getattr(shift_register, MILESTONE_SPECS[kind].timeline_field)
    return value if isinstance(value, datetime) else None


def milestone_sheet_url(
    shift_register: ShiftRegisterConfig,
    kind: AdminNotificationMilestoneKind,
) -> str:
    worksheet_id = getattr(
        shift_register,
        MILESTONE_SPECS[kind].worksheet_id_field,
    )
    return google_sheet_url_with_gid(shift_register.sheet_url, worksheet_id)


def discord_content_length(content: str) -> int:
    return len(content.encode("utf-16-le")) // 2


@dataclass(frozen=True)
class MentionResolution:
    active_roles: tuple[Role, ...]
    active_users: tuple[Member, ...]
    missing_role_ids: tuple[int, ...]
    missing_user_ids: tuple[int, ...]
    unmentionable_roles: tuple[Role, ...]

    @property
    def mention_line(self) -> str:
        targets = (*self.active_roles, *self.active_users)
        return " ".join(target.mention for target in targets)

    def allowed_mentions(self) -> AllowedMentions:
        return AllowedMentions(
            everyone=False,
            roles=self.active_roles,
            users=self.active_users,
            replied_user=False,
        )


def _bot_can_mention_roles(guild: Guild, destination: TextChannel) -> bool:
    bot_member = guild.me
    if bot_member is None:
        return False
    return destination.permissions_for(bot_member).mention_everyone


def _role_is_usable(
    role: Role,
    *,
    guild: Guild,
    destination: TextChannel,
) -> bool:
    return not role.is_default() and (
        role.mentionable or _bot_can_mention_roles(guild, destination)
    )


def resolve_saved_mentions(
    guild: Guild,
    destination: TextChannel,
    *,
    role_ids: Sequence[int],
    user_ids: Sequence[int],
) -> MentionResolution:
    active_roles: list[Role] = []
    missing_role_ids: list[int] = []
    unmentionable_roles: list[Role] = []
    for role_id in role_ids:
        role = guild.get_role(role_id)
        if role is None:
            missing_role_ids.append(role_id)
        elif _role_is_usable(role, guild=guild, destination=destination):
            active_roles.append(role)
        else:
            unmentionable_roles.append(role)

    active_users: list[Member] = []
    missing_user_ids: list[int] = []
    for user_id in user_ids:
        member = guild.get_member(user_id)
        if member is None:
            missing_user_ids.append(user_id)
        else:
            active_users.append(member)

    return MentionResolution(
        active_roles=tuple(active_roles),
        active_users=tuple(active_users),
        missing_role_ids=tuple(missing_role_ids),
        missing_user_ids=tuple(missing_user_ids),
        unmentionable_roles=tuple(unmentionable_roles),
    )


def validate_selected_mentions(
    guild: Guild,
    destination: TextChannel,
    values: Sequence[Role | Member | User],
) -> tuple[list[int], list[int]]:
    if len(values) > MAX_MENTION_TARGETS:
        raise MentionSelectionError
    role_ids: list[int] = []
    user_ids: list[int] = []
    for value in values:
        if isinstance(value, Role):
            if value.id == guild.id or not _role_is_usable(
                value,
                guild=guild,
                destination=destination,
            ):
                raise MentionSelectionError
            role_ids.append(value.id)
        else:
            user_ids.append(value.id)
    return role_ids, user_ids


def saved_mention_defaults(
    role_ids: Sequence[int],
    user_ids: Sequence[int],
) -> list[Object]:
    return [
        *(Object(id=role_id, type=Role) for role_id in role_ids),
        *(Object(id=user_id, type=User) for user_id in user_ids),
    ]


@dataclass(frozen=True)
class ReminderMessage:
    content: str
    allowed_mentions: AllowedMentions
    sheet_url: str


def build_reminder_message(  # noqa: PLR0913
    *,
    shift_register: ShiftRegisterConfig,
    kind: AdminNotificationMilestoneKind,
    milestone_at: datetime,
    source_channel: str,
    languages: Sequence[str],
    mentions: MentionResolution,
) -> ReminderMessage:
    spec = MILESTONE_SPECS[kind]
    rendered = render_announcement_messages_for_languages(
        spec.template_key,
        languages,
        source_channel=source_channel,
        milestone_full_timestamp=format_dt(milestone_at, style="F"),
        milestone_relative_timestamp=format_dt(milestone_at, style="R"),
    )
    if [item.language for item in rendered] != list(languages):
        raise ReminderMessageError

    sections = [item.content.rstrip() for item in rendered]
    if mentions.mention_line:
        sections.insert(0, mentions.mention_line)
    content = "\n\n".join(sections)
    if discord_content_length(content) > MAX_DISCORD_CONTENT_UNITS:
        raise ReminderMessageError

    return ReminderMessage(
        content=content,
        allowed_mentions=mentions.allowed_mentions(),
        sheet_url=milestone_sheet_url(shift_register, kind),
    )
