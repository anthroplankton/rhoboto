from __future__ import annotations

# ruff: noqa: E501, PLR0913
import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from tortoise.queryset import QuerySet

from models.admin_notifications import (
    AdminNotificationDelivery,
    AdminNotificationDeliveryStatus,
    AdminNotificationsConfig,
)
from models.feature_channel import FeatureChannel
from models.shift_register import ShiftRegisterConfig
from utils.admin_notifications_manager import (
    AdminNotificationsStaleStateError,
    claim_destination,
    complete_setup,
    get_delivery_with_context,
    mark_delivery_expired,
    mark_delivery_failed,
    mark_delivery_sent,
    reconcile_occurrences,
    record_delivery_attempt,
    replace_unavailable_destination,
    save_lead_time,
    save_mentions,
    save_shift_timeline_reminders_enabled,
)
from utils.db import close_db, init_db

NOW = datetime(2026, 8, 13, 12, tzinfo=UTC)


async def _create_shift(
    guild_id: int,
    channel_id: int,
    *,
    enabled: bool = True,
    submission_deadline_at: datetime | None = None,
    draft_shift_proposal_at: datetime | None = None,
    final_shift_notice_at: datetime | None = None,
) -> ShiftRegisterConfig:
    feature_channel = await FeatureChannel.create(
        guild_id=guild_id,
        channel_id=channel_id,
        feature_name="shift_register",
        is_enabled=enabled,
    )
    return await ShiftRegisterConfig.create(
        feature_channel=feature_channel,
        sheet_url=f"https://docs.google.com/spreadsheets/d/shift-{channel_id}/edit",
        entry_worksheet_id=channel_id + 1,
        draft_worksheet_id=channel_id + 2,
        final_schedule_worksheet_id=channel_id + 3,
        submission_deadline_at=submission_deadline_at,
        draft_shift_proposal_at=draft_shift_proposal_at,
        final_shift_notice_at=final_shift_notice_at,
    )


async def _create_enabled_notification(
    guild_id: int = 1001,
    channel_id: int = 2001,
) -> AdminNotificationsConfig:
    claim = await claim_destination(guild_id, channel_id)
    config = await AdminNotificationsConfig.get(id=claim.config_id)
    await complete_setup(
        config.id,
        expected_updated_at=config.updated_at,
        expected_lead=None,
        new_lead=10,
    )
    config = await AdminNotificationsConfig.get(id=config.id)
    await save_shift_timeline_reminders_enabled(
        config.id,
        expected_updated_at=config.updated_at,
        expected_enabled=False,
        new_enabled=True,
    )
    return await AdminNotificationsConfig.get(id=config.id)


async def _start_db() -> str:
    db_url = "sqlite://:memory:"
    await asyncio.wait_for(init_db(db_url), timeout=3)
    return db_url


@pytest.mark.asyncio
async def test_claim_destination_creates_one_incomplete_atomic_reservation() -> None:
    db_url = await _start_db()
    try:
        first, second = await asyncio.gather(
            claim_destination(1001, 2001),
            claim_destination(1001, 2002),
        )

        assert first.config_id == second.config_id
        assert [first.created, second.created].count(True) == 1
        assert first.owns_requested_destination is not second.owns_requested_destination
        assert await AdminNotificationsConfig.filter(guild_id=1001).count() == 1
        assert (
            await FeatureChannel.filter(feature_name="admin_notifications").count() == 1
        )
    finally:
        await asyncio.wait_for(close_db(db_url), timeout=3)


@pytest.mark.asyncio
async def test_locked_config_queries_scope_for_update_to_config_table(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock_scopes: list[tuple[str, ...]] = []
    select_for_update = QuerySet.select_for_update

    def record_lock_scope(
        query: QuerySet,
        *,
        nowait: bool = False,
        skip_locked: bool = False,
        of: tuple[str, ...] = (),
        no_key: bool = False,
    ) -> QuerySet:
        if query.model is AdminNotificationsConfig:
            lock_scopes.append(of)
        return select_for_update(
            query,
            nowait=nowait,
            skip_locked=skip_locked,
            of=of,
            no_key=no_key,
        )

    monkeypatch.setattr(QuerySet, "select_for_update", record_lock_scope)
    db_url = await _start_db()
    try:
        claim = await claim_destination(1001, 2001)
        config = await AdminNotificationsConfig.get(id=claim.config_id)
        await complete_setup(
            config.id,
            expected_updated_at=config.updated_at,
            expected_lead=None,
            new_lead=10,
        )
        await replace_unavailable_destination(config.id, 2001, 2002)

        assert lock_scopes == [("admin_notifications_config",)] * 3
    finally:
        await asyncio.wait_for(close_db(db_url), timeout=3)


@pytest.mark.asyncio
async def test_same_destination_claim_reenables_without_resetting_config() -> None:
    db_url = await _start_db()
    try:
        config = await _create_enabled_notification()
        feature_channel = await FeatureChannel.get(id=config.feature_channel_id)
        config.mention_role_ids = [101]
        await config.save()
        feature_channel.is_enabled = False
        await feature_channel.save()

        claim = await claim_destination(1001, 2001)
        await config.refresh_from_db()
        await feature_channel.refresh_from_db()
        assert claim.owns_requested_destination is True
        assert feature_channel.is_enabled is True
        assert config.reminder_lead_minutes == 10
        assert config.mention_role_ids == [101]
    finally:
        await asyncio.wait_for(close_db(db_url), timeout=3)


@pytest.mark.asyncio
async def test_other_destination_claim_preserves_owner() -> None:
    db_url = await _start_db()
    try:
        config = await _create_enabled_notification()
        claim = await claim_destination(1001, 2002)
        assert claim.owns_requested_destination is False
        assert claim.channel_id == 2001
        assert (
            await AdminNotificationsConfig.get(id=config.id)
        ).feature_channel_id == (config.feature_channel_id)
    finally:
        await asyncio.wait_for(close_db(db_url), timeout=3)


@pytest.mark.asyncio
async def test_replace_destination_preserves_config_delivery_and_nonce() -> None:
    db_url = await _start_db()
    try:
        config = await _create_enabled_notification()
        shift = await _create_shift(
            1001,
            2100,
            final_shift_notice_at=NOW + timedelta(hours=1),
        )
        result = await reconcile_occurrences(1001, NOW)
        delivery_id = result.schedules[0].delivery_id
        delivery = await AdminNotificationDelivery.get(id=delivery_id)
        await replace_unavailable_destination(config.id, 2001, 2002)

        refreshed = await AdminNotificationsConfig.get(id=config.id)
        feature_channel = await FeatureChannel.get(id=config.feature_channel_id)
        retained_delivery = await AdminNotificationDelivery.get(id=delivery.id)
        assert refreshed.id == config.id
        assert feature_channel.channel_id == 2002
        assert retained_delivery.delivery_nonce == delivery.delivery_nonce
        assert retained_delivery.shift_register_id == shift.id
    finally:
        await asyncio.wait_for(close_db(db_url), timeout=3)


@pytest.mark.asyncio
async def test_setup_lead_mentions_and_toggle_writes_reject_stale_snapshots() -> None:
    db_url = await _start_db()
    try:
        claim = await claim_destination(1001, 2001)
        config = await AdminNotificationsConfig.get(id=claim.config_id)
        stale_at = config.updated_at
        config.mention_role_ids = [101]
        await config.save()

        with pytest.raises(AdminNotificationsStaleStateError):
            await complete_setup(
                config.id,
                expected_updated_at=stale_at,
                expected_lead=None,
                new_lead=10,
            )
        with pytest.raises(AdminNotificationsStaleStateError):
            await save_lead_time(
                config.id,
                expected_updated_at=stale_at,
                expected_lead=None,
                new_lead=20,
            )
        with pytest.raises(AdminNotificationsStaleStateError):
            await save_mentions(
                config.id,
                expected_updated_at=stale_at,
                expected_role_ids=[],
                expected_user_ids=[],
                new_role_ids=[],
                new_user_ids=[],
            )
        with pytest.raises(AdminNotificationsStaleStateError):
            await save_shift_timeline_reminders_enabled(
                config.id,
                expected_updated_at=stale_at,
                expected_enabled=False,
                new_enabled=True,
            )
        await config.refresh_from_db()
        assert config.reminder_lead_minutes is None
        assert config.mention_role_ids == [101]
        assert config.shift_timeline_reminders_enabled is False
    finally:
        await asyncio.wait_for(close_db(db_url), timeout=3)


@pytest.mark.asyncio
async def test_manager_rejects_config_feature_channel_guild_mismatch() -> None:
    db_url = await _start_db()
    try:
        feature_channel = await FeatureChannel.create(
            guild_id=2002,
            channel_id=2001,
            feature_name="admin_notifications",
        )
        await AdminNotificationsConfig.create(
            feature_channel=feature_channel,
            guild_id=1001,
        )
        with pytest.raises(AdminNotificationsStaleStateError):
            await reconcile_occurrences(1001, NOW)
    finally:
        await asyncio.wait_for(close_db(db_url), timeout=3)


@pytest.mark.asyncio
async def test_incomplete_reservation_survives_soft_disable_and_cascades_on_hard_clear() -> (
    None
):
    db_url = await _start_db()
    try:
        claim = await claim_destination(1001, 2001)
        feature_channel = await FeatureChannel.get(id=claim.feature_channel_id)
        feature_channel.is_enabled = False
        await feature_channel.save()
        assert await AdminNotificationsConfig.get(id=claim.config_id)
        await feature_channel.delete()
        assert await AdminNotificationsConfig.get_or_none(id=claim.config_id) is None
    finally:
        await asyncio.wait_for(close_db(db_url), timeout=3)


@pytest.mark.asyncio
async def test_reconcile_uses_all_persisted_shift_configs_including_soft_disabled() -> (
    None
):
    db_url = await _start_db()
    try:
        await _create_enabled_notification()
        future = NOW + timedelta(minutes=5)
        await _create_shift(
            1001,
            2100,
            enabled=False,
            submission_deadline_at=future,
            draft_shift_proposal_at=future + timedelta(hours=1),
            final_shift_notice_at=future + timedelta(hours=2),
        )
        result = await reconcile_occurrences(1001, NOW)
        assert len(result.schedules) == 3
        assert {schedule.shift_register_id for schedule in result.schedules}
        assert all(schedule.wake_at == NOW for schedule in result.schedules[:1])
    finally:
        await asyncio.wait_for(close_db(db_url), timeout=3)


@pytest.mark.asyncio
async def test_reconcile_schedules_future_and_immediate_catch_up_but_expires_past() -> (
    None
):
    db_url = await _start_db()
    try:
        await _create_enabled_notification()
        await _create_shift(
            1001,
            2100,
            submission_deadline_at=NOW - timedelta(minutes=1),
            draft_shift_proposal_at=NOW + timedelta(minutes=5),
            final_shift_notice_at=None,
        )
        result = await reconcile_occurrences(1001, NOW)
        assert len(result.schedules) == 1
        assert result.schedules[0].wake_at == NOW
        deliveries = await AdminNotificationDelivery.all()
        assert {delivery.status for delivery in deliveries} == {
            AdminNotificationDeliveryStatus.EXPIRED,
            AdminNotificationDeliveryStatus.SCHEDULED,
        }
    finally:
        await asyncio.wait_for(close_db(db_url), timeout=3)


@pytest.mark.asyncio
async def test_reconcile_skips_null_milestones_and_allows_zero_schedules() -> None:
    db_url = await _start_db()
    try:
        config = await _create_enabled_notification()
        await _create_shift(1001, 2100)
        result = await reconcile_occurrences(1001, NOW)
        assert result.schedules == ()

        await save_shift_timeline_reminders_enabled(
            config.id,
            expected_updated_at=config.updated_at,
            expected_enabled=True,
            new_enabled=False,
        )
        assert (await reconcile_occurrences(1001, NOW)).schedules == ()
    finally:
        await asyncio.wait_for(close_db(db_url), timeout=3)


@pytest.mark.asyncio
async def test_lead_edit_reschedules_only_unsent_rows() -> None:
    db_url = await _start_db()
    try:
        config = await _create_enabled_notification()
        shift = await _create_shift(
            1001,
            2100,
            final_shift_notice_at=NOW + timedelta(hours=1),
        )
        first = (await reconcile_occurrences(1001, NOW)).schedules[0]
        delivery = await AdminNotificationDelivery.get(id=first.delivery_id)
        await record_delivery_attempt(delivery.id, NOW)
        await mark_delivery_sent(delivery.id, 555)
        config = await AdminNotificationsConfig.get(id=config.id)
        await save_lead_time(
            config.id,
            expected_updated_at=config.updated_at,
            expected_lead=10,
            new_lead=20,
        )
        result = await reconcile_occurrences(1001, NOW)
        assert result.schedules == ()
        sent = await AdminNotificationDelivery.get(id=delivery.id)
        assert sent.status is AdminNotificationDeliveryStatus.SENT
        assert sent.delivery_nonce == delivery.delivery_nonce
        assert sent.shift_register_id == shift.id
    finally:
        await asyncio.wait_for(close_db(db_url), timeout=3)


@pytest.mark.asyncio
async def test_changed_milestone_creates_new_occurrence_and_keeps_sent_history() -> (
    None
):
    db_url = await _start_db()
    try:
        await _create_enabled_notification()
        shift = await _create_shift(
            1001,
            2100,
            final_shift_notice_at=NOW + timedelta(hours=1),
        )
        first = (await reconcile_occurrences(1001, NOW)).schedules[0]
        delivery = await AdminNotificationDelivery.get(id=first.delivery_id)
        await record_delivery_attempt(delivery.id, NOW)
        await mark_delivery_sent(delivery.id, 555)
        shift.final_shift_notice_at = NOW + timedelta(hours=2)
        await shift.save()
        result = await reconcile_occurrences(1001, NOW)
        assert len(result.schedules) == 1
        assert result.schedules[0].delivery_id != delivery.id
        assert await AdminNotificationDelivery.filter(status="sent").count() == 1
    finally:
        await asyncio.wait_for(close_db(db_url), timeout=3)


@pytest.mark.asyncio
async def test_cleared_milestone_deletes_only_superseded_unsent_rows() -> None:
    db_url = await _start_db()
    try:
        await _create_enabled_notification()
        shift = await _create_shift(
            1001,
            2100,
            final_shift_notice_at=NOW + timedelta(hours=1),
        )
        first = (await reconcile_occurrences(1001, NOW)).schedules[0]
        delivery = await AdminNotificationDelivery.get(id=first.delivery_id)
        await record_delivery_attempt(delivery.id, NOW)
        await mark_delivery_sent(delivery.id, 555)
        shift.final_shift_notice_at = None
        await shift.save()
        assert (await reconcile_occurrences(1001, NOW)).schedules == ()
        assert await AdminNotificationDelivery.get_or_none(id=delivery.id) is not None
    finally:
        await asyncio.wait_for(close_db(db_url), timeout=3)


@pytest.mark.asyncio
async def test_reconcile_keeps_failed_terminal_and_nonce_positive_signed_63_bit() -> (
    None
):
    db_url = await _start_db()
    try:
        await _create_enabled_notification()
        await _create_shift(1001, 2100, final_shift_notice_at=NOW + timedelta(hours=1))
        result = await reconcile_occurrences(1001, NOW)
        delivery = await AdminNotificationDelivery.get(
            id=result.schedules[0].delivery_id
        )
        await mark_delivery_failed(delivery.id)
        result = await reconcile_occurrences(1001, NOW)
        assert result.schedules == ()
        failed = await AdminNotificationDelivery.get(id=delivery.id)
        assert failed.status is AdminNotificationDeliveryStatus.FAILED
        assert 0 < failed.delivery_nonce < 2**63
    finally:
        await asyncio.wait_for(close_db(db_url), timeout=3)


@pytest.mark.asyncio
async def test_delivery_attempt_sent_failed_and_expired_transitions_are_stale_safe() -> (
    None
):
    db_url = await _start_db()
    try:
        await _create_enabled_notification()
        await _create_shift(1001, 2100, final_shift_notice_at=NOW + timedelta(hours=1))
        result = await reconcile_occurrences(1001, NOW)
        delivery = await AdminNotificationDelivery.get(
            id=result.schedules[0].delivery_id
        )
        await record_delivery_attempt(delivery.id, NOW)
        await mark_delivery_sent(delivery.id, 555)
        with pytest.raises(AdminNotificationsStaleStateError):
            await record_delivery_attempt(delivery.id, NOW)
        assert (await get_delivery_with_context(delivery.id)).message_id == 555

        await _create_shift(1001, 2200, final_shift_notice_at=NOW + timedelta(hours=1))
        result = await reconcile_occurrences(1001, NOW)
        pending = await AdminNotificationDelivery.get(
            id=next(
                schedule.delivery_id
                for schedule in result.schedules
                if schedule.delivery_id != delivery.id
            )
        )
        await mark_delivery_failed(pending.id)
        with pytest.raises(AdminNotificationsStaleStateError):
            await mark_delivery_expired(pending.id)
    finally:
        await asyncio.wait_for(close_db(db_url), timeout=3)
