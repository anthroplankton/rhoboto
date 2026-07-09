from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING

from tortoise import fields, models

from models.base.timestamp_mixin import TimestampMixin

if TYPE_CHECKING:
    from tortoise.fields.relational import ForeignKeyRelation

    from models.feature_channel import FeatureChannel


class FeatureChannelMessageKind(StrEnum):
    AUTO_GUIDE = "auto_guide"
    MANUAL_GUIDE = "manual_guide"


class FeatureChannelMessageState(models.Model, TimestampMixin):
    id = fields.IntField(primary_key=True)
    feature_channel: ForeignKeyRelation[FeatureChannel] = fields.ForeignKeyField(
        "models.FeatureChannel",
        related_name="message_states",
        on_delete=fields.CASCADE,
    )
    message_kind: FeatureChannelMessageKind = fields.CharEnumField(
        FeatureChannelMessageKind
    )
    is_enabled = fields.BooleanField(default=False)
    message_id: int | None = fields.BigIntField(null=True)

    class Meta:
        table = "feature_channel_message_state"
        unique_together = ("feature_channel", "message_kind")


async def get_auto_guide_state(
    feature_channel: FeatureChannel,
) -> FeatureChannelMessageState | None:
    return await FeatureChannelMessageState.get_or_none(
        feature_channel=feature_channel,
        message_kind=FeatureChannelMessageKind.AUTO_GUIDE,
    )


async def get_or_create_auto_guide_state(
    feature_channel: FeatureChannel,
) -> FeatureChannelMessageState:
    state, _ = await FeatureChannelMessageState.get_or_create(
        feature_channel=feature_channel,
        message_kind=FeatureChannelMessageKind.AUTO_GUIDE,
        defaults={"is_enabled": False},
    )
    return state


async def save_manual_guide_anchor(
    feature_channel: FeatureChannel,
    message_id: int,
) -> FeatureChannelMessageState:
    state, _ = await FeatureChannelMessageState.update_or_create(
        feature_channel=feature_channel,
        message_kind=FeatureChannelMessageKind.MANUAL_GUIDE,
        defaults={"is_enabled": True, "message_id": message_id},
    )
    return state
