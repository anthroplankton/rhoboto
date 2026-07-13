from __future__ import annotations

from typing import TYPE_CHECKING, override

from discord import Interaction, app_commands
from discord.app_commands import locale_str

from cogs.base.feature_channel_base import FeatureChannelBase, FeatureChannelUserBase
from cogs.team_register import TeamRegister
from utils.team_register_manager import (
    TeamRegisterManager,
    fresh_team_channel_transaction,
)
from utils.team_register_structs import TeamRegisterGoogleSheetsMetadata

if TYPE_CHECKING:
    from bot import Rhoboto
    from cogs.base.feature_channel_context import ConfiguredFeatureChannelContext
    from utils.structs_base import UserInfo


class Team(
    FeatureChannelUserBase[
        TeamRegister, TeamRegisterManager, TeamRegisterGoogleSheetsMetadata
    ],
    group_name=app_commands.locale_str("team"),
):
    feature_name = TeamRegister.feature_name
    feature_display_name = TeamRegister.feature_display_name

    FeatureChannelType = TeamRegister
    ManagerType = TeamRegisterManager
    GoogleSheetsMetadataType = TeamRegisterGoogleSheetsMetadata

    @override
    async def _delete_user_data_transaction(
        self,
        context: ConfiguredFeatureChannelContext[TeamRegisterManager],
        user_info: UserInfo,
    ) -> None:
        manager = context.manager
        async with fresh_team_channel_transaction(
            manager,
            self.FeatureChannelType.sheet_write_lock,
            channel_id=context.channel_id,
        ):
            await manager.delete_user_registration(user_info)

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
        del metadata
        await manager.delete_user_registration(user_info)

    @app_commands.command(
        name=locale_str("delete"),
        description=locale_str("Delete your team registration in this channel."),
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
        description=locale_str("Show how to register your teams."),
    )
    @app_commands.check(
        FeatureChannelBase.feature_enabled_app_command_predicate(
            feature_name,
            feature_display_name,
        )
    )
    async def guide(self, interaction: Interaction) -> None:
        """Show how to register your teams."""
        await self.send_guide_message(
            interaction,
            TeamRegister.guide_template_key,
        )


async def setup(bot: Rhoboto) -> None:
    await bot.add_cog(Team(bot))
