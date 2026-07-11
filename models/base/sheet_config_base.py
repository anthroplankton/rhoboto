from abc import abstractmethod

from tortoise import fields, models
from tortoise.fields.relational import ForeignKeyRelation

from models.base.timestamp_mixin import TimestampMixin
from models.feature_channel import FeatureChannel


class SheetConfigBase(models.Model, TimestampMixin):
    id: int = fields.IntField(primary_key=True)
    feature_channel: ForeignKeyRelation[FeatureChannel] = fields.ForeignKeyField(
        "models.FeatureChannel"
    )
    sheet_url: fields.CharField = fields.CharField(max_length=256)

    class Meta:
        abstract = True

    @property
    @abstractmethod
    def landing_worksheet_id(self) -> int:
        """Return the default user-facing worksheet destination ID."""

    @abstractmethod
    def get_worksheet_ids(self) -> list[int]:
        """Returns all worksheet ids relevant to this config."""
