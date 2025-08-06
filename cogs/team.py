from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, override

from discord import Interaction, app_commands
from discord.app_commands import locale_str

from cogs.base.feature_channel_base import FeatureChannelBase, FeatureChannelUserBase
from cogs.team_register import TeamRegister
from utils.team_register_manager import TeamRegisterManager
from utils.team_register_structs import TeamRegisterGoogleSheetsMetadata

if TYPE_CHECKING:
    from bot import Rhoboto
    from utils.structs_base import UserInfo


class Team(
    FeatureChannelUserBase[
        TeamRegister, TeamRegisterManager, TeamRegisterGoogleSheetsMetadata
    ],
    group_name=app_commands.locale_str("team"),
):
    feature_name = TeamRegister.feature_name

    FeatureChannelType = TeamRegister
    ManagerType = TeamRegisterManager
    GoogleSheetsMetadataType = TeamRegisterGoogleSheetsMetadata

    @override
    async def _delete_user_data(
        self,
        manager: TeamRegisterManager,
        user_info: UserInfo,
        metadata: TeamRegisterGoogleSheetsMetadata,
    ) -> None:
        """
        Slash command to delete the user's teams.
        """
        await asyncio.gather(
            manager.delete_user_teams(user_info, metadata),
            manager.delete_user_summary(user_info, metadata),
        )

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
        description=locale_str("Show how to register your teams."),
    )
    @app_commands.check(
        FeatureChannelBase.feature_enabled_app_command_predicate(feature_name)
    )
    async def help(self, interaction: Interaction) -> None:
        """Show how to register your teams.

        Args:
            interaction (Interaction): The Discord interaction.
        """
        await self.send_help_message(
            interaction,
            TeamRegister.help_text_en,
            TeamRegister.help_text_ja,
            TeamRegister.help_text_zh_tw,
        )


async def setup(bot: Rhoboto) -> None:
    await bot.add_cog(Team(bot))
