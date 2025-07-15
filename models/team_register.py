from tortoise import fields
from tortoise.fields.relational import ForeignKeyRelation

from models.base.sheet_config_base import SheetConfigBase
from models.feature_channel import FeatureChannel


class TeamRegisterConfig(SheetConfigBase):
    feature_channel: ForeignKeyRelation[FeatureChannel] = fields.ForeignKeyField(
        "models.FeatureChannel", related_name="team_register"
    )

    team_worksheet_ids: list[int] = fields.JSONField(
        default=list,
        description="List of worksheet ids",
    )
    summary_worksheet_id = fields.IntField(
        description="ID of the summary worksheet for team register"
    )

    encore_role_ids: list[int] = fields.JSONField(
        default=list,
        description="List of encore role ids",
    )

    class Meta:
        table = "team_register"

    def get_worksheet_ids(self) -> list[int]:
        return [*self.team_worksheet_ids, self.summary_worksheet_id]
