from __future__ import annotations

# ruff: noqa: SLF001, E501, ANN201, ANN202, ARG005, EM101
import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from discord import AllowedMentions

from cogs import admin_notifications
from cogs.admin_notifications import (
    INVALID_DESTINATION_MESSAGE,
    AdminNotifications,
    configured_elsewhere_message,
    is_usable_admin_notification_destination,
)
from models.admin_notifications import AdminNotificationMilestoneKind
from tests.fakes import FakeInteraction
from utils.admin_notifications import MentionResolution
from utils.admin_notifications_manager import DeliverySchedule


class FakeTextChannel:
    def __init__(
        self, channel_id: int, *, can_view: bool = True, can_send: bool = True
    ) -> None:
        self.id = channel_id
        self.mention = f"<#{channel_id}>"
        self._permissions = SimpleNamespace(
            view_channel=can_view,
            send_messages=can_send,
        )

    def permissions_for(self, member: object) -> SimpleNamespace:
        del member
        return self._permissions


class FakeGuild:
    def __init__(self, channels: list[object]) -> None:
        self.id = 1001
        self.me = object()
        self._channels = {channel.id: channel for channel in channels}

    def get_channel(self, channel_id: int) -> object | None:
        return self._channels.get(channel_id)


def _bot() -> SimpleNamespace:
    return SimpleNamespace(user=SimpleNamespace(id=999), wait_until_ready=AsyncMock())


class RuntimeBot:
    def __init__(self, guild: object) -> None:
        self.user = SimpleNamespace(id=999)
        self.guilds = [guild]
        self._ready = asyncio.Event()

    async def wait_until_ready(self) -> None:
        await self._ready.wait()

    def get_guild(self, guild_id: int) -> object | None:
        return next((guild for guild in self.guilds if guild.id == guild_id), None)


class DeliveryChannel(FakeTextChannel):
    def __init__(self, channel_id: int, *, can_history: bool = True) -> None:
        super().__init__(channel_id)
        self.can_history = can_history
        self.sent: list[dict[str, object]] = []
        self.history_calls: list[dict[str, object]] = []
        self.send_errors: list[Exception] = []
        self.history_messages: list[object] = []

    async def send(self, content: str, **kwargs: object) -> SimpleNamespace:
        if self.send_errors:
            raise self.send_errors.pop(0)
        self.sent.append({"content": content, **kwargs})
        return SimpleNamespace(id=9001)

    def history(self, **kwargs: object):
        self.history_calls.append(kwargs)

        async def iterator():
            for message in self.history_messages:
                yield message

        return iterator()

    def permissions_for(self, member: object) -> SimpleNamespace:
        del member
        return SimpleNamespace(
            view_channel=True,
            send_messages=True,
            read_message_history=self.can_history,
            mention_everyone=False,
        )


def _delivery_fixture(
    *,
    now: datetime,
    attempted_at: datetime | None = None,
    milestone_at: datetime | None = None,
    reminder_at: datetime | None = None,
) -> tuple[SimpleNamespace, DeliverySchedule, RuntimeBot, DeliveryChannel]:
    milestone_at = milestone_at or now + timedelta(hours=1)
    reminder_at = reminder_at or milestone_at - timedelta(minutes=60)
    channel = DeliveryChannel(777)
    guild = SimpleNamespace(
        id=1001,
        me=object(),
        get_channel=lambda channel_id: channel if channel_id == 777 else None,
        get_role=lambda role_id: None,
        get_member=lambda user_id: None,
    )
    shift_channel = SimpleNamespace(guild_id=1001, channel_id=555)
    shift = SimpleNamespace(
        id=55,
        feature_channel=shift_channel,
        submission_deadline_at=milestone_at,
        draft_shift_proposal_at=None,
        final_shift_notice_at=None,
        sheet_url="https://docs.google.com/spreadsheets/d/example",
        entry_worksheet_id=11,
        draft_worksheet_id=22,
        final_schedule_worksheet_id=33,
    )
    config = SimpleNamespace(
        id=10,
        guild_id=1001,
        reminder_lead_minutes=60,
        mention_role_ids=[],
        mention_user_ids=[],
        shift_timeline_reminders_enabled=True,
        feature_channel=SimpleNamespace(
            guild_id=1001,
            channel_id=777,
            is_enabled=True,
        ),
    )
    delivery = SimpleNamespace(
        id=7,
        admin_notifications_config=config,
        shift_register=shift,
        milestone_kind=AdminNotificationMilestoneKind.SUBMISSION_DEADLINE,
        milestone_at=milestone_at,
        reminder_at=reminder_at,
        delivery_nonce=1234,
        attempted_at=attempted_at,
        message_id=None,
        status="scheduled",
    )
    schedule = DeliverySchedule(
        delivery_id=7,
        config_id=10,
        guild_id=1001,
        shift_register_id=55,
        milestone_kind=AdminNotificationMilestoneKind.SUBMISSION_DEADLINE,
        milestone_at=milestone_at,
        reminder_at=reminder_at,
        wake_at=now,
        delivery_nonce=1234,
    )
    return delivery, schedule, RuntimeBot(guild), channel


def test_cog_registers_only_enable_settings_disable_and_disable_and_clear() -> None:
    names = {command.name for command in AdminNotifications.__cog_app_commands__}
    assert names == {"enable", "settings", "disable", "disable_and_clear"}


def test_enable_rejects_non_text_or_unusable_destination(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(admin_notifications, "TextChannel", FakeTextChannel)
    guild = FakeGuild([FakeTextChannel(222, can_view=False)])
    channel = guild.get_channel(222)
    assert is_usable_admin_notification_destination(channel, guild) is False
    assert configured_elsewhere_message(222) == (
        "Admin Notifications is already configured in <#222>. "
        "Use `/admin_notifications settings` there."
    )
    assert INVALID_DESTINATION_MESSAGE.startswith("⚠️ Admin Notifications")


@pytest.mark.asyncio
async def test_enable_new_destination_claims_then_sends_setup_followup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(admin_notifications, "TextChannel", FakeTextChannel)
    monkeypatch.setattr(
        admin_notifications,
        "claim_destination",
        AsyncMock(
            return_value=SimpleNamespace(
                config_id=10,
                feature_channel_id=20,
                channel_id=222,
                created=True,
                owns_requested_destination=True,
            )
        ),
    )
    monkeypatch.setattr(
        admin_notifications,
        "get_destination_config",
        AsyncMock(
            return_value=SimpleNamespace(
                id=10,
                guild_id=1001,
                updated_at=datetime(2026, 8, 13, tzinfo=UTC),
                reminder_lead_minutes=None,
                mention_role_ids=[],
                mention_user_ids=[],
                shift_timeline_reminders_enabled=False,
                feature_channel=SimpleNamespace(channel_id=222),
            )
        ),
    )
    cog = AdminNotifications(_bot())
    cog._reconcile_guild_locked = AsyncMock(return_value=SimpleNamespace(schedules=()))
    guild = FakeGuild([FakeTextChannel(222)])
    interaction = FakeInteraction(guild=guild)
    interaction.channel = guild.get_channel(222)

    await cog.enable.callback(cog, interaction)

    assert interaction.response.messages[0][0] == (
        "Feature Admin Notifications enabled in this channel."
    )
    assert interaction.followup.messages[0][1]["view"].children


@pytest.mark.asyncio
async def test_enable_other_usable_destination_uses_exact_block_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(admin_notifications, "TextChannel", FakeTextChannel)
    monkeypatch.setattr(
        admin_notifications,
        "claim_destination",
        AsyncMock(
            return_value=SimpleNamespace(
                config_id=10,
                feature_channel_id=20,
                channel_id=333,
                created=False,
                owns_requested_destination=False,
            )
        ),
    )
    cog = AdminNotifications(_bot())
    guild = FakeGuild([FakeTextChannel(222), FakeTextChannel(333)])
    interaction = FakeInteraction(guild=guild)
    interaction.channel = guild.get_channel(222)

    await cog.enable.callback(cog, interaction)

    assert interaction.response.messages[0][0] == configured_elsewhere_message(333)


@pytest.mark.asyncio
async def test_enable_other_unavailable_destination_shows_replacement_confirmation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(admin_notifications, "TextChannel", FakeTextChannel)
    monkeypatch.setattr(
        admin_notifications,
        "claim_destination",
        AsyncMock(
            return_value=SimpleNamespace(
                config_id=10,
                feature_channel_id=20,
                channel_id=333,
                created=False,
                owns_requested_destination=False,
            )
        ),
    )
    cog = AdminNotifications(_bot())
    guild = FakeGuild([FakeTextChannel(222), FakeTextChannel(333, can_view=False)])
    interaction = FakeInteraction(guild=guild)
    interaction.channel = guild.get_channel(222)

    await cog.enable.callback(cog, interaction)

    assert interaction.response.messages[0][0] == (
        "‼️ The configured Admin Notifications channel is unavailable. "
        "Replace it with this channel?"
    )


@pytest.mark.asyncio
async def test_setup_after_enable_uses_null_lead_as_only_incomplete_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(admin_notifications, "TextChannel", FakeTextChannel)
    config = SimpleNamespace(
        id=10,
        updated_at=datetime(2026, 8, 13, tzinfo=UTC),
        reminder_lead_minutes=None,
        mention_role_ids=[],
        mention_user_ids=[],
        shift_timeline_reminders_enabled=False,
        feature_channel=SimpleNamespace(channel_id=222),
    )
    monkeypatch.setattr(
        admin_notifications, "get_destination_config", AsyncMock(return_value=config)
    )
    cog = AdminNotifications(_bot())
    guild = FakeGuild([FakeTextChannel(222)])
    interaction = FakeInteraction(guild=guild)
    interaction.channel = guild.get_channel(222)

    await cog.setup_after_enable(interaction)

    assert "not yet configured" in interaction.followup.messages[0][0]


@pytest.mark.asyncio
async def test_settings_requires_enabled_owner_and_reuses_setup_after_enable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        admin_notifications, "get_destination_config", AsyncMock(return_value=None)
    )
    cog = AdminNotifications(_bot())
    interaction = FakeInteraction()
    await cog.setup_after_enable(interaction)
    assert interaction.followup.messages[0][0].startswith(
        "Admin Notifications settings are no longer configured"
    )


@pytest.mark.asyncio
async def test_cog_load_waits_until_ready_before_reading_notification_configs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queried = Mock()

    class ConfigRows:
        def all(self) -> ConfigRows:
            queried()
            return self

        async def values_list(self, *args: object, **kwargs: object) -> list[int]:
            del args, kwargs
            return [1001]

    monkeypatch.setattr(admin_notifications, "AdminNotificationsConfig", ConfigRows())
    guild = FakeGuild([])
    bot = RuntimeBot(guild)
    cog = AdminNotifications(bot)
    cog.request_reconcile_guild = Mock()

    await cog.cog_load()
    await asyncio.sleep(0)
    queried.assert_not_called()
    bot._ready.set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    queried.assert_called_once()
    cog.request_reconcile_guild.assert_called_once_with(1001)
    await cog.cog_unload()


@pytest.mark.asyncio
async def test_startup_requests_each_distinct_configured_guild_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ConfigRows:
        def all(self) -> ConfigRows:
            return self

        async def values_list(self, *args: object, **kwargs: object) -> list[int]:
            del args, kwargs
            return [1001, 1001, 2002]

    monkeypatch.setattr(admin_notifications, "AdminNotificationsConfig", ConfigRows())
    guild = FakeGuild([])
    bot = RuntimeBot(guild)
    bot._ready.set()
    cog = AdminNotifications(bot)
    cog.request_reconcile_guild = Mock()

    await cog.cog_load()
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert cog.request_reconcile_guild.call_args_list == [
        ((1001,),),
        ((2002,),),
    ]
    await cog.cog_unload()


@pytest.mark.asyncio
async def test_request_reconcile_coalesces_same_guild_and_reads_latest_state() -> None:
    bot = _bot()
    cog = AdminNotifications(bot)
    started = asyncio.Event()
    release = asyncio.Event()
    reconcile = AsyncMock()

    async def fake_reconcile(guild_id: int) -> None:
        del guild_id
        started.set()
        await release.wait()

    cog.reconcile_guild = fake_reconcile
    cog.request_reconcile_guild(1001)
    cog.request_reconcile_guild(1001)
    await started.wait()
    assert len(cog._reconcile_tasks) == 1
    release.set()
    await asyncio.gather(*cog._reconcile_tasks.values())
    assert not cog._reconcile_tasks
    del reconcile


@pytest.mark.asyncio
async def test_schedule_replacement_done_callback_cannot_remove_new_task() -> None:
    bot = _bot()
    cog = AdminNotifications(bot, sleep_until=AsyncMock())
    now = datetime(2026, 8, 13, 12, tzinfo=UTC)
    _, old_schedule, _, _ = _delivery_fixture(now=now)
    new_schedule = DeliverySchedule(
        **{
            **old_schedule.__dict__,
            "reminder_at": now + timedelta(minutes=2),
            "wake_at": now + timedelta(minutes=2),
        }
    )
    cog._apply_delivery_schedules(1001, (old_schedule,))
    old_task = cog._delivery_tasks[old_schedule.delivery_id]
    cog._apply_delivery_schedules(1001, (new_schedule,))
    new_task = cog._delivery_tasks[old_schedule.delivery_id]
    assert old_task is not new_task
    cog._remove_delivery_task_if_current(old_schedule.delivery_id, old_task)
    assert cog._delivery_tasks[old_schedule.delivery_id] is new_task
    await cog.cog_unload()


@pytest.mark.asyncio
async def test_direct_delivery_sends_one_ordered_message_with_nonce_mentions_and_sheet_button(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 8, 13, 12, tzinfo=UTC)
    delivery, schedule, bot, channel = _delivery_fixture(
        now=now,
        milestone_at=now + timedelta(hours=1),
        reminder_at=now,
    )
    monkeypatch.setattr(admin_notifications, "TextChannel", DeliveryChannel)
    monkeypatch.setattr(
        admin_notifications,
        "get_delivery_with_context",
        AsyncMock(return_value=delivery),
    )
    monkeypatch.setattr(
        admin_notifications,
        "resolve_saved_mentions",
        lambda *args, **kwargs: MentionResolution((), (), (), (), ()),
    )
    monkeypatch.setattr(
        admin_notifications,
        "get_announcement_languages",
        AsyncMock(return_value=["ja", "en"]),
    )
    monkeypatch.setattr(admin_notifications, "record_delivery_attempt", AsyncMock())
    monkeypatch.setattr(admin_notifications, "mark_delivery_sent", AsyncMock())
    cog = AdminNotifications(bot, now=lambda: now)

    assert await cog._deliver_once(schedule) is True
    assert len(channel.sent) == 1
    sent = channel.sent[0]
    assert sent["nonce"] == 1234
    assert isinstance(sent["allowed_mentions"], AllowedMentions)
    assert sent["allowed_mentions"].to_dict() == {
        "parse": [],
        "users": [],
        "roles": [],
    }
    view = sent["view"]
    assert len(view.children) == 1
    assert view.children[0].url.endswith("gid=11")


@pytest.mark.asyncio
async def test_stale_delivery_requests_reconcile_and_exits_without_send(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 8, 13, 12, tzinfo=UTC)
    delivery, schedule, bot, channel = _delivery_fixture(now=now)
    delivery.reminder_at = now + timedelta(minutes=3)
    monkeypatch.setattr(admin_notifications, "TextChannel", DeliveryChannel)
    monkeypatch.setattr(
        admin_notifications,
        "get_delivery_with_context",
        AsyncMock(return_value=delivery),
    )
    cog = AdminNotifications(bot, now=lambda: now)
    cog.request_reconcile_guild = Mock()

    assert await cog._deliver_once(schedule) is True
    assert channel.sent == []
    cog.request_reconcile_guild.assert_called_once_with(1001)


@pytest.mark.asyncio
async def test_permanent_template_or_length_failure_marks_failed_without_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 8, 13, 12, tzinfo=UTC)
    delivery, schedule, bot, channel = _delivery_fixture(now=now)
    monkeypatch.setattr(admin_notifications, "TextChannel", DeliveryChannel)
    monkeypatch.setattr(
        admin_notifications,
        "get_delivery_with_context",
        AsyncMock(return_value=delivery),
    )
    monkeypatch.setattr(
        admin_notifications,
        "build_reminder_message",
        Mock(side_effect=admin_notifications.ReminderMessageError),
    )
    monkeypatch.setattr(
        admin_notifications,
        "get_announcement_languages",
        AsyncMock(return_value=["en"]),
    )
    mark_failed = AsyncMock()
    monkeypatch.setattr(admin_notifications, "mark_delivery_failed", mark_failed)
    cog = AdminNotifications(bot, now=lambda: now)

    assert await cog._deliver_once(schedule) is True
    mark_failed.assert_awaited_once_with(7)
    assert channel.sent == []


@pytest.mark.asyncio
async def test_reconcile_retry_uses_sixty_second_exponential_cap() -> None:
    cog = AdminNotifications(_bot())
    delays: list[float] = []
    attempts = 0

    async def fake_reconcile(guild_id: int) -> None:
        nonlocal attempts
        del guild_id
        attempts += 1
        if attempts <= 8:
            raise RuntimeError("temporary")
        cog._is_unloading = True

    async def fake_wait(event: asyncio.Event, delay: float) -> None:
        del event
        delays.append(delay)

    cog.reconcile_guild = fake_reconcile
    cog._wait_for_reconcile_request = fake_wait
    event = asyncio.Event()

    await cog._run_reconcile_requests(1001, event)

    assert delays == [60, 120, 240, 480, 960, 1920, 3600, 3600]


@pytest.mark.asyncio
async def test_cog_unload_cancels_and_awaits_bootstrap_reconcile_and_delivery_tasks() -> (
    None
):
    cog = AdminNotifications(_bot(), sleep_until=AsyncMock())
    event = asyncio.Event()

    async def wait_forever() -> None:
        await event.wait()

    bootstrap = asyncio.create_task(wait_forever())
    reconcile = asyncio.create_task(wait_forever())
    delivery = asyncio.create_task(wait_forever())
    cog._bootstrap_task = bootstrap
    cog._reconcile_tasks[1001] = reconcile
    cog._reconcile_events[1001] = asyncio.Event()
    schedule = _delivery_fixture(now=datetime(2026, 8, 13, 12, tzinfo=UTC))[1]
    cog._delivery_specs[schedule.delivery_id] = schedule
    cog._delivery_tasks[schedule.delivery_id] = delivery

    await cog.cog_unload()
    assert cog._bootstrap_task is None
    assert not cog._reconcile_tasks
    assert not cog._reconcile_events
    assert not cog._delivery_tasks
    assert not cog._delivery_specs
    assert bootstrap.cancelled()
    assert reconcile.cancelled()
    assert delivery.cancelled()


@pytest.mark.asyncio
async def test_reconcile_request_after_unload_does_not_create_a_worker() -> None:
    cog = AdminNotifications(_bot())
    cog._is_unloading = True
    cog.request_reconcile_guild(1001)
    assert not cog._reconcile_tasks
    assert not cog._reconcile_events


@pytest.mark.asyncio
async def test_restart_history_nonce_match_marks_sent_without_resend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 8, 13, 12, tzinfo=UTC)
    attempted_at = now - timedelta(minutes=1)
    delivery, schedule, bot, channel = _delivery_fixture(
        now=now,
        attempted_at=attempted_at,
    )
    channel.history_messages = [
        SimpleNamespace(
            id=812,
            nonce=1234,
            author=SimpleNamespace(id=999),
        )
    ]
    monkeypatch.setattr(admin_notifications, "TextChannel", DeliveryChannel)
    monkeypatch.setattr(
        admin_notifications,
        "get_delivery_with_context",
        AsyncMock(return_value=delivery),
    )
    mark_sent = AsyncMock()
    monkeypatch.setattr(admin_notifications, "mark_delivery_sent", mark_sent)
    cog = AdminNotifications(bot, now=lambda: now)

    assert await cog._deliver_once(schedule) is True
    mark_sent.assert_awaited_once_with(7, 812)
    assert channel.sent == []
    assert channel.history_calls[0]["limit"] == 100


@pytest.mark.asyncio
async def test_delivery_transient_failure_retries_with_capped_backoff_until_cutoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 8, 13, 12, tzinfo=UTC)
    delivery, schedule, bot, channel = _delivery_fixture(
        now=now,
        milestone_at=now + timedelta(hours=4),
    )
    channel.send_errors = [RuntimeError("temporary")]
    monkeypatch.setattr(admin_notifications, "TextChannel", DeliveryChannel)
    monkeypatch.setattr(
        admin_notifications,
        "get_delivery_with_context",
        AsyncMock(return_value=delivery),
    )
    monkeypatch.setattr(
        admin_notifications,
        "resolve_saved_mentions",
        lambda *args, **kwargs: MentionResolution((), (), (), (), ()),
    )
    monkeypatch.setattr(
        admin_notifications,
        "get_announcement_languages",
        AsyncMock(return_value=["en"]),
    )
    monkeypatch.setattr(admin_notifications, "record_delivery_attempt", AsyncMock())
    monkeypatch.setattr(admin_notifications, "mark_delivery_sent", AsyncMock())
    delays: list[float] = []

    async def retry_sleep(delay: float) -> None:
        delays.append(delay)

    cog = AdminNotifications(
        bot,
        now=lambda: now,
        sleep_until=AsyncMock(),
        retry_sleep=retry_sleep,
    )
    await cog._run_delivery(schedule)

    assert delays == [60]
    assert len(channel.sent) == 1
