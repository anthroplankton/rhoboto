from __future__ import annotations

from tortoise import fields, models

from models.base.timestamp_mixin import TimestampMixin


def default_announcement_languages() -> list[str]:
    """Return the default public announcement language order."""
    return ["ja"]


class GuildLanguageSettings(models.Model, TimestampMixin):
    id = fields.IntField(primary_key=True)
    guild_id = fields.BigIntField(unique=True)
    announcement_languages: list[str] = fields.JSONField(
        default=default_announcement_languages,
        description="Ordered language codes for public guild announcements.",
    )

    class Meta:
        table = "guild_language_settings"
