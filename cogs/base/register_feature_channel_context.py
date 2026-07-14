from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from bot import config
from models.base.sheet_config_base import SheetConfigBase
from models.feature_channel import FeatureChannel
from utils.manager_base import ManagerBase
from utils.register_i18n import register_user_text
from utils.structs_base import GoogleSheetsMetadata

if TYPE_CHECKING:
    from discord import Interaction

    from cogs.base.discord_context import GuildChannelSource


@dataclass(frozen=True)
class RegisterFeatureChannelContext[ManagerT: ManagerBase]:
    """Operation-scoped Register membership and manager."""

    guild_id: int
    channel_id: int
    feature_channel: FeatureChannel
    manager: ManagerT


@dataclass(frozen=True)
class ConfiguredRegisterFeatureChannelContext[
    ConfigT: SheetConfigBase,
    ManagerT: ManagerBase,
]:
    """Register context narrowed to a persisted sheet configuration."""

    guild_id: int
    channel_id: int
    feature_channel: FeatureChannel
    manager: ManagerT
    feature_config: ConfigT


class RegisterFeatureChannelContextMixin[
    ConfigT: SheetConfigBase,
    MetadataT: GoogleSheetsMetadata,
    ManagerT: ManagerBase[ConfigT, MetadataT],
]:
    """Construct typed operation contexts for Google Sheets Register features."""

    feature_name: str
    feature_display_name: str
    ManagerType: type[ManagerT]

    async def _get_register_feature_channel_context(
        self,
        source: GuildChannelSource,
    ) -> RegisterFeatureChannelContext[ManagerT]:
        membership = await FeatureChannel.get(
            guild_id=source.guild.id,
            channel_id=source.channel.id,
            feature_name=self.feature_name,
        )
        return self._build_register_feature_channel_context(membership)

    def _build_register_feature_channel_context(
        self,
        membership: FeatureChannel,
    ) -> RegisterFeatureChannelContext[ManagerT]:
        manager = self.ManagerType(
            membership,
            config.GOOGLE_SERVICE_ACCOUNT_PATH,
        )
        return RegisterFeatureChannelContext(
            guild_id=membership.guild_id,
            channel_id=membership.channel_id,
            feature_channel=membership,
            manager=manager,
        )

    async def _get_register_feature_channel_context_or_none(
        self,
        *,
        guild_id: int,
        channel_id: int,
        require_enabled: bool = False,
    ) -> RegisterFeatureChannelContext[ManagerT] | None:
        membership = await FeatureChannel.get_or_none(
            guild_id=guild_id,
            channel_id=channel_id,
            feature_name=self.feature_name,
        )
        if membership is None or (require_enabled and not membership.is_enabled):
            return None
        return self._build_register_feature_channel_context(membership)

    async def _get_configured_register_feature_channel_context(
        self,
        context: RegisterFeatureChannelContext[ManagerT],
    ) -> ConfiguredRegisterFeatureChannelContext[ConfigT, ManagerT] | None:
        feature_config = await context.manager.get_sheet_config_or_none()
        if feature_config is None:
            return None
        return ConfiguredRegisterFeatureChannelContext(
            guild_id=context.guild_id,
            channel_id=context.channel_id,
            feature_channel=context.feature_channel,
            manager=context.manager,
            feature_config=feature_config,
        )

    async def _send_missing_register_config_followup(
        self,
        interaction: Interaction,
        *,
        ephemeral: bool = True,
    ) -> None:
        await interaction.followup.send(
            content=register_user_text(
                self.feature_name,
                interaction.locale.value,
                "missing_config",
                fallback_display_name=self.feature_display_name,
            ),
            ephemeral=ephemeral,
        )
