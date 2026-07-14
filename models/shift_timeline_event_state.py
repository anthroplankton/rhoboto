from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING

from tortoise import fields, models

from models.base.timestamp_mixin import TimestampMixin

if TYPE_CHECKING:
    from tortoise.fields.relational import ForeignKeyRelation

    from models.shift_register import ShiftRegisterConfig


class ShiftTimelineEventKind(StrEnum):
    SUBMISSION_DEADLINE = "submission_deadline"


class ShiftTimelineEventStatus(StrEnum):
    SCHEDULED = "scheduled"
    SENT = "sent"
    COMPLETED = "completed"


class ShiftTimelineEventState(models.Model, TimestampMixin):
    id = fields.IntField(primary_key=True)
    shift_register: ForeignKeyRelation[ShiftRegisterConfig] = fields.ForeignKeyField(
        "models.ShiftRegisterConfig",
        related_name="timeline_event_states",
        on_delete=fields.CASCADE,
    )
    event_kind: ShiftTimelineEventKind = fields.CharEnumField(
        ShiftTimelineEventKind,
        max_length=32,
    )
    scheduled_at = fields.DatetimeField()
    delivery_nonce = fields.BigIntField()
    status: ShiftTimelineEventStatus = fields.CharEnumField(
        ShiftTimelineEventStatus,
        max_length=16,
        default=ShiftTimelineEventStatus.SCHEDULED,
    )
    message_id: int | None = fields.BigIntField(null=True)

    class Meta:
        table = "shift_timeline_event_state"
        unique_together = ("shift_register", "event_kind")
