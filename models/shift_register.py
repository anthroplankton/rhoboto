import datetime as dt

from tortoise import fields
from tortoise.fields.relational import (
    ForeignKeyNullableRelation,
    ForeignKeyRelation,
)

from models.base.sheet_config_base import SheetConfigBase
from models.feature_channel import FeatureChannel


class ShiftRegisterConfig(SheetConfigBase):
    feature_channel: ForeignKeyRelation[FeatureChannel] = fields.ForeignKeyField(
        "models.FeatureChannel", related_name="shift_register"
    )
    team_source_feature_channel: ForeignKeyNullableRelation[FeatureChannel] = (
        fields.ForeignKeyField(
            "models.FeatureChannel",
            related_name="source_for_shift_registers",
            null=True,
            on_delete=fields.SET_NULL,
        )
    )

    entry_worksheet_id = fields.BigIntField(
        description="ID of the entry worksheet for shift register"
    )
    draft_worksheet_id = fields.BigIntField(
        description="ID of the draft worksheet for shift register"
    )
    final_schedule_worksheet_id = fields.BigIntField(
        description="ID of the final schedule worksheet for shift register"
    )

    final_schedule_anchor_cell = fields.CharField(
        default="A1",
        max_length=8,
        description="Anchor cell for the final schedule worksheet",
    )
    day_number: int | None = fields.IntField(
        null=True,
        description="Shift event day number",
    )
    event_date: dt.date | None = fields.DateField(
        null=True,
        description="Shift event date",
    )
    submission_deadline_at: dt.datetime | None = fields.DatetimeField(
        null=True,
        description="Submission deadline timestamp",
    )
    draft_shift_proposal_at: dt.datetime | None = fields.DatetimeField(
        null=True,
        description="Draft shift proposal timestamp",
    )
    final_shift_notice_at: dt.datetime | None = fields.DatetimeField(
        null=True,
        description="Final shift notice timestamp",
    )
    recruitment_time_ranges: list[dict[str, int]] = fields.JSONField(
        default=lambda: [{"start": 4, "end": 28}],
        description="Normalized recruitment time ranges",
    )
    deadline_automation_enabled: bool = fields.BooleanField(
        default=False,
        description="Automatically disable Shift Register at Submission Deadline",
    )

    class Meta:
        table = "shift_register"

    @property
    def landing_worksheet_id(self) -> int:
        return self.entry_worksheet_id

    def get_worksheet_ids(self) -> list[int]:
        return [
            self.entry_worksheet_id,
            self.draft_worksheet_id,
            self.final_schedule_worksheet_id,
        ]
