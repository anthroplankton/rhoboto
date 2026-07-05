from __future__ import annotations

import logging
from abc import ABCMeta, abstractmethod
from typing import TYPE_CHECKING, Generic, TypeVar

from discord import Interaction, Message, app_commands
from discord.ext import commands

from bot import config
from cogs.base.feature_context import (
    ConfiguredFeatureContext,
    FeatureContextMixin,
    FeatureManagerContext,
    MessageParseResult,
    MessageParseStatus,
)
from components.ui_feature_channel import DisableAndClearConfirmView
from components.ui_google_sheets_errors import (
    mark_google_sheets_message_failure,
    send_google_sheets_error,
)
from components.ui_settings_flow import (
    initial_setup_content,
    send_current_panel_followup,
    send_settings_view_followup,
)
from models.feature_channel import FeatureChannel
from utils.announcement_languages import (
    ANNOUNCEMENT_RENDER_FAILURE_MESSAGE,
    render_announcement_messages,
)
from utils.google_sheets_errors import GoogleSheetsError
from utils.manager_base import ManagerBase
from utils.message_templates import locale_to_template_code, render_message_template
from utils.reactions import add_reaction_if_possible
from utils.structs_base import GoogleSheetsMetadata, UserInfo

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from discord.ui import View

    from bot import Rhoboto
    from components.ui_settings_flow import SettingsPanel
    from utils.announcement_languages import RenderedAnnouncement
    from utils.key_async_lock import KeyAsyncLock


TFeatureChannel = TypeVar("TFeatureChannel", bound="FeatureChannelBase")
TManager = TypeVar("TManager", bound=ManagerBase)
TGoogleSheetsMetadata = TypeVar("TGoogleSheetsMetadata", bound=GoogleSheetsMetadata)
TSubmission = TypeVar("TSubmission")
TUpsertResult = TypeVar("TUpsertResult")


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
        feature_name (str): Name of the feature that is not enabled.
    """

    def __init__(self, feature_name: str) -> None:
        msg = f"Feature `{feature_name}` not enabled in this channel."
        super().__init__(msg)


class FeatureChannelErrorHandler:
    async def cog_command_error(self, ctx: commands.Context, error: Exception) -> None:
        """Handle command errors for this cog."""
        if isinstance(error, FeatureNotEnabled):
            await ctx.reply(str(error))
        elif isinstance(error, commands.MissingPermissions):
            await ctx.reply("You do not have permission to use this command.")
        else:
            raise error

    async def cog_app_command_error(
        self, interaction: Interaction, error: app_commands.AppCommandError
    ) -> None:
        """Handle slash command errors for this cog."""
        if isinstance(error, FeatureNotEnabled):
            await interaction.response.send_message(str(error), ephemeral=True)
        elif isinstance(error, app_commands.MissingPermissions):
            await interaction.response.send_message(
                "You do not have permission to use this command.", ephemeral=True
            )
        else:
            raise error


@app_commands.guild_only()
@app_commands.default_permissions(administrator=True, manage_channels=True)
class FeatureChannelBase(
    FeatureContextMixin[TManager],
    FeatureChannelErrorHandler,
    commands.GroupCog,
    Generic[TManager, TSubmission, TUpsertResult],
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
            name=f"{self.feature_name} upsert",
            callback=self.upsert_from_content_menu,
        )
        self.context_menu.add_check(
            self.feature_enabled_app_command_predicate(self.feature_name)
        )
        self.context_menu.error(self.cog_app_command_error)
        bot.tree.add_command(self.context_menu)

    async def process_upsert_from_message(
        self, message: Message
    ) -> TUpsertResult | None:
        """Process the message to upsert registration data for this feature.

        Args:
            message (Message): The Discord message to process.
        """
        manager_context = await self._get_message_feature_manager_context_or_none(
            message
        )
        if manager_context is None:
            return None

        self._log_received_message(message)

        parse_result = await self._parse_message_submission(message)
        if parse_result.status is MessageParseStatus.IGNORED:
            return None

        if parse_result.status is MessageParseStatus.INVALID:
            await add_reaction_if_possible(
                message,
                config.CONFUSED_EMOJI,
                log=self.logger,
            )
            return None

        context = await self._get_configured_feature_context(manager_context)
        if context is None:
            self.logger.debug(
                "Feature `%s` in Guild: `%s` Channel: `%s` has no feature config; "
                "ignoring parsed message.",
                self.feature_name,
                manager_context.guild_id,
                manager_context.channel_id,
            )
            return None

        if parse_result.submission is None or parse_result.user_info is None:
            msg = "Parsed message result is missing submission or user info."
            raise ValueError(msg)

        return await self._process_configured_message_submission(
            message,
            context,
            parse_result.submission,
            parse_result.user_info,
        )

    async def _get_message_feature_manager_context_or_none(
        self,
        message: Message,
    ) -> FeatureManagerContext[TManager] | None:
        if message.author.bot or message.guild is None or message.channel is None:
            return None
        return await self._get_feature_manager_context_or_none(
            guild=message.guild,
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
        context: ConfiguredFeatureContext[TManager],
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

    @app_commands.default_permissions(administrator=True, manage_channels=True)
    async def upsert_from_content_menu(
        self, interaction: Interaction, message: Message
    ) -> None:
        """
        Upsert registration data for this feature from a message (context menu).
        """
        self._get_interaction_channel_context(interaction)

        await interaction.response.defer(ephemeral=False)

        try:
            result = await self.process_upsert_from_message(message)
        except GoogleSheetsError as exc:
            logger = getattr(self, "logger", None)
            bot_user = getattr(getattr(self, "bot", None), "user", None)
            await mark_google_sheets_message_failure(
                message,
                bot_user,
                exc,
                logger,
            )
            await send_google_sheets_error(interaction, exc, ephemeral=False)
            return

        content = (
            f"Failed to upsert for `{self.feature_name}`."
            if result is None
            else f"Upsert for `{self.feature_name}` complete. Data: ```js\n{result}```"
        )
        await interaction.followup.send(content, ephemeral=False)

    @commands.Cog.listener()
    async def on_message(self, message: Message) -> None:
        """
        Listen for messages to provide a button for team register setup/edit.
        This is used in channels where the feature is enabled.
        """
        try:
            await self.process_upsert_from_message(message)
        except GoogleSheetsError as exc:
            await mark_google_sheets_message_failure(
                message,
                self.bot.user,
                exc,
                self.logger,
            )

    async def setup_after_enable(self, interaction: Interaction) -> None:
        """Show current settings or prompt to set up if not configured."""
        interaction_context = self._get_interaction_channel_context(interaction)
        manager_context = await self._get_feature_manager_context(interaction_context)
        context = await self._get_configured_feature_context(manager_context)
        if context is None:
            view = self._build_initial_setup_view(manager_context.manager)
            await send_settings_view_followup(
                interaction,
                content=initial_setup_content(self.feature_display_name),
                view=view,
            )
            return

        try:
            panel = await self._build_settings_panel(
                interaction,
                context.manager,
                context.feature_config,
            )
        except GoogleSheetsError as exc:
            await send_google_sheets_error(interaction, exc)
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
        if interaction.guild is None or interaction.channel is None:
            msg = (
                "Interaction guild or channel is None. "
                "Cannot proceed with enable command."
            )
            raise ValueError(msg)
        guild_id = interaction.guild.id
        channel_id = interaction.channel.id
        await self._enable_channel(guild_id, channel_id)
        await interaction.response.send_message(
            f"Feature `{self.feature_name}` enabled in this channel.", ephemeral=True
        )
        await self.setup_after_enable(interaction)

    @app_commands.command(
        name="disable",
        description="Disable this feature in the current channel (soft disable).",
    )
    async def disable(self, interaction: Interaction) -> None:
        """Slash command to disable this feature in the current channel."""
        if interaction.guild is None or interaction.channel is None:
            msg = (
                "Interaction guild or channel is None. "
                "Cannot proceed with disable command."
            )
            raise ValueError(msg)
        guild_id = interaction.guild.id
        channel_id = interaction.channel.id
        result = await self._disable_channel(guild_id, channel_id)
        msg = (
            f"Feature `{self.feature_name}` disabled in this channel."
            if result
            else f"Feature `{self.feature_name}` is not enabled in this channel."
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
        if interaction.guild is None or interaction.channel is None:
            msg = (
                "Interaction guild or channel is None. "
                "Cannot proceed with disable and clear command."
            )
            raise ValueError(msg)
        view = DisableAndClearConfirmView()
        await interaction.response.send_message(
            f"Are you sure you want to disable and clear all settings for feature "
            f"`{self.feature_name}` in this channel?",
            view=view,
            ephemeral=True,
        )
        # Wait for user interaction
        await view.wait()
        if view.value:
            guild_id = interaction.guild.id
            channel_id = interaction.channel.id
            await self._clear_feature_settings(guild_id, channel_id)
            await interaction.followup.send(
                f"Feature `{self.feature_name}` has been disabled and all bot settings "
                f"for this feature in this channel have been permanently cleared.",
                ephemeral=True,
            )
        elif view.value is False:
            # Already sent cancel message in view
            pass
        else:
            await interaction.followup.send(
                "No response received. Operation timed out.", ephemeral=True
            )

    help_template_key: str

    async def _help_callback(self, interaction: Interaction) -> None:
        """
        Show help for this feature.
        This method should be implemented by subclasses to provide
        feature-specific help text.
        """
        await interaction.response.defer(ephemeral=False)

        interaction_context = self._get_interaction_channel_context(interaction)
        manager_context = await self._get_feature_manager_context(interaction_context)
        context = await self._get_configured_feature_context(manager_context)
        if context is None:
            await self._send_missing_config_followup(interaction)
            return

        bot_mention = self.bot.user.mention if self.bot.user is not None else "@Bot"
        announcements = await render_announcement_messages(
            self.help_template_key,
            context.guild_id,
            self.logger,
            bot=bot_mention,
            sheet_url=context.feature_config.sheet_url,
        )
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
    def feature_enabled_prefix_command_predicate(feature_name: str) -> Callable:
        """
        Predicate for prefix command: require feature to be enabled in this channel.

        Args:
            feature_name (str): Feature name to check.

        Returns:
            callable: Predicate function for command check.
        """

        async def predicate(ctx: commands.Context) -> bool:
            if ctx.guild is None or ctx.channel is None:
                msg = (
                    f"Context guild or channel is None. "
                    f"Cannot check feature status for feature: {feature_name}."
                )
                raise ValueError(msg)
            if not await FeatureChannelBase.is_enabled(
                ctx.guild.id, ctx.channel.id, feature_name
            ):
                raise FeatureNotEnabled(feature_name)
            return True

        return predicate

    @staticmethod
    def feature_enabled_app_command_predicate(feature_name: str) -> Callable:
        """
        Predicate for app command: require feature to be enabled in this channel.

        Args:
            feature_name (str): Feature name to check.

        Returns:
            callable: Predicate function for app command check.
        """

        async def predicate(interaction: Interaction) -> bool:
            if interaction.guild is None or interaction.channel is None:
                msg = (
                    f"Interaction guild or channel is None. "
                    f"Cannot check feature status for feature: {feature_name}."
                )
                raise ValueError(msg)
            if not await FeatureChannelBase.is_enabled(
                interaction.guild.id, interaction.channel.id, feature_name
            ):
                raise FeatureNotEnabled(feature_name)
            return True

        return predicate


@app_commands.guild_only()
class FeatureChannelUserBase(
    FeatureContextMixin[TManager],
    FeatureChannelErrorHandler,
    commands.GroupCog,
    Generic[TFeatureChannel, TManager, TGoogleSheetsMetadata],
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

    async def delete_callback(self, interaction: Interaction) -> None:
        """
        Delete the user's data for this feature in this channel.
        """
        interaction_context = self._get_interaction_channel_context(interaction)

        await interaction.response.defer(ephemeral=True)

        user_info = UserInfo(
            username=interaction.user.name,
            display_name=interaction.user.display_name,
        )

        manager_context = await self._get_feature_manager_context(interaction_context)
        context = await self._get_configured_feature_context(manager_context)
        if context is None:
            await self._send_missing_config_followup(interaction)
            return

        manager = context.manager

        async with self.FeatureChannelType.lock(context.channel_id):
            try:
                metadata = await manager.fetch_google_sheets_metadata()
                await self._delete_user_data(manager, user_info, metadata)
            except GoogleSheetsError as exc:
                await send_google_sheets_error(interaction, exc)
                return

            locale = interaction.locale.value

            if locale.startswith("zh"):
                content = f"✅ 已成功刪除 `{self.feature_name}` 登記的資料。"
            elif locale.startswith("ja"):
                content = f"✅ `{self.feature_name}` の入力データを正常に削除しました。"
            else:
                content = (
                    f"✅ Your data for `{self.feature_name}` has been "
                    f"deleted successfully."
                )

        await interaction.followup.send(content=content, ephemeral=True)

    async def send_help_message(
        self,
        interaction: Interaction,
        template_key: str,
    ) -> None:
        """
        Show help for team registration.
        """

        await interaction.response.defer(ephemeral=True)

        interaction_context = self._get_interaction_channel_context(interaction)
        manager_context = await self._get_feature_manager_context(interaction_context)
        context = await self._get_configured_feature_context(manager_context)
        if context is None:
            await self._send_missing_config_followup(interaction)
            return

        locale = locale_to_template_code(interaction.locale.value)
        bot_mention = self.bot.user.mention if self.bot.user is not None else "@Bot"
        await interaction.followup.send(
            render_message_template(
                template_key,
                locale,
                bot=bot_mention,
                sheet_url=context.feature_config.sheet_url,
            ),
            ephemeral=True,
        )
