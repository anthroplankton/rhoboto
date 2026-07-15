from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING

from tortoise import fields, models

from models.base.timestamp_mixin import TimestampMixin

if TYPE_CHECKING:
    from tortoise.fields.relational import ForeignKeyRelation, OneToOneRelation

    from models.feature_channel import FeatureChannel
    from models.shift_register import ShiftRegisterConfig


class AdminNotificationMilestoneKind(StrEnum):
    SUBMISSION_DEADLINE = "submission_deadline"
    DRAFT_SHIFT_PROPOSAL = "draft_shift_proposal"
    FINAL_SHIFT_NOTICE = "final_shift_notice"


class AdminNotificationDeliveryStatus(StrEnum):
    SCHEDULED = "scheduled"
    SENT = "sent"
    EXPIRED = "expired"
    FAILED = "failed"


class AdminNotificationsConfig(models.Model, TimestampMixin):
    id = fields.IntField(primary_key=True)
    feature_channel: OneToOneRelation[FeatureChannel] = fields.OneToOneField(
        "models.FeatureChannel",
        related_name="admin_notifications_config",
        on_delete=fields.CASCADE,
    )
    guild_id = fields.BigIntField(unique=True)
    reminder_lead_minutes: int | None = fields.IntField(null=True)
    mention_role_ids: list[int] = fields.JSONField(default=list)
    mention_user_ids: list[int] = fields.JSONField(default=list)
    shift_timeline_reminders_enabled: bool = fields.BooleanField(default=False)

    class Meta:
        table = "admin_notifications_config"


class AdminNotificationDelivery(models.Model, TimestampMixin):
    id = fields.IntField(primary_key=True)
    admin_notifications_config: ForeignKeyRelation[AdminNotificationsConfig] = (
        fields.ForeignKeyField(
            "models.AdminNotificationsConfig",
            related_name="deliveries",
            on_delete=fields.CASCADE,
        )
    )
    shift_register: ForeignKeyRelation[ShiftRegisterConfig] = fields.ForeignKeyField(
        "models.ShiftRegisterConfig",
        related_name="admin_notification_deliveries",
        on_delete=fields.CASCADE,
    )
    milestone_kind: AdminNotificationMilestoneKind = fields.CharEnumField(
        AdminNotificationMilestoneKind,
        max_length=32,
    )
    milestone_at = fields.DatetimeField()
    reminder_at = fields.DatetimeField()
    delivery_nonce = fields.BigIntField()
    status: AdminNotificationDeliveryStatus = fields.CharEnumField(
        AdminNotificationDeliveryStatus,
        max_length=16,
        default=AdminNotificationDeliveryStatus.SCHEDULED,
    )
    attempted_at = fields.DatetimeField(null=True)
    message_id: int | None = fields.BigIntField(null=True)

    class Meta:
        table = "admin_notification_delivery"
        unique_together = (
            "admin_notifications_config",
            "shift_register",
            "milestone_kind",
            "milestone_at",
        )
