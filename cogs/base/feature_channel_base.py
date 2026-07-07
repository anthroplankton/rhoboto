from __future__ import annotations

import logging
from abc import ABCMeta, abstractmethod
from dataclasses import dataclass
from enum import Enum, auto
from typing import TYPE_CHECKING

from discord import Interaction, Message, app_commands
from discord.ext import commands

from bot import config
from cogs.base.discord_context import require_guild_channel_source
from cogs.base.feature_channel_context import (
    ConfiguredFeatureChannelContext,
    FeatureChannelContext,
    FeatureChannelContextMixin,
    MessageParseResult,
    MessageParseStatus,
)
from components.ui_feature_channel import DisableAndClearConfirmView
from components.ui_settings_flow import (
    initial_setup_content,
    send_current_panel_followup,
    send_settings_view_followup,
)
from components.ui_storage_errors import (
    mark_storage_message_failure,
    send_storage_error,
)
from models.feature_channel import FeatureChannel
from utils.announcement_languages import (
    ANNOUNCEMENT_RENDER_FAILURE_MESSAGE,
    render_announcement_messages,
)
from utils.google_sheets_urls import google_sheet_url_with_gid
from utils.manager_base import ManagerBase
from utils.message_templates import locale_to_template_code, render_message_template
from utils.reactions import add_reaction_if_possible
from utils.storage_errors import (
    StorageError,
    StorageOperationContext,
    classify_storage_exception,
    generate_error_reference,
    storage_error_content,
)
from utils.structs_base import GoogleSheetsMetadata, UserInfo

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from discord.ui import View

    from bot import Rhoboto
    from cogs.base.discord_context import GuildChannelSource
    from components.ui_settings_flow import SettingsPanel
    from models.base.sheet_config_base import SheetConfigBase
    from utils.announcement_languages import RenderedAnnouncement
    from utils.key_async_lock import KeyAsyncLock


class _MessageUpsertStatus(Enum):
    IGNORED = auto()
    INVALID = auto()
    MISSING_CONFIG = auto()
    PROCESSED = auto()


@dataclass(frozen=True)
class _MessageUpsertOutcome[TUpsertResult]:
    status: _MessageUpsertStatus
    result: TUpsertResult | None = None

    @classmethod
    def ignored(cls) -> _MessageUpsertOutcome[TUpsertResult]:
        return cls(status=_MessageUpsertStatus.IGNORED)

    @classmethod
    def invalid(cls) -> _MessageUpsertOutcome[TUpsertResult]:
        return cls(status=_MessageUpsertStatus.INVALID)

    @classmethod
    def missing_config(cls) -> _MessageUpsertOutcome[TUpsertResult]:
        return cls(status=_MessageUpsertStatus.MISSING_CONFIG)

    @classmethod
    def processed(
        cls,
        result: TUpsertResult | None,
    ) -> _MessageUpsertOutcome[TUpsertResult]:
        return cls(status=_MessageUpsertStatus.PROCESSED, result=result)


async def _send_public_announcement_followups(
    interaction: Interaction,
    announcements: Sequence[RenderedAnnouncement],
) -> bool:
    if not announcements:
        await interaction.followup.send(
            ANNOUNCEMENT_RENDER_FAILURE_MESSAGE,
            ephemeral=True,
        )
        return False

    for announcement in announcements:
        await interaction.followup.send(
            announcement.content,
            ephemeral=False,
        )
    return True


class CogABCMeta(commands.CogMeta, ABCMeta):
    pass


class FeatureNotEnabled(commands.CheckFailure, app_commands.CheckFailure):
    """
    Exception raised when a required feature is not enabled in the channel.

    Args:
        feature_display_name (str): Human-facing name of the feature.
    """

    def __init__(self, feature_display_name: str) -> None:
        msg = f"{feature_display_name} is not enabled in this channel."
        super().__init__(msg)


class StorageCheckFailure(commands.CheckFailure, app_commands.CheckFailure):
    """Exception raised when a feature-enabled check cannot read storage safely."""

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
    original = getattr(error, "original", None)
    if isinstance(original, StorageCheckFailure):
        return original
    return None


class FeatureChannelErrorHandler:
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
        """Handle command errors for this cog."""
        storage_failure = _storage_check_failure(error)
        if storage_failure is not None:
            reference = generate_error_reference()
            logger = getattr(self, "logger", logging.getLogger(__name__))
            logger.warning(
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
        self, interaction: Interaction, error: app_commands.AppCommandError
    ) -> None:
        """Handle slash command errors for this cog."""
        storage_failure = _storage_check_failure(error)
        if storage_failure is not None:
            await send_storage_error(
                interaction,
                storage_failure.error,
                context=storage_failure.context,
                log=getattr(self, "logger", None),
            )
        elif isinstance(error, FeatureNotEnabled):
            await interaction.response.send_message(str(error), ephemeral=True)
        elif isinstance(error, app_commands.MissingPermissions):
            await interaction.response.send_message(
                "You do not have permission to use this command.", ephemeral=True
            )
        else:
            raise error


@app_commands.guild_only()
@app_commands.default_permissions(administrator=True, manage_channels=True)
class FeatureChannelBase[TManager: ManagerBase, TSubmission, TUpsertResult](
    FeatureChannelContextMixin[TManager],
    FeatureChannelErrorHandler,
    commands.GroupCog,
    metaclass=CogABCMeta,
):
    """
    Base class for per-channel feature management using Tortoise ORM.

    Provides logging and persistent storage for Discord bot features that can be
    enabled or disabled per channel. Only cogs that require channel feature
    management should inherit from this class.

    Attributes:
        feature_name (str): Name of the feature. Should be overridden by subclasses.
        feature_display_name (str): Human-facing name for settings UI text.
    """

    feature_name: str  # Stable feature identifier; overridden by subclasses.
    feature_display_name: str  # Human-facing settings UI label.
    lock: KeyAsyncLock

    ManagerType: type[TManager]  # Type of the manager to use for this feature

    def __init__(self, bot: Rhoboto) -> None:
        self.bot = bot
        self.logger = logging.getLogger(self.__class__.__name__)
        self.context_menu = app_commands.ContextMenu(
            name=f"{self.feature_display_name} Upsert",
            callback=self.upsert_from_content_menu,
        )
        self.context_menu.add_check(
            self.feature_enabled_app_command_predicate(
                self.feature_name,
                self.feature_display_name,
            )
        )
        self.context_menu.error(self.cog_app_command_error)
        bot.tree.add_command(self.context_menu)

    async def _process_feature_channel_message_with_outcome(
        self,
        message: Message,
        feature_channel_context: FeatureChannelContext[TManager],
    ) -> _MessageUpsertOutcome[TUpsertResult]:
        self._log_received_message(message)

        parse_result = await self._parse_message_submission(message)
        if parse_result.status is MessageParseStatus.IGNORED:
            return _MessageUpsertOutcome.ignored()

        context = await self._get_configured_feature_channel_context(
            feature_channel_context
        )
        if context is None:
            self.logger.debug(
                "Feature `%s` in Guild: `%s` Channel: `%s` has no feature config; "
                "ignoring parsed message.",
                self.feature_name,
                feature_channel_context.guild_id,
                feature_channel_context.channel_id,
            )
            return _MessageUpsertOutcome.missing_config()

        if parse_result.status is MessageParseStatus.INVALID:
            await self._add_invalid_registration_reactions(message)
            return _MessageUpsertOutcome.invalid()

        if parse_result.submission is None or parse_result.user_info is None:
            msg = "Parsed message result is missing submission or user info."
            raise ValueError(msg)

        result = await self._process_configured_message_submission(
            message,
            context,
            parse_result.submission,
            parse_result.user_info,
        )
        return _MessageUpsertOutcome.processed(result)

    async def _get_message_feature_channel_context_or_none(
        self,
        message: Message,
    ) -> FeatureChannelContext[TManager] | None:
        if message.author.bot or message.guild is None or message.channel is None:
            return None
        return await self._get_feature_channel_context_or_none(
            guild_id=message.guild.id,
            channel_id=message.channel.id,
            require_enabled=True,
        )

    @abstractmethod
    async def _parse_message_submission(
        self,
        message: Message,
    ) -> MessageParseResult[TSubmission]:
        msg = "Subclasses must implement _parse_message_submission method."
        raise NotImplementedError(msg)

    @abstractmethod
    async def _process_configured_message_submission(
        self,
        message: Message,
        context: ConfiguredFeatureChannelContext[TManager],
        submission: TSubmission,
        user_info: UserInfo,
    ) -> TUpsertResult | None:
        msg = "Subclasses must implement _process_configured_message_submission method."
        raise NotImplementedError(msg)

    def _message_user_info(self, message: Message) -> UserInfo:
        return UserInfo(
            username=message.author.name,
            display_name=message.author.display_name,
        )

    def _log_received_message(self, message: Message) -> None:
        if message.guild is None or message.channel is None:
            return
        self.logger.debug(
            "Received message in Guild: `%s` Channel: `%s` (Feature: `%s`): %r",
            message.guild.id,
            message.channel.id,
            self.feature_name,
            message.content,
        )

    async def _add_invalid_registration_reactions(self, message: Message) -> None:
        await add_reaction_if_possible(
            message,
            config.WARNING_EMOJI,
            log=self.logger,
        )
        await add_reaction_if_possible(
            message,
            config.CONFUSED_EMOJI,
            log=self.logger,
        )

    @app_commands.default_permissions(administrator=True, manage_channels=True)
    async def upsert_from_content_menu(
        self, interaction: Interaction, message: Message
    ) -> None:
        """
        Upsert registration data for this feature from a message (context menu).
        """
        source = require_guild_channel_source(
            interaction,
            action="upsert feature data from context menu",
        )

        await interaction.response.defer(ephemeral=True)

        try:
            feature_channel_context = (
                await self._get_message_feature_channel_context_or_none(message)
            )
            if feature_channel_context is None:
                outcome = _MessageUpsertOutcome.ignored()
            else:
                outcome = await self._process_feature_channel_message_with_outcome(
                    message,
                    feature_channel_context,
                )
        except Exception as exc:
            error = classify_storage_exception(exc)
            if error is None:
                raise
            bot_user = getattr(getattr(self, "bot", None), "user", None)
            reference = generate_error_reference()
            await mark_storage_message_failure(
                message,
                bot_user,
                error,
                context=StorageOperationContext(
                    operation="context_menu_upsert",
                    feature_name=self.feature_name,
                    guild_id=source.guild.id,
                    channel_id=source.channel.id,
                    message_id=message.id,
                ),
                reference_id=reference,
                log=getattr(self, "logger", None),
            )
            await send_storage_error(
                interaction,
                error,
                context=self._interaction_storage_context(
                    source,
                    "context_menu_upsert",
                ),
                reference_id=reference,
                log=getattr(self, "logger", None),
            )
            return

        if outcome.status is _MessageUpsertStatus.MISSING_CONFIG:
            await self._send_missing_config_followup(interaction)
            return

        content = (
            f"Failed to upsert for {self.feature_display_name}."
            if outcome.result is None
            else (
                f"Upsert for {self.feature_display_name} complete. "
                f"Data: ```js\n{outcome.result}```"
            )
        )
        await interaction.followup.send(content, ephemeral=True)

    @commands.Cog.listener()
    async def on_message(self, message: Message) -> None:
        """
        Listen for messages to provide a button for team register setup/edit.
        This is used in channels where the feature is enabled.
        """
        if message.author.bot or message.guild is None or message.channel is None:
            return

        try:
            feature_channel_context = (
                await self._get_message_feature_channel_context_or_none(message)
            )
        except Exception as exc:
            error = classify_storage_exception(exc)
            if error is None:
                raise
            reference = generate_error_reference()
            self.logger.warning(
                (
                    "Message listener storage lookup failed before filtering. "
                    "reference=%s operation=%s feature=%s guild=%s channel=%s "
                    "message=%s kind=%s hint=%s"
                ),
                reference,
                "message_lookup",
                self.feature_name,
                message.guild.id,
                message.channel.id,
                message.id,
                error.kind.value,
                error.log_hint,
            )
            return
        if feature_channel_context is None:
            return

        try:
            await self._process_feature_channel_message_with_outcome(
                message,
                feature_channel_context,
            )
        except Exception as exc:
            error = classify_storage_exception(exc)
            if error is None:
                raise
            await mark_storage_message_failure(
                message,
                self.bot.user,
                error,
                context=StorageOperationContext(
                    operation="message_upsert",
                    feature_name=self.feature_name,
                    guild_id=message.guild.id,
                    channel_id=message.channel.id,
                    message_id=message.id,
                ),
                log=self.logger,
            )

    async def setup_after_enable(self, interaction: Interaction) -> None:
        """Show current settings or prompt to set up if not configured."""
        source = require_guild_channel_source(
            interaction,
            action="set up feature settings",
        )
        initial_setup_view = None
        panel = None
        try:
            feature_channel_context = await self._get_feature_channel_context(source)
            context = await self._get_configured_feature_channel_context(
                feature_channel_context
            )
            if context is None:
                initial_setup_view = self._build_initial_setup_view(
                    feature_channel_context.manager
                )
            else:
                panel = await self._build_settings_panel(
                    interaction,
                    context.manager,
                    context.feature_config,
                )
        except Exception as exc:  # noqa: BLE001
            await self._send_interaction_storage_error_or_raise(
                interaction,
                exc,
                source=source,
                operation="setup_after_enable",
            )
            return

        if initial_setup_view is not None:
            await send_settings_view_followup(
                interaction,
                content=initial_setup_content(self.feature_display_name),
                view=initial_setup_view,
            )
            return

        if panel is None:
            return
        await send_current_panel_followup(interaction, panel)

    @abstractmethod
    def _build_initial_setup_view(self, manager: TManager) -> View:
        """Build the initial setup view for a feature with no sheet config."""
        msg = "Subclasses must implement _build_initial_setup_view method."
        raise NotImplementedError(msg)

    @abstractmethod
    async def _build_settings_panel(
        self,
        interaction: Interaction,
        manager: TManager,
        sheet_config: object,
    ) -> SettingsPanel:
        """Build the current settings panel for a configured feature."""
        msg = "Subclasses must implement _build_settings_panel method."
        raise NotImplementedError(msg)

    @app_commands.command(
        name="enable", description="Enable this feature in the current channel."
    )
    async def enable(self, interaction: Interaction) -> None:
        """Slash command to enable this feature in the current channel."""
        source = require_guild_channel_source(
            interaction,
            action="proceed with enable command",
        )
        try:
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
        """Slash command to disable this feature in the current channel."""
        source = require_guild_channel_source(
            interaction,
            action="proceed with disable command",
        )
        try:
            result = await self._disable_channel(source.guild.id, source.channel.id)
        except Exception as exc:  # noqa: BLE001
            await self._send_interaction_storage_error_or_raise(
                interaction,
                exc,
                source=source,
                operation="disable",
            )
            return

        msg = (
            f"Feature {self.feature_display_name} disabled in this channel."
            if result
            else (
                f"Feature {self.feature_display_name} is not enabled in this channel."
            )
        )
        await interaction.response.send_message(msg, ephemeral=True)

    @app_commands.command(
        name="disable_and_clear",
        description=(
            "Disable and permanently clear all bot settings for this "
            "feature in this channel."
        ),
    )
    async def disable_and_clear(self, interaction: Interaction) -> None:
        """
        Slash command to disable this feature and permanently clear all bot
        feature settings for this feature in the current channel.
        """
        source = require_guild_channel_source(
            interaction,
            action="proceed with disable and clear command",
        )
        view = DisableAndClearConfirmView()
        await interaction.response.send_message(
            f"Are you sure you want to disable and clear all settings for feature "
            f"{self.feature_display_name} in this channel?",
            view=view,
            ephemeral=True,
        )
        # Wait for user interaction
        await view.wait()
        if view.value:
            try:
                await self._clear_feature_settings(source.guild.id, source.channel.id)
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
        elif view.value is False:
            # Already sent cancel message in view
            pass
        else:
            await interaction.followup.send(
                "No response received. Operation timed out.", ephemeral=True
            )

    guide_template_key: str

    def _guide_worksheet_id(
        self,
        _feature_config: SheetConfigBase,
    ) -> int | None:
        return None

    def _guide_sheet_url(
        self,
        feature_config: SheetConfigBase,
    ) -> str:
        return google_sheet_url_with_gid(
            feature_config.sheet_url,
            self._guide_worksheet_id(feature_config),
        )

    async def send_guide_message(self, interaction: Interaction) -> None:
        """
        Post guide announcements for this feature.
        """
        await interaction.response.defer(ephemeral=False)
        source = require_guild_channel_source(
            interaction,
            action=f"post {self.feature_display_name} guide announcement",
        )

        try:
            feature_channel_context = await self._get_feature_channel_context(source)
            context = await self._get_configured_feature_channel_context(
                feature_channel_context
            )
            if context is None:
                await self._send_missing_config_followup(interaction)
                return

            bot_mention = self.bot.user.mention if self.bot.user is not None else "@Bot"
            announcements = await render_announcement_messages(
                self.guide_template_key,
                context.guild_id,
                self.logger,
                bot=bot_mention,
                sheet_url=self._guide_sheet_url(context.feature_config),
            )
        except Exception as exc:  # noqa: BLE001
            await self._send_interaction_storage_error_or_raise(
                interaction,
                exc,
                source=source,
                operation="send_guide_announcement",
            )
            return

        await _send_public_announcement_followups(interaction, announcements)

    async def _enable_channel(self, guild_id: int, channel_id: int) -> None:
        """
        Set is_enabled=True for this feature in the specified guild/channel.

        Args:
            guild_id (int): Discord guild ID.
            channel_id (int): Discord channel ID.
        """
        feature_channel, _ = await FeatureChannel.get_or_create(
            guild_id=guild_id,
            channel_id=channel_id,
            feature_name=self.feature_name,
        )
        feature_channel.is_enabled = True
        await feature_channel.save()
        self.logger.info(
            "Enabled Feature: `%s` in Guild: `%s` Channel: `%s`",
            self.feature_name,
            guild_id,
            channel_id,
        )

    async def _disable_channel(self, guild_id: int, channel_id: int) -> bool:
        """
        Set is_enabled=False for this feature in the specified guild/channel.

        Args:
            guild_id (int): Discord guild ID.
            channel_id (int): Discord channel ID.

        Returns:
            bool: True if disabled, False if no record.
        """
        feature_channel = await FeatureChannel.get_or_none(
            guild_id=guild_id,
            channel_id=channel_id,
            feature_name=self.feature_name,
        )
        if feature_channel:
            feature_channel.is_enabled = False
            await feature_channel.save()
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
        """
        Permanently delete all bot feature settings for this feature in the specified
        guild/channel.

        Args:
            guild_id (int): Discord guild ID.
            channel_id (int): Discord channel ID.
        """
        deleted = await FeatureChannel.filter(
            guild_id=guild_id,
            channel_id=channel_id,
            feature_name=self.feature_name,
        ).delete()
        self.logger.info(
            "Cleared %d feature settings for "
            "Feature: `%s` in Guild: `%s` Channel: `%s`",
            deleted,
            self.feature_name,
            guild_id,
            channel_id,
        )

    @classmethod
    async def is_enabled(
        cls, guild_id: int, channel_id: int, feature_name: str | None = None
    ) -> bool:
        """
        Check if the feature is enabled in the specified guild/channel.

        Args:
            guild_id (int): Discord guild ID.
            channel_id (int): Discord channel ID.
            feature_name (str | None, optional): Feature name to check.
                Defaults to None.

        Returns:
            bool: True if enabled, False otherwise.
        """
        if feature_name is None:
            feature_name = cls.feature_name
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
    ) -> Callable:
        """
        Predicate for prefix command: require feature to be enabled in this channel.

        Args:
            feature_name (str): Feature name to check.
            feature_display_name (str): Human-facing feature name for errors.

        Returns:
            callable: Predicate function for command check.
        """

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
                raise FeatureNotEnabled(feature_display_name)
            return True

        return predicate

    @staticmethod
    def feature_enabled_app_command_predicate(
        feature_name: str,
        feature_display_name: str,
    ) -> Callable:
        """
        Predicate for app command: require feature to be enabled in this channel.

        Args:
            feature_name (str): Feature name to check.
            feature_display_name (str): Human-facing feature name for errors.

        Returns:
            callable: Predicate function for app command check.
        """

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
                raise FeatureNotEnabled(feature_display_name)
            return True

        return predicate


@app_commands.guild_only()
class FeatureChannelUserBase[
    TFeatureChannel: FeatureChannelBase,
    TManager: ManagerBase,
    TGoogleSheetsMetadata: GoogleSheetsMetadata,
](
    FeatureChannelContextMixin[TManager],
    FeatureChannelErrorHandler,
    commands.GroupCog,
    metaclass=CogABCMeta,
):
    """
    Generic base class for per-channel, per-user data management commands
    (e.g., delete own data).
    Subclasses must define:
      - feature_name: str
      - ManagerType: type[TManager]
      - FeatureChannelType: type[TFeatureChannel]
    And must implement:
      - async def _delete_user_data(
            self,
            manager: TManager,
            user_info: UserInfo,
            metadata: TGoogleSheetsMetadata
        ) -> None
    """

    feature_name: str = NotImplemented

    FeatureChannelType: type[TFeatureChannel]
    ManagerType: type[TManager]
    GoogleSheetsMetadataType: type[TGoogleSheetsMetadata]

    def __init__(self, bot: Rhoboto) -> None:
        self.bot = bot
        self.logger = logging.getLogger(self.__class__.__name__)

    @abstractmethod
    async def _delete_user_data(
        self, manager: TManager, user_info: UserInfo, metadata: TGoogleSheetsMetadata
    ) -> None: ...

    def _guide_worksheet_id(
        self,
        _feature_config: SheetConfigBase,
    ) -> int | None:
        return None

    def _guide_sheet_url(
        self,
        feature_config: SheetConfigBase,
    ) -> str:
        return google_sheet_url_with_gid(
            feature_config.sheet_url,
            self._guide_worksheet_id(feature_config),
        )

    async def delete_callback(self, interaction: Interaction) -> None:
        """
        Delete the user's data for this feature in this channel.
        """
        source = require_guild_channel_source(
            interaction,
            action="delete feature user data",
        )

        await interaction.response.defer(ephemeral=True)

        try:
            user_info = UserInfo(
                username=interaction.user.name,
                display_name=interaction.user.display_name,
            )

            feature_channel_context = await self._get_feature_channel_context(source)
            context = await self._get_configured_feature_channel_context(
                feature_channel_context
            )
            if context is None:
                await self._send_missing_config_followup(interaction)
                return

            manager = context.manager

            async with self.FeatureChannelType.lock(context.channel_id):
                metadata = await manager.fetch_google_sheets_metadata()
                await self._delete_user_data(manager, user_info, metadata)

            locale = interaction.locale.value

            if locale.startswith("zh"):
                content = f"✅ 已成功刪除 {self.feature_display_name} 登記的資料。"
            elif locale.startswith("ja"):
                content = (
                    f"✅ {self.feature_display_name} の入力データを正常に削除しました。"
                )
            else:
                content = (
                    f"✅ Your data for {self.feature_display_name} has been "
                    f"deleted successfully."
                )
        except Exception as exc:  # noqa: BLE001
            await self._send_interaction_storage_error_or_raise(
                interaction,
                exc,
                source=source,
                operation="delete_user_data",
            )
            return

        await interaction.followup.send(content=content, ephemeral=True)

    async def send_guide_message(
        self,
        interaction: Interaction,
        template_key: str,
    ) -> None:
        """
        Send an ephemeral guide message for this feature.
        """

        await interaction.response.defer(ephemeral=True)
        source = require_guild_channel_source(
            interaction,
            action=f"send {self.feature_display_name} guide message",
        )

        try:
            feature_channel_context = await self._get_feature_channel_context(source)
            context = await self._get_configured_feature_channel_context(
                feature_channel_context
            )
            if context is None:
                await self._send_missing_config_followup(interaction)
                return

            locale = locale_to_template_code(interaction.locale.value)
            bot_mention = self.bot.user.mention if self.bot.user is not None else "@Bot"
            content = render_message_template(
                template_key,
                locale,
                bot=bot_mention,
                sheet_url=self._guide_sheet_url(context.feature_config),
            )
        except Exception as exc:  # noqa: BLE001
            await self._send_interaction_storage_error_or_raise(
                interaction,
                exc,
                source=source,
                operation="send_guide_message",
            )
            return

        await interaction.followup.send(content, ephemeral=True)
