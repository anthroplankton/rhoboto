from __future__ import annotations

from typing import TYPE_CHECKING

from tortoise import fields, models

from models.base.timestamp_mixin import TimestampMixin

if TYPE_CHECKING:
    from tortoise.fields.relational import OneToOneRelation

    from models.feature_channel import FeatureChannel


class ShiftNoticeConfig(models.Model, TimestampMixin):
    id = fields.IntField(primary_key=True)
    feature_channel: OneToOneRelation[FeatureChannel] = fields.OneToOneField(
        "models.FeatureChannel",
        related_name="shift_notice_config",
        on_delete=fields.CASCADE,
    )
    guild_id = fields.BigIntField(unique=True)
    minute_of_hour: int | None = fields.IntField(null=True)

    class Meta:
        table = "shift_notice_config"
