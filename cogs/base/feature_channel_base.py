from __future__ import annotations

import logging
from abc import ABCMeta, abstractmethod
from typing import TYPE_CHECKING

from discord import Interaction, app_commands
from discord.ext import commands

from cogs.base.discord_context import require_guild_channel_source
from components.ui_feature_channel import DisableAndClearConfirmView
from components.ui_storage_errors import send_storage_error
from models.feature_channel import FeatureChannel
from utils.register_i18n import register_user_text
from utils.storage_errors import (
    StorageError,
    StorageOperationContext,
    classify_storage_exception,
    generate_error_reference,
    storage_error_content,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from bot import Rhoboto
    from cogs.base.discord_context import GuildChannelSource


class CogABCMeta(commands.CogMeta, ABCMeta):
    """Combine discord.py's cog metaclass with abstract base classes."""


class FeatureNotEnabled(commands.CheckFailure, app_commands.CheckFailure):
    """Raised when a required feature is not enabled in the current channel."""

    def __init__(
        self,
        feature_name: str,
        feature_display_name: str,
        *,
        locale: str = "en",
    ) -> None:
        message = register_user_text(
            feature_name,
            locale,
            "not_enabled",
            fallback_display_name=feature_display_name,
        )
        super().__init__(message)


class StorageCheckFailure(commands.CheckFailure, app_commands.CheckFailure):
    """Raised when a feature-enabled check cannot read storage safely."""

    def __init__(
        self,
        error: StorageError,
        context: StorageOperationContext,
    ) -> None:
        super().__init__("Feature storage check failed.")
        self.error = error
        self.context = context


def _storage_check_failure(error: Exception) -> StorageCheckFailure | None:
    if isinstance(error, StorageCheckFailure):
        return error
    if isinstance(
        error,
        (commands.CommandInvokeError, app_commands.CommandInvokeError),
    ) and isinstance(error.original, StorageCheckFailure):
        return error.original
    return None


class FeatureChannelErrorHandler:
    """Centralized Discord command handling for feature-channel failures."""

    feature_name: str
    logger: logging.Logger

    def _interaction_storage_context(
        self,
        source: GuildChannelSource,
        operation: str,
    ) -> StorageOperationContext:
        return StorageOperationContext(
            operation=operation,
            feature_name=self.feature_name,
            guild_id=source.guild.id,
            channel_id=source.channel.id,
        )

    async def _send_interaction_storage_error_or_raise(
        self,
        interaction: Interaction,
        exc: Exception,
        *,
        source: GuildChannelSource,
        operation: str,
    ) -> None:
        error = classify_storage_exception(exc)
        if error is None:
            raise exc

        await send_storage_error(
            interaction,
            error,
            context=self._interaction_storage_context(source, operation),
            log=self.logger,
        )

    async def cog_command_error(self, ctx: commands.Context, error: Exception) -> None:
        """Handle prefix-command errors for this cog."""
        storage_failure = _storage_check_failure(error)
        if storage_failure is not None:
            reference = generate_error_reference()
            self.logger.warning(
                (
                    "Storage check failed. reference=%s operation=%s feature=%s "
                    "guild=%s channel=%s kind=%s hint=%s"
                ),
                reference,
                storage_failure.context.operation,
                storage_failure.context.feature_name,
                storage_failure.context.guild_id,
                storage_failure.context.channel_id,
                storage_failure.error.kind.value,
                storage_failure.error.log_hint,
            )
            await ctx.reply(
                storage_error_content(storage_failure.error, reference_id=reference)
            )
        elif isinstance(error, FeatureNotEnabled):
            await ctx.reply(str(error))
        elif isinstance(error, commands.MissingPermissions):
            await ctx.reply("You do not have permission to use this command.")
        else:
            raise error

    async def cog_app_command_error(
        self,
        interaction: Interaction,
        error: app_commands.AppCommandError,
    ) -> None:
        """Handle application-command errors for this cog."""
        storage_failure = _storage_check_failure(error)
        if storage_failure is not None:
            await send_storage_error(
                interaction,
                storage_failure.error,
                context=storage_failure.context,
                log=self.logger,
            )
        elif isinstance(error, FeatureNotEnabled):
            await interaction.response.send_message(str(error), ephemeral=True)
        elif isinstance(error, app_commands.MissingPermissions):
            await interaction.response.send_message(
                "You do not have permission to use this command.",
                ephemeral=True,
            )
        else:
            raise error


@app_commands.guild_only()
@app_commands.default_permissions(administrator=True, manage_channels=True)
class FeatureChannelBase(
    FeatureChannelErrorHandler,
    commands.GroupCog,
    metaclass=CogABCMeta,
):
    """Shared lifecycle for a feature enabled per Discord channel."""

    feature_name: str
    feature_display_name: str

    def __init__(self, bot: Rhoboto) -> None:
        self.bot = bot
        self.logger = logging.getLogger(self.__class__.__name__)

    @abstractmethod
    async def setup_after_enable(self, interaction: Interaction) -> None:
        """Run the feature-specific initial setup flow after enabling."""

    async def _validate_lifecycle_owner(
        self,
        source: GuildChannelSource,
    ) -> None:
        """Validate that the current channel owns lifecycle operations."""
        del source

    async def _cleanup_after_disable(
        self,
        membership: FeatureChannel,
    ) -> str | None:
        """Run optional cleanup after a successful soft disable."""
        del membership
        return None

    async def _cleanup_before_clear(
        self,
        membership: FeatureChannel,
    ) -> str | None:
        """Run optional cleanup before hard-clear deletes persisted state."""
        del membership
        return None

    @classmethod
    async def _get_feature_channel_or_none(
        cls,
        guild_id: int,
        channel_id: int,
        feature_name: str | None = None,
        *,
        require_enabled: bool = False,
    ) -> FeatureChannel | None:
        resolved_feature_name = feature_name or cls.feature_name
        membership = await FeatureChannel.get_or_none(
            guild_id=guild_id,
            channel_id=channel_id,
            feature_name=resolved_feature_name,
        )
        if membership is None or (require_enabled and not membership.is_enabled):
            return None
        return membership

    @classmethod
    async def _get_enabled_feature_channel_or_none(
        cls,
        guild_id: int,
        channel_id: int,
        feature_name: str | None = None,
    ) -> FeatureChannel | None:
        return await cls._get_feature_channel_or_none(
            guild_id,
            channel_id,
            feature_name,
            require_enabled=True,
        )

    @app_commands.command(
        name="enable",
        description="Enable this feature in the current channel.",
    )
    async def enable(self, interaction: Interaction) -> None:
        """Enable this feature in the current channel."""
        source = require_guild_channel_source(
            interaction,
            action="proceed with enable command",
        )
        try:
            await self._validate_lifecycle_owner(source)
            await self._enable_channel(source.guild.id, source.channel.id)
        except Exception as exc:  # noqa: BLE001
            await self._send_interaction_storage_error_or_raise(
                interaction,
                exc,
                source=source,
                operation="enable",
            )
            return

        await interaction.response.send_message(
            f"Feature {self.feature_display_name} enabled in this channel.",
            ephemeral=True,
        )
        await self.setup_after_enable(interaction)

    @app_commands.command(
        name="disable",
        description="Disable this feature in the current channel (soft disable).",
    )
    async def disable(self, interaction: Interaction) -> None:
        """Soft-disable this feature while preserving its settings."""
        source = require_guild_channel_source(
            interaction,
            action="proceed with disable command",
        )
        cleanup_warning: str | None = None
        try:
            await self._validate_lifecycle_owner(source)
            membership = await self._get_feature_channel_or_none(
                source.guild.id,
                source.channel.id,
                require_enabled=True,
            )
            if membership is None:
                result = False
                self.logger.info(
                    "No enabled record to disable for Feature: `%s` in "
                    "Guild: `%s` Channel: `%s`",
                    self.feature_name,
                    source.guild.id,
                    source.channel.id,
                )
            else:
                result = await self._disable_channel(
                    source.guild.id,
                    source.channel.id,
                )
                if result:
                    cleanup_warning = await self._cleanup_after_disable(membership)
        except Exception as exc:  # noqa: BLE001
            await self._send_interaction_storage_error_or_raise(
                interaction,
                exc,
                source=source,
                operation="disable",
            )
            return

        message = (
            f"Feature {self.feature_display_name} disabled in this channel."
            if result
            else f"Feature {self.feature_display_name} is not enabled in this channel."
        )
        await interaction.response.send_message(message, ephemeral=True)
        if cleanup_warning is not None:
            await interaction.followup.send(cleanup_warning, ephemeral=True)

    @app_commands.command(
        name="disable_and_clear",
        description=(
            "Disable and permanently clear all bot settings for this "
            "feature in this channel."
        ),
    )
    async def disable_and_clear(self, interaction: Interaction) -> None:
        """Disable the feature and permanently clear its channel settings."""
        source = require_guild_channel_source(
            interaction,
            action="proceed with disable and clear command",
        )
        try:
            await self._validate_lifecycle_owner(source)
        except Exception as exc:  # noqa: BLE001
            await self._send_interaction_storage_error_or_raise(
                interaction,
                exc,
                source=source,
                operation="disable_and_clear_preflight",
            )
            return

        view = DisableAndClearConfirmView()
        await interaction.response.send_message(
            f"Are you sure you want to disable and clear all settings for feature "
            f"{self.feature_display_name} in this channel?",
            view=view,
            ephemeral=True,
        )
        await view.wait()
        if view.value:
            cleanup_warning: str | None = None
            try:
                membership = await self._get_feature_channel_or_none(
                    source.guild.id,
                    source.channel.id,
                )
                if membership is not None:
                    cleanup_warning = await self._cleanup_before_clear(membership)
                await self._clear_feature_settings(
                    source.guild.id,
                    source.channel.id,
                )
            except Exception as exc:  # noqa: BLE001
                await self._send_interaction_storage_error_or_raise(
                    interaction,
                    exc,
                    source=source,
                    operation="disable_and_clear",
                )
                return
            await interaction.followup.send(
                f"Feature {self.feature_display_name} has been disabled and all bot "
                f"settings for this feature in this channel have been permanently "
                f"cleared.",
                ephemeral=True,
            )
            if cleanup_warning is not None:
                await interaction.followup.send(cleanup_warning, ephemeral=True)
        elif view.value is None:
            await interaction.followup.send(
                "No response received. Operation timed out.",
                ephemeral=True,
            )

    async def _enable_channel(self, guild_id: int, channel_id: int) -> None:
        membership, _ = await FeatureChannel.get_or_create(
            guild_id=guild_id,
            channel_id=channel_id,
            feature_name=self.feature_name,
        )
        membership.is_enabled = True
        await membership.save()
        self.logger.info(
            "Enabled Feature: `%s` in Guild: `%s` Channel: `%s`",
            self.feature_name,
            guild_id,
            channel_id,
        )

    async def _disable_channel(self, guild_id: int, channel_id: int) -> bool:
        membership = await FeatureChannel.get_or_none(
            guild_id=guild_id,
            channel_id=channel_id,
            feature_name=self.feature_name,
        )
        if membership is not None:
            membership.is_enabled = False
            await membership.save()
            self.logger.info(
                "Disabled Feature: `%s` in Guild: `%s` Channel: `%s`",
                self.feature_name,
                guild_id,
                channel_id,
            )
            return True
        self.logger.info(
            "No record to disable for Feature: `%s` in Guild: `%s` Channel: `%s`",
            self.feature_name,
            guild_id,
            channel_id,
        )
        return False

    async def _clear_feature_settings(self, guild_id: int, channel_id: int) -> None:
        deleted = await FeatureChannel.filter(
            guild_id=guild_id,
            channel_id=channel_id,
            feature_name=self.feature_name,
        ).delete()
        self.logger.info(
            "Cleared %d feature settings for Feature: `%s` in Guild: `%s` "
            "Channel: `%s`",
            deleted,
            self.feature_name,
            guild_id,
            channel_id,
        )

    @classmethod
    async def is_enabled(
        cls,
        guild_id: int,
        channel_id: int,
        feature_name: str | None = None,
    ) -> bool:
        return (
            await cls._get_enabled_feature_channel_or_none(
                guild_id,
                channel_id,
                feature_name,
            )
            is not None
        )

    @staticmethod
    def feature_enabled_prefix_command_predicate(
        feature_name: str,
        feature_display_name: str,
    ) -> Callable[[commands.Context], Awaitable[bool]]:
        """Build a prefix-command predicate requiring an enabled membership."""

        async def predicate(ctx: commands.Context) -> bool:
            source = require_guild_channel_source(
                ctx,
                action=f"check feature status for feature: {feature_name}",
            )
            try:
                enabled = await FeatureChannelBase.is_enabled(
                    source.guild.id,
                    source.channel.id,
                    feature_name,
                )
            except Exception as exc:
                error = classify_storage_exception(exc)
                if error is None:
                    raise
                raise StorageCheckFailure(
                    error,
                    StorageOperationContext(
                        operation="feature_check",
                        feature_name=feature_name,
                        guild_id=source.guild.id,
                        channel_id=source.channel.id,
                    ),
                ) from exc
            if not enabled:
                raise FeatureNotEnabled(feature_name, feature_display_name)
            return True

        return predicate

    @staticmethod
    def feature_enabled_app_command_predicate(
        feature_name: str,
        feature_display_name: str,
    ) -> Callable[[Interaction], Awaitable[bool]]:
        """Build an app-command predicate requiring an enabled membership."""

        async def predicate(interaction: Interaction) -> bool:
            source = require_guild_channel_source(
                interaction,
                action=f"check feature status for feature: {feature_name}",
            )
            try:
                enabled = await FeatureChannelBase.is_enabled(
                    source.guild.id,
                    source.channel.id,
                    feature_name,
                )
            except Exception as exc:
                error = classify_storage_exception(exc)
                if error is None:
                    raise
                raise StorageCheckFailure(
                    error,
                    StorageOperationContext(
                        operation="feature_check",
                        feature_name=feature_name,
                        guild_id=source.guild.id,
                        channel_id=source.channel.id,
                    ),
                ) from exc
            if not enabled:
                raise FeatureNotEnabled(
                    feature_name,
                    feature_display_name,
                    locale=interaction.locale.value,
                )
            return True

        return predicate
