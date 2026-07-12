from __future__ import annotations

from typing import TYPE_CHECKING, override

from discord import Interaction, app_commands
from discord.app_commands import locale_str

from cogs.base.feature_channel_base import FeatureChannelBase, FeatureChannelUserBase
from cogs.shift_register import ShiftRegister
from utils.shift_register_manager import ShiftRegisterManager
from utils.shift_register_structs import ShiftRegisterGoogleSheetsMetadata

if TYPE_CHECKING:
    from bot import Rhoboto
    from cogs.base.feature_channel_context import ConfiguredFeatureChannelContext
    from utils.structs_base import UserInfo


class Shift(
    FeatureChannelUserBase[
        ShiftRegister, ShiftRegisterManager, ShiftRegisterGoogleSheetsMetadata
    ],
    group_name=locale_str("shift"),
):
    feature_name = ShiftRegister.feature_name
    feature_display_name = ShiftRegister.feature_display_name

    FeatureChannelType = ShiftRegister
    ManagerType = ShiftRegisterManager
    GoogleSheetsMetadataType = ShiftRegisterGoogleSheetsMetadata

    @override
    async def _guide_template_values(
        self,
        context: ConfiguredFeatureChannelContext[ShiftRegisterManager],
    ) -> dict[str, object]:
        values = await super()._guide_template_values(context)
        values[
            "team_source_channel_id"
        ] = await context.manager.get_saved_team_source_channel_id()
        return values

    @override
    async def _delete_user_data(
        self,
        manager: ShiftRegisterManager,
        user_info: UserInfo,
        metadata: ShiftRegisterGoogleSheetsMetadata,
    ) -> None:
        """
        Slash command to delete the user's shift entry (entry worksheet).
        """
        await manager.upsert_or_delete_user_shift(user_info, None, metadata)

    @app_commands.command(
        name=locale_str("delete"),
        description=locale_str("Delete your shift registration in this channel."),
    )
    @app_commands.check(
        FeatureChannelBase.feature_enabled_app_command_predicate(
            feature_name,
            feature_display_name,
        )
    )
    async def delete(self, interaction: Interaction) -> None:
        await self.delete_callback(interaction)

    @app_commands.command(
        name=locale_str("guide"),
        description=locale_str("Show how to register your shifts."),
    )
    @app_commands.check(
        FeatureChannelBase.feature_enabled_app_command_predicate(
            feature_name,
            feature_display_name,
        )
    )
    async def guide(self, interaction: Interaction) -> None:
        """Show how to register your shifts."""
        await self.send_guide_message(
            interaction,
            ShiftRegister.guide_template_key,
        )


async def setup(bot: Rhoboto) -> None:
    await bot.add_cog(Shift(bot))
