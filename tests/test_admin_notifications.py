from __future__ import annotations

# ruff: noqa: RUF001, E501, ARG005
from dataclasses import dataclass
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from models.admin_notifications import AdminNotificationMilestoneKind
from utils import admin_notifications
from utils.admin_notifications import (
    MentionResolution,
    MentionSelectionError,
    ReminderLeadTimeError,
    ReminderMessageError,
    build_reminder_message,
    milestone_sheet_url,
    parse_reminder_lead_minutes,
    resolve_saved_mentions,
    saved_mention_defaults,
    validate_selected_mentions,
)


@dataclass(frozen=True)
class FakeRole:
    id: int
    mentionable: bool = True
    default: bool = False

    @property
    def mention(self) -> str:
        return f"<@&{self.id}>"

    def is_default(self) -> bool:
        return self.default


@dataclass(frozen=True)
class FakeMember:
    id: int

    @property
    def mention(self) -> str:
        return f"<@{self.id}>"


@dataclass(frozen=True)
class FakeUser:
    id: int


class FakeDestination:
    def __init__(self, *, mention_everyone: bool = False) -> None:
        self.mention_everyone = mention_everyone

    def permissions_for(self, member: object) -> SimpleNamespace:
        del member
        return SimpleNamespace(mention_everyone=self.mention_everyone)


class FakeGuild:
    def __init__(
        self,
        *,
        guild_id: int = 100,
        roles: list[FakeRole] | None = None,
        members: list[FakeMember] | None = None,
        bot_member: object | None = object(),
    ) -> None:
        self.id = guild_id
        self.me = bot_member
        self._roles = {role.id: role for role in roles or []}
        self._members = {member.id: member for member in members or []}

    def get_role(self, role_id: int) -> FakeRole | None:
        return self._roles.get(role_id)

    def get_member(self, user_id: int) -> FakeMember | None:
        return self._members.get(user_id)


@pytest.fixture(autouse=True)
def patch_discord_target_types(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(admin_notifications, "Role", FakeRole)
    monkeypatch.setattr(admin_notifications, "Member", FakeMember)
    monkeypatch.setattr(admin_notifications, "User", FakeUser)


def test_parse_reminder_lead_minutes_accepts_nfkc_digits_and_bounds() -> None:
    assert parse_reminder_lead_minutes(" ５ ") == 5
    assert parse_reminder_lead_minutes("1") == 1
    assert parse_reminder_lead_minutes("1440") == 1440


@pytest.mark.parametrize("value", ["", "+5", "-5", "5.0", "5 minutes", "0", "1441"])
def test_parse_reminder_lead_minutes_rejects_noncanonical_grammar(value: str) -> None:
    with pytest.raises(ReminderLeadTimeError):
        parse_reminder_lead_minutes(value)


def test_milestone_specs_map_all_three_fields_templates_and_worksheets() -> None:
    assert set(admin_notifications.MILESTONE_SPECS) == set(
        AdminNotificationMilestoneKind
    )
    assert (
        admin_notifications.MILESTONE_SPECS[
            AdminNotificationMilestoneKind.SUBMISSION_DEADLINE
        ].worksheet_id_field
        == "entry_worksheet_id"
    )
    assert (
        admin_notifications.MILESTONE_SPECS[
            AdminNotificationMilestoneKind.DRAFT_SHIFT_PROPOSAL
        ].worksheet_id_field
        == "draft_worksheet_id"
    )
    assert (
        admin_notifications.MILESTONE_SPECS[
            AdminNotificationMilestoneKind.FINAL_SHIFT_NOTICE
        ].worksheet_id_field
        == "final_schedule_worksheet_id"
    )


def test_validate_selected_mentions_rejects_everyone_too_many_and_unmentionable_roles() -> (
    None
):
    guild = FakeGuild(
        roles=[
            FakeRole(id=100, default=True),
            FakeRole(id=101, mentionable=False),
            FakeRole(id=102),
        ],
        members=[FakeMember(id=201)],
    )
    destination = FakeDestination()

    with pytest.raises(MentionSelectionError):
        validate_selected_mentions(guild, destination, [guild.get_role(100)])
    with pytest.raises(MentionSelectionError):
        validate_selected_mentions(guild, destination, [guild.get_role(101)])
    with pytest.raises(MentionSelectionError):
        validate_selected_mentions(
            guild,
            destination,
            [FakeMember(id=index) for index in range(26)],
        )

    role_ids, user_ids = validate_selected_mentions(
        guild,
        destination,
        [guild.get_role(102), guild.get_member(201)],
    )
    assert role_ids == [102]
    assert user_ids == [201]


def test_resolve_saved_mentions_retains_missing_and_unmentionable_targets() -> None:
    guild = FakeGuild(
        roles=[FakeRole(id=101, mentionable=False), FakeRole(id=102)],
        members=[FakeMember(id=201)],
    )
    resolution = resolve_saved_mentions(
        guild,
        FakeDestination(),
        role_ids=[101, 102, 103],
        user_ids=[201, 202],
    )

    assert [role.id for role in resolution.active_roles] == [102]
    assert [member.id for member in resolution.active_users] == [201]
    assert resolution.missing_role_ids == (103,)
    assert resolution.missing_user_ids == (202,)
    assert [role.id for role in resolution.unmentionable_roles] == [101]
    assert resolution.mention_line == "<@&102> <@201>"
    assert resolution.allowed_mentions().to_dict() == {
        "users": [201],
        "roles": [102],
        "parse": [],
    }


def test_saved_mention_defaults_keep_typed_missing_ids() -> None:
    defaults = saved_mention_defaults([101], [201])
    assert [(item.id, item.type) for item in defaults] == [
        (101, admin_notifications.Role),
        (201, admin_notifications.User),
    ]


def _shift_register() -> SimpleNamespace:
    return SimpleNamespace(
        sheet_url="https://docs.google.com/spreadsheets/d/example/edit",
        entry_worksheet_id=11,
        draft_worksheet_id=12,
        final_schedule_worksheet_id=13,
    )


def _milestone_at() -> datetime:
    return datetime(2026, 8, 13, 12, tzinfo=UTC)


def test_build_reminder_message_keeps_one_mention_line_and_language_order() -> None:
    mentions = MentionResolution(
        active_roles=(FakeRole(id=101),),
        active_users=(FakeMember(id=201),),
        missing_role_ids=(),
        missing_user_ids=(),
        unmentionable_roles=(),
    )
    result = build_reminder_message(
        shift_register=_shift_register(),
        kind=AdminNotificationMilestoneKind.DRAFT_SHIFT_PROPOSAL,
        milestone_at=_milestone_at(),
        source_channel="<#222>",
        languages=["zh_tw", "ja", "en"],
        mentions=mentions,
    )

    assert result.content.startswith("<@&101> <@201>\n\n")
    assert result.content.index("暫定班表") < result.content.index("仮シフト")
    assert result.content.index("仮シフト") < result.content.index(
        "Draft shift proposal"
    )
    assert result.sheet_url.endswith("?gid=12#gid=12")


@pytest.mark.parametrize(
    ("kind", "worksheet_id"),
    [
        (AdminNotificationMilestoneKind.SUBMISSION_DEADLINE, 11),
        (AdminNotificationMilestoneKind.DRAFT_SHIFT_PROPOSAL, 12),
        (AdminNotificationMilestoneKind.FINAL_SHIFT_NOTICE, 13),
    ],
)
def test_build_reminder_message_uses_entry_draft_and_final_links(
    kind: AdminNotificationMilestoneKind,
    worksheet_id: int,
) -> None:
    assert milestone_sheet_url(_shift_register(), kind).endswith(
        f"?gid={worksheet_id}#gid={worksheet_id}"
    )


def test_build_reminder_message_rejects_missing_template_and_utf16_overflow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mentions = MentionResolution((), (), (), (), ())
    with pytest.raises(ReminderMessageError):
        build_reminder_message(
            shift_register=_shift_register(),
            kind=AdminNotificationMilestoneKind.SUBMISSION_DEADLINE,
            milestone_at=_milestone_at(),
            source_channel="<#222>",
            languages=["fr"],
            mentions=mentions,
        )

    monkeypatch.setattr(
        admin_notifications,
        "render_announcement_messages_for_languages",
        lambda *args, **kwargs: [SimpleNamespace(language="en", content="😀" * 1001)],
    )
    with pytest.raises(ReminderMessageError):
        build_reminder_message(
            shift_register=_shift_register(),
            kind=AdminNotificationMilestoneKind.SUBMISSION_DEADLINE,
            milestone_at=_milestone_at(),
            source_channel="<#222>",
            languages=["en"],
            mentions=mentions,
        )


def test_maximum_three_language_twenty_five_mention_message_fits_discord() -> None:
    roles = tuple(FakeRole(id=100 + index) for index in range(13))
    users = tuple(FakeMember(id=200 + index) for index in range(12))
    result = build_reminder_message(
        shift_register=_shift_register(),
        kind=AdminNotificationMilestoneKind.FINAL_SHIFT_NOTICE,
        milestone_at=_milestone_at(),
        source_channel="<#222>",
        languages=["ja", "zh_tw", "en"],
        mentions=MentionResolution(roles, users, (), (), ()),
    )

    assert len(result.content.encode("utf-16-le")) // 2 < 2000
    assert result.content.count("<@&") == 13
    assert result.content.count("<@2") == 12
