from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta

from tortoise.exceptions import IntegrityError
from tortoise.transactions import in_transaction

from models.admin_notifications import (
    AdminNotificationDelivery,
    AdminNotificationDeliveryStatus,
    AdminNotificationMilestoneKind,
    AdminNotificationsConfig,
)
from models.feature_channel import FeatureChannel
from models.shift_register import ShiftRegisterConfig
from utils.admin_notifications import MILESTONE_SPECS, milestone_datetime


class AdminNotificationsConfigNotFoundError(LookupError):
    pass


class AdminNotificationsStaleStateError(RuntimeError):
    pass


@dataclass(frozen=True)
class DestinationClaim:
    config_id: int
    feature_channel_id: int
    channel_id: int
    created: bool
    owns_requested_destination: bool


@dataclass(frozen=True)
class DeliverySchedule:
    delivery_id: int
    config_id: int
    guild_id: int
    shift_register_id: int
    milestone_kind: AdminNotificationMilestoneKind
    milestone_at: datetime
    reminder_at: datetime
    wake_at: datetime
    delivery_nonce: int


@dataclass(frozen=True)
class ReconcileResult:
    config_id: int | None
    schedules: tuple[DeliverySchedule, ...]


def _new_delivery_nonce() -> int:
    return secrets.randbelow(2**63 - 1) + 1


async def _get_config_with_feature(
    guild_id: int,
    *,
    using_db: object | None = None,
    lock: bool = False,
) -> AdminNotificationsConfig | None:
    query = AdminNotificationsConfig.filter(guild_id=guild_id)
    if using_db is not None:
        query = query.using_db(using_db)
    query = query.select_related("feature_channel")
    if lock:
        query = query.select_for_update(of=("admin_notifications_config",))
    config = await query.first()
    if config is None:
        return None
    feature_channel = config.feature_channel
    if feature_channel.guild_id != config.guild_id:
        raise AdminNotificationsStaleStateError
    return config


async def get_guild_config(
    guild_id: int,
    *,
    using_db: object | None = None,
) -> AdminNotificationsConfig | None:
    return await _get_config_with_feature(guild_id, using_db=using_db)


async def get_destination_config(
    guild_id: int,
    channel_id: int,
    *,
    require_enabled: bool = False,
) -> AdminNotificationsConfig | None:
    config = await get_guild_config(guild_id)
    if config is None or config.feature_channel.channel_id != channel_id:
        return None
    if require_enabled and not config.feature_channel.is_enabled:
        return None
    return config


def _claim_from_config(
    config: AdminNotificationsConfig,
    *,
    requested_channel_id: int,
    created: bool,
) -> DestinationClaim:
    feature_channel = config.feature_channel
    return DestinationClaim(
        config_id=config.id,
        feature_channel_id=feature_channel.id,
        channel_id=feature_channel.channel_id,
        created=created,
        owns_requested_destination=feature_channel.channel_id == requested_channel_id,
    )


async def claim_destination(guild_id: int, channel_id: int) -> DestinationClaim:
    try:
        async with in_transaction() as connection:
            config = await _get_config_with_feature(
                guild_id,
                using_db=connection,
                lock=True,
            )
            if config is not None:
                feature_channel = await (
                    FeatureChannel.filter(id=config.feature_channel_id)
                    .using_db(connection)
                    .select_for_update()
                    .first()
                )
                if feature_channel is None:
                    raise AdminNotificationsStaleStateError
                if feature_channel.guild_id != config.guild_id:
                    raise AdminNotificationsStaleStateError
                if (
                    feature_channel.channel_id == channel_id
                    and not feature_channel.is_enabled
                ):
                    feature_channel.is_enabled = True
                    await feature_channel.save(
                        using_db=connection,
                        update_fields=["is_enabled", "updated_at"],
                    )
                    config.feature_channel = feature_channel
                return _claim_from_config(
                    config,
                    requested_channel_id=channel_id,
                    created=False,
                )

            feature_channel = await (
                FeatureChannel.filter(
                    guild_id=guild_id,
                    channel_id=channel_id,
                    feature_name="admin_notifications",
                )
                .using_db(connection)
                .first()
            )
            if feature_channel is None:
                feature_channel = await FeatureChannel.create(
                    using_db=connection,
                    guild_id=guild_id,
                    channel_id=channel_id,
                    feature_name="admin_notifications",
                )
            config = await AdminNotificationsConfig.create(
                using_db=connection,
                feature_channel=feature_channel,
                guild_id=guild_id,
            )
            config.feature_channel = feature_channel
            return _claim_from_config(
                config,
                requested_channel_id=channel_id,
                created=True,
            )
    except IntegrityError:
        winner = await get_guild_config(guild_id)
        if winner is None:
            raise
        return _claim_from_config(
            winner,
            requested_channel_id=channel_id,
            created=False,
        )


async def replace_unavailable_destination(
    config_id: int,
    expected_channel_id: int,
    new_channel_id: int,
) -> None:
    async with in_transaction() as connection:
        config = await (
            AdminNotificationsConfig.filter(id=config_id)
            .using_db(connection)
            .select_related("feature_channel")
            .select_for_update(of=("admin_notifications_config",))
            .first()
        )
        if config is None:
            raise AdminNotificationsStaleStateError
        feature_channel = await (
            FeatureChannel.filter(id=config.feature_channel_id)
            .using_db(connection)
            .select_for_update()
            .first()
        )
        if (
            feature_channel is None
            or feature_channel.guild_id != config.guild_id
            or feature_channel.channel_id != expected_channel_id
        ):
            raise AdminNotificationsStaleStateError
        feature_channel.channel_id = new_channel_id
        feature_channel.is_enabled = True
        await feature_channel.save(
            using_db=connection,
            update_fields=["channel_id", "is_enabled", "updated_at"],
        )


async def _get_locked_config(
    config_id: int,
    connection: object,
) -> AdminNotificationsConfig:
    config = await (
        AdminNotificationsConfig.filter(id=config_id)
        .using_db(connection)
        .select_related("feature_channel")
        .select_for_update(of=("admin_notifications_config",))
        .first()
    )
    if config is None:
        raise AdminNotificationsConfigNotFoundError(config_id)
    if config.guild_id != config.feature_channel.guild_id:
        raise AdminNotificationsStaleStateError
    return config


def _check_snapshot(
    config: AdminNotificationsConfig,
    *,
    expected_updated_at: datetime,
    expected_values: dict[str, object],
) -> None:
    if config.updated_at != expected_updated_at or any(
        getattr(config, field) != expected_value
        for field, expected_value in expected_values.items()
    ):
        raise AdminNotificationsStaleStateError


async def complete_setup(
    config_id: int,
    *,
    expected_updated_at: datetime,
    expected_lead: int | None,
    new_lead: int,
) -> AdminNotificationsConfig:
    async with in_transaction() as connection:
        config = await _get_locked_config(config_id, connection)
        _check_snapshot(
            config,
            expected_updated_at=expected_updated_at,
            expected_values={"reminder_lead_minutes": expected_lead},
        )
        if config.reminder_lead_minutes is not None:
            raise AdminNotificationsStaleStateError
        config.reminder_lead_minutes = new_lead
        await config.save(
            using_db=connection,
            update_fields=["reminder_lead_minutes", "updated_at"],
        )
        return config


async def save_lead_time(
    config_id: int,
    *,
    expected_updated_at: datetime,
    expected_lead: int | None,
    new_lead: int,
) -> AdminNotificationsConfig:
    async with in_transaction() as connection:
        config = await _get_locked_config(config_id, connection)
        _check_snapshot(
            config,
            expected_updated_at=expected_updated_at,
            expected_values={"reminder_lead_minutes": expected_lead},
        )
        config.reminder_lead_minutes = new_lead
        await config.save(
            using_db=connection,
            update_fields=["reminder_lead_minutes", "updated_at"],
        )
        return config


async def save_mentions(  # noqa: PLR0913
    config_id: int,
    *,
    expected_updated_at: datetime,
    expected_role_ids: list[int],
    expected_user_ids: list[int],
    new_role_ids: list[int],
    new_user_ids: list[int],
) -> AdminNotificationsConfig:
    async with in_transaction() as connection:
        config = await _get_locked_config(config_id, connection)
        _check_snapshot(
            config,
            expected_updated_at=expected_updated_at,
            expected_values={
                "mention_role_ids": expected_role_ids,
                "mention_user_ids": expected_user_ids,
            },
        )
        config.mention_role_ids = list(new_role_ids)
        config.mention_user_ids = list(new_user_ids)
        await config.save(
            using_db=connection,
            update_fields=[
                "mention_role_ids",
                "mention_user_ids",
                "updated_at",
            ],
        )
        return config


async def save_shift_timeline_reminders_enabled(
    config_id: int,
    *,
    expected_updated_at: datetime,
    expected_enabled: bool,
    new_enabled: bool,
) -> AdminNotificationsConfig:
    async with in_transaction() as connection:
        config = await _get_locked_config(config_id, connection)
        _check_snapshot(
            config,
            expected_updated_at=expected_updated_at,
            expected_values={
                "shift_timeline_reminders_enabled": expected_enabled,
            },
        )
        config.shift_timeline_reminders_enabled = new_enabled
        await config.save(
            using_db=connection,
            update_fields=["shift_timeline_reminders_enabled", "updated_at"],
        )
        return config


def _delivery_identity(
    delivery: AdminNotificationDelivery,
) -> tuple[int, AdminNotificationMilestoneKind, datetime]:
    return (
        delivery.shift_register_id,
        delivery.milestone_kind,
        delivery.milestone_at,
    )


def _schedule_from_delivery(
    delivery: AdminNotificationDelivery,
    *,
    config: AdminNotificationsConfig,
    shift_register: ShiftRegisterConfig,
    now: datetime,
) -> DeliverySchedule:
    return DeliverySchedule(
        delivery_id=delivery.id,
        config_id=config.id,
        guild_id=config.guild_id,
        shift_register_id=shift_register.id,
        milestone_kind=delivery.milestone_kind,
        milestone_at=delivery.milestone_at,
        reminder_at=delivery.reminder_at,
        wake_at=max(delivery.reminder_at, now),
        delivery_nonce=delivery.delivery_nonce,
    )


async def reconcile_occurrences(  # noqa: C901
    guild_id: int,
    now: datetime,
) -> ReconcileResult:
    async with in_transaction() as connection:
        config = await _get_config_with_feature(
            guild_id,
            using_db=connection,
            lock=True,
        )
        if config is None:
            return ReconcileResult(config_id=None, schedules=())
        feature_channel = await (
            FeatureChannel.filter(id=config.feature_channel_id)
            .using_db(connection)
            .select_for_update()
            .first()
        )
        if feature_channel is None or feature_channel.guild_id != config.guild_id:
            raise AdminNotificationsStaleStateError
        if (
            not feature_channel.is_enabled
            or config.reminder_lead_minutes is None
            or not config.shift_timeline_reminders_enabled
        ):
            return ReconcileResult(config_id=config.id, schedules=())

        shift_configs = await (
            ShiftRegisterConfig.filter(feature_channel__guild_id=guild_id)
            .using_db(connection)
            .select_related("feature_channel")
        )
        deliveries = await (
            AdminNotificationDelivery.filter(admin_notifications_config_id=config.id)
            .using_db(connection)
            .select_for_update()
        )
        by_identity = {
            _delivery_identity(delivery): delivery for delivery in deliveries
        }
        desired_identities: set[
            tuple[int, AdminNotificationMilestoneKind, datetime]
        ] = set()
        schedules: list[DeliverySchedule] = []

        for shift_register in shift_configs:
            for kind in MILESTONE_SPECS:
                milestone_at = milestone_datetime(shift_register, kind)
                if milestone_at is None:
                    continue
                identity = (shift_register.id, kind, milestone_at)
                desired_identities.add(identity)
                reminder_at = milestone_at - timedelta(
                    minutes=config.reminder_lead_minutes
                )
                delivery = by_identity.get(identity)
                if delivery is None:
                    delivery = await AdminNotificationDelivery.create(
                        using_db=connection,
                        admin_notifications_config=config,
                        shift_register=shift_register,
                        milestone_kind=kind,
                        milestone_at=milestone_at,
                        reminder_at=reminder_at,
                        delivery_nonce=_new_delivery_nonce(),
                        status=(
                            AdminNotificationDeliveryStatus.EXPIRED
                            if milestone_at <= now
                            else AdminNotificationDeliveryStatus.SCHEDULED
                        ),
                    )
                    by_identity[identity] = delivery
                elif (
                    delivery.status is AdminNotificationDeliveryStatus.SCHEDULED
                    and delivery.reminder_at != reminder_at
                ):
                    delivery.reminder_at = reminder_at
                    await delivery.save(
                        using_db=connection,
                        update_fields=["reminder_at", "updated_at"],
                    )

                if delivery.status is not AdminNotificationDeliveryStatus.SCHEDULED:
                    continue
                if milestone_at <= now:
                    delivery.status = (
                        AdminNotificationDeliveryStatus.FAILED
                        if delivery.attempted_at is not None
                        else AdminNotificationDeliveryStatus.EXPIRED
                    )
                    await delivery.save(
                        using_db=connection,
                        update_fields=["status", "updated_at"],
                    )
                    continue
                schedules.append(
                    _schedule_from_delivery(
                        delivery,
                        config=config,
                        shift_register=shift_register,
                        now=now,
                    )
                )

        for delivery in deliveries:
            if (
                _delivery_identity(delivery) not in desired_identities
                and delivery.status is not AdminNotificationDeliveryStatus.SENT
            ):
                await delivery.delete(using_db=connection)

        schedules.sort(
            key=lambda schedule: (
                schedule.wake_at,
                schedule.shift_register_id,
                schedule.milestone_kind.value,
            )
        )
        return ReconcileResult(config_id=config.id, schedules=tuple(schedules))


async def get_delivery_with_context(
    delivery_id: int,
) -> AdminNotificationDelivery:
    delivery = await (
        AdminNotificationDelivery.filter(id=delivery_id)
        .select_related(
            "admin_notifications_config__feature_channel",
            "shift_register__feature_channel",
        )
        .first()
    )
    if delivery is None:
        raise AdminNotificationsStaleStateError
    return delivery


async def _get_locked_delivery(
    delivery_id: int,
    connection: object,
) -> AdminNotificationDelivery:
    delivery = await (
        AdminNotificationDelivery.filter(id=delivery_id)
        .using_db(connection)
        .select_for_update()
        .first()
    )
    if (
        delivery is None
        or delivery.status is not AdminNotificationDeliveryStatus.SCHEDULED
    ):
        raise AdminNotificationsStaleStateError
    return delivery


async def record_delivery_attempt(
    delivery_id: int,
    attempted_at: datetime,
) -> AdminNotificationDelivery:
    async with in_transaction() as connection:
        delivery = await _get_locked_delivery(delivery_id, connection)
        delivery.attempted_at = attempted_at
        await delivery.save(
            using_db=connection,
            update_fields=["attempted_at", "updated_at"],
        )
        return delivery


async def mark_delivery_sent(
    delivery_id: int,
    message_id: int,
) -> AdminNotificationDelivery:
    async with in_transaction() as connection:
        delivery = await _get_locked_delivery(delivery_id, connection)
        delivery.message_id = message_id
        delivery.status = AdminNotificationDeliveryStatus.SENT
        await delivery.save(
            using_db=connection,
            update_fields=["message_id", "status", "updated_at"],
        )
        return delivery


async def mark_delivery_failed(delivery_id: int) -> AdminNotificationDelivery:
    async with in_transaction() as connection:
        delivery = await _get_locked_delivery(delivery_id, connection)
        delivery.status = AdminNotificationDeliveryStatus.FAILED
        await delivery.save(
            using_db=connection,
            update_fields=["status", "updated_at"],
        )
        return delivery


async def mark_delivery_expired(delivery_id: int) -> AdminNotificationDelivery:
    async with in_transaction() as connection:
        delivery = await _get_locked_delivery(delivery_id, connection)
        delivery.status = AdminNotificationDeliveryStatus.EXPIRED
        await delivery.save(
            using_db=connection,
            update_fields=["status", "updated_at"],
        )
        return delivery
