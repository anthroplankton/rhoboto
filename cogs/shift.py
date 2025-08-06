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
    from utils.structs_base import UserInfo


class Shift(
    FeatureChannelUserBase[
        ShiftRegister, ShiftRegisterManager, ShiftRegisterGoogleSheetsMetadata
    ],
    group_name=locale_str("shift"),
):

    feature_name = ShiftRegister.feature_name

    FeatureChannelType = ShiftRegister
    ManagerType = ShiftRegisterManager
    GoogleSheetsMetadataType = ShiftRegisterGoogleSheetsMetadata

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
        description=locale_str(
            "Delete your registration data for this feature in this channel."
        ),
    )
    @app_commands.check(
            FeatureChannelBase.feature_enabled_app_command_predicate(feature_name)
    )
    async def delete(self, interaction: Interaction) -> None:
        await self.delete_callback(interaction)

    @app_commands.command(
        name=locale_str("help"),
        description=locale_str("Show how to register your shifts."),
    )
    @app_commands.check(
        FeatureChannelBase.feature_enabled_app_command_predicate(feature_name)
    )
    async def help(self, interaction: Interaction) -> None:
        """Show how to register your shifts.

        Args:
            interaction (Interaction): The Discord interaction.
        """
        await self.send_help_message(
            interaction,
            ShiftRegister.help_text_en,
            ShiftRegister.help_text_ja,
            ShiftRegister.help_text_zh_tw,
        )


async def setup(bot: Rhoboto) -> None:
    await bot.add_cog(Shift(bot))
