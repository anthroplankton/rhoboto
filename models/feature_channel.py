from tortoise import fields, models

from models.base.timestamp_mixin import TimestampMixin


class FeatureChannel(models.Model, TimestampMixin):
    id = fields.IntField(pk=True)
    guild_id = fields.BigIntField()
    channel_id = fields.BigIntField()
    feature_name = fields.CharField(max_length=32)
    is_enabled = fields.BooleanField(
        default=True, description="Whether this feature is enabled in the channel."
    )

    class Meta:
        table = "feature_channel"
        unique_together = ("guild_id", "channel_id", "feature_name")
