from tortoise import fields
from tortoise.fields.relational import ForeignKeyRelation

from models.base.sheet_config_base import SheetConfigBase
from models.feature_channel import FeatureChannel


class ShiftRegisterConfig(SheetConfigBase):
    feature_channel: ForeignKeyRelation[FeatureChannel] = fields.ForeignKeyField(
        "models.FeatureChannel", related_name="shift_register"
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

    class Meta:
        table = "shift_register"

    def get_worksheet_ids(self) -> list[int]:
        return [
            self.entry_worksheet_id,
            self.draft_worksheet_id,
            self.final_schedule_worksheet_id,
        ]
