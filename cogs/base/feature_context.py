from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto
from typing import TYPE_CHECKING, Generic, TypeVar

from bot import config
from models.feature_channel import FeatureChannel
from utils.manager_base import ManagerBase

if TYPE_CHECKING:
    from discord import Guild, Interaction

    from models.base.sheet_config_base import SheetConfigBase
    from utils.structs_base import UserInfo


TManager = TypeVar("TManager", bound=ManagerBase)
TSubmission = TypeVar("TSubmission")


@dataclass(frozen=True)
class InteractionChannelContext:
    guild: Guild
    guild_id: int
    channel_id: int


@dataclass(frozen=True)
class FeatureManagerContext(Generic[TManager]):
    guild: Guild
    guild_id: int
    channel_id: int
    feature_channel: FeatureChannel
    manager: TManager


@dataclass(frozen=True)
class ConfiguredFeatureContext(Generic[TManager]):
    guild: Guild
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
class MessageParseResult(Generic[TSubmission]):
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


class FeatureContextMixin(Generic[TManager]):
    feature_name: str
    feature_display_name: str
    ManagerType: type[TManager]

    def _get_interaction_channel_context(
        self,
        interaction: Interaction,
    ) -> InteractionChannelContext:
        if interaction.channel is None or interaction.guild is None:
            msg = "Cannot proceed without an interaction channel and guild."
            raise ValueError(msg)

        return InteractionChannelContext(
            guild=interaction.guild,
            guild_id=interaction.guild.id,
            channel_id=interaction.channel.id,
        )

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

    async def _get_feature_manager_context(
        self,
        interaction_context: InteractionChannelContext,
    ) -> FeatureManagerContext[TManager]:
        feature_channel = await FeatureChannel.get(
            guild_id=interaction_context.guild_id,
            channel_id=interaction_context.channel_id,
            feature_name=self.feature_name,
        )
        return self._build_feature_manager_context(
            guild=interaction_context.guild,
            channel_id=interaction_context.channel_id,
            feature_channel=feature_channel,
        )

    def _build_feature_manager_context(
        self,
        *,
        guild: Guild,
        channel_id: int,
        feature_channel: FeatureChannel,
    ) -> FeatureManagerContext[TManager]:
        manager = self.ManagerType(feature_channel, config.GOOGLE_SERVICE_ACCOUNT_PATH)
        return FeatureManagerContext(
            guild=guild,
            guild_id=guild.id,
            channel_id=channel_id,
            feature_channel=feature_channel,
            manager=manager,
        )

    async def _get_feature_manager_context_or_none(
        self,
        *,
        guild: Guild,
        channel_id: int,
        require_enabled: bool = False,
    ) -> FeatureManagerContext[TManager] | None:
        if require_enabled:
            feature_channel = await self._get_enabled_feature_channel_or_none(
                guild.id,
                channel_id,
                self.feature_name,
            )
        else:
            feature_channel = await FeatureChannel.get_or_none(
                guild_id=guild.id,
                channel_id=channel_id,
                feature_name=self.feature_name,
            )
        if feature_channel is None:
            return None

        return self._build_feature_manager_context(
            guild=guild,
            channel_id=channel_id,
            feature_channel=feature_channel,
        )

    async def _get_configured_feature_context(
        self,
        manager_context: FeatureManagerContext[TManager],
    ) -> ConfiguredFeatureContext[TManager] | None:
        feature_config = await manager_context.manager.get_sheet_config_or_none()
        if feature_config is None:
            return None

        return ConfiguredFeatureContext(
            guild=manager_context.guild,
            guild_id=manager_context.guild_id,
            channel_id=manager_context.channel_id,
            feature_channel=manager_context.feature_channel,
            manager=manager_context.manager,
            feature_config=feature_config,
        )

    async def _send_missing_config_followup(
        self,
        interaction: Interaction,
        *,
        ephemeral: bool = True,
    ) -> None:
        await interaction.followup.send(
            content=f"{self.feature_display_name} is not configured for this channel.",
            ephemeral=ephemeral,
        )
