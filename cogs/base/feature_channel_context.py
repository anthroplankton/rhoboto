from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto
from typing import TYPE_CHECKING

from bot import config
from models.feature_channel import FeatureChannel
from utils.manager_base import ManagerBase
from utils.register_i18n import register_user_text

if TYPE_CHECKING:
    from discord import Interaction

    from cogs.base.discord_context import GuildChannelSource
    from models.base.sheet_config_base import SheetConfigBase
    from utils.structs_base import UserInfo


@dataclass(frozen=True)
class FeatureChannelContext[TManager: ManagerBase]:
    guild_id: int
    channel_id: int
    feature_channel: FeatureChannel
    manager: TManager


@dataclass(frozen=True)
class ConfiguredFeatureChannelContext[TManager: ManagerBase]:
    guild_id: int
    channel_id: int
    feature_channel: FeatureChannel
    manager: TManager
    feature_config: SheetConfigBase


class MessageParseStatus(Enum):
    IGNORED = auto()
    INVALID = auto()
    PARSED = auto()


@dataclass(frozen=True)
class MessageParseResult[TSubmission]:
    status: MessageParseStatus
    submission: TSubmission | None
    user_info: UserInfo | None

    @classmethod
    def ignored(cls) -> MessageParseResult[TSubmission]:
        return cls(
            status=MessageParseStatus.IGNORED,
            submission=None,
            user_info=None,
        )

    @classmethod
    def invalid(cls, *, user_info: UserInfo) -> MessageParseResult[TSubmission]:
        return cls(
            status=MessageParseStatus.INVALID,
            submission=None,
            user_info=user_info,
        )

    @classmethod
    def parsed(
        cls,
        submission: TSubmission,
        *,
        user_info: UserInfo,
    ) -> MessageParseResult[TSubmission]:
        return cls(
            status=MessageParseStatus.PARSED,
            submission=submission,
            user_info=user_info,
        )


class FeatureChannelContextMixin[TManager: ManagerBase]:
    feature_name: str
    feature_display_name: str
    ManagerType: type[TManager]

    @classmethod
    async def _get_enabled_feature_channel_or_none(
        cls,
        guild_id: int,
        channel_id: int,
        feature_name: str | None = None,
    ) -> FeatureChannel | None:
        if feature_name is None:
            feature_name = cls.feature_name

        feature_channel = await FeatureChannel.get_or_none(
            guild_id=guild_id,
            channel_id=channel_id,
            feature_name=feature_name,
        )
        if feature_channel is None or not feature_channel.is_enabled:
            return None
        return feature_channel

    async def _get_feature_channel_context(
        self,
        source: GuildChannelSource,
    ) -> FeatureChannelContext[TManager]:
        feature_channel = await FeatureChannel.get(
            guild_id=source.guild.id,
            channel_id=source.channel.id,
            feature_name=self.feature_name,
        )
        return self._build_feature_channel_context(
            guild_id=source.guild.id,
            channel_id=source.channel.id,
            feature_channel=feature_channel,
        )

    def _build_feature_channel_context(
        self,
        *,
        guild_id: int,
        channel_id: int,
        feature_channel: FeatureChannel,
    ) -> FeatureChannelContext[TManager]:
        manager = self.ManagerType(feature_channel, config.GOOGLE_SERVICE_ACCOUNT_PATH)
        return FeatureChannelContext(
            guild_id=guild_id,
            channel_id=channel_id,
            feature_channel=feature_channel,
            manager=manager,
        )

    async def _get_feature_channel_context_or_none(
        self,
        *,
        guild_id: int,
        channel_id: int,
        require_enabled: bool = False,
    ) -> FeatureChannelContext[TManager] | None:
        if require_enabled:
            feature_channel = await self._get_enabled_feature_channel_or_none(
                guild_id,
                channel_id,
                self.feature_name,
            )
        else:
            feature_channel = await FeatureChannel.get_or_none(
                guild_id=guild_id,
                channel_id=channel_id,
                feature_name=self.feature_name,
            )
        if feature_channel is None:
            return None

        return self._build_feature_channel_context(
            guild_id=guild_id,
            channel_id=channel_id,
            feature_channel=feature_channel,
        )

    async def _get_configured_feature_channel_context(
        self,
        feature_channel_context: FeatureChannelContext[TManager],
    ) -> ConfiguredFeatureChannelContext[TManager] | None:
        feature_config = (
            await feature_channel_context.manager.get_sheet_config_or_none()
        )
        if feature_config is None:
            return None

        return ConfiguredFeatureChannelContext(
            guild_id=feature_channel_context.guild_id,
            channel_id=feature_channel_context.channel_id,
            feature_channel=feature_channel_context.feature_channel,
            manager=feature_channel_context.manager,
            feature_config=feature_config,
        )

    async def _send_missing_config_followup(
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
