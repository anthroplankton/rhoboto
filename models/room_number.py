from __future__ import annotations

from typing import TYPE_CHECKING, override

from tortoise import fields, models

from models.base.timestamp_mixin import TimestampMixin
from utils.room_number import DEFAULT_CHANNEL_NAME_FORMAT, parse_room_number_text

if TYPE_CHECKING:
    from collections.abc import Iterable

    from tortoise.backends.base.client import BaseDBAsyncClient
    from tortoise.fields.relational import OneToOneRelation

    from models.feature_channel import FeatureChannel


class RoomNumberConfig(models.Model, TimestampMixin):
    id = fields.IntField(primary_key=True)
    feature_channel: OneToOneRelation[FeatureChannel] = fields.OneToOneField(
        "models.FeatureChannel",
        related_name="room_number_config",
        on_delete=fields.CASCADE,
    )
    target_channel_id = fields.BigIntField(unique=True)
    room_number: str | None = fields.CharField(max_length=6, null=True)
    channel_name_format: str = fields.CharField(
        max_length=256,
        default=DEFAULT_CHANNEL_NAME_FORMAT,
    )
    recruitment_template_enabled: bool = fields.BooleanField(default=True)
    recruitment_template_channel_id: int | None = fields.BigIntField(null=True)
    recruitment_template_message_id: int | None = fields.BigIntField(null=True)

    class Meta:
        table = "room_number_config"

    def _validate_persisted_state(self) -> None:
        pointer_values = (
            self.recruitment_template_channel_id,
            self.recruitment_template_message_id,
        )
        if (pointer_values[0] is None) != (pointer_values[1] is None):
            message = "Recruitment template channel/message IDs must be paired."
            raise ValueError(message)
        if (
            self.room_number is not None
            and parse_room_number_text(self.room_number) != self.room_number
        ):
            message = "Room number must be canonical 5-6 digit ASCII text."
            raise ValueError(message)

    @override
    async def save(
        self,
        using_db: BaseDBAsyncClient | None = None,
        update_fields: Iterable[str] | None = None,
        force_create: bool = False,
        force_update: bool = False,
    ) -> None:
        self._validate_persisted_state()
        await super().save(
            using_db=using_db,
            update_fields=update_fields,
            force_create=force_create,
            force_update=force_update,
        )
