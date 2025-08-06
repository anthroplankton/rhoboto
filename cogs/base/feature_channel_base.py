from __future__ import annotations

import logging
from abc import ABCMeta, abstractmethod
from typing import TYPE_CHECKING, Callable, Generic, TypeVar

from discord import Interaction, Message, app_commands
from discord.app_commands import locale_str
from discord.ext import commands

from bot import config
from components.ui_feature_channel import DisableAndClearConfirmView
from models.feature_channel import FeatureChannel
from utils.manager_base import ManagerBase
from utils.structs_base import GoogleSheetsMetadata, UserInfo

if TYPE_CHECKING:
    from bot import Rhoboto
    from utils.key_async_lock import KeyAsyncLock


TFeatureChannel = TypeVar("TFeatureChannel", bound="FeatureChannelBase")
TManager = TypeVar("TManager", bound=ManagerBase)
TGoogleSheetsMetadata = TypeVar("TGoogleSheetsMetadata", bound=GoogleSheetsMetadata)
TUpsertResult = TypeVar("TUpsertResult")


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
    FeatureChannelErrorHandler,
    commands.GroupCog,
    Generic[TManager, TUpsertResult],
    metaclass=CogABCMeta,
):
    """
    Base class for per-channel feature management using Tortoise ORM.

    Provides logging and persistent storage for Discord bot features that can be
    enabled or disabled per channel. Only cogs that require channel feature
    management should inherit from this class.

    Attributes:
        feature_name (str): Name of the feature. Should be overridden by subclasses.
    """

    feature_name: str  # This should be overridden by subclasses
    lock: KeyAsyncLock

    ManagerType: type[TManager]  # Type of the manager to use for this feature

    def __init__(self, bot: Rhoboto) -> None:
        self.bot = bot
        self.logger = logging.getLogger(self.__class__.__name__)
        self.context_menu = app_commands.ContextMenu(
            name=f"{self.feature_name} upsert", callback=self.on_message_context
        )
        bot.tree.add_command(self.context_menu)

    @abstractmethod
    async def process_upsert_from_message(
        self, message: Message
    ) -> TUpsertResult | None:
        """Process the message to upsert registration data for this feature.

        Args:
            message (Message): The Discord message to process.
        """
        msg = "Subclasses must implement this method."
        raise NotImplementedError(msg)

    async def on_message_context(
        self, interaction: Interaction, message: Message
    ) -> None:
        """
        Upsert registration data for this feature from a message (context menu).
        """
        if interaction.channel is None or interaction.guild is None:
            msg = "Interaction channel or guild is None."
            raise ValueError(msg)

        await interaction.response.defer(ephemeral=False)

        result = await self.process_upsert_from_message(message)

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
        await self.process_upsert_from_message(message)

    @abstractmethod
    async def setup_after_enable(self, interaction: Interaction) -> None:
        """
        Show current settings or prompt to set up if not configured.
        This method should be implemented by subclasses to handle
        feature-specific setup.
        """
        msg = "Subclasses must implement setup_after_enable method."
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

    help_text_en: str
    help_text_ja: str
    help_text_zh_tw: str

    async def _help_callback(self, interaction: Interaction) -> None:
        """
        Show help for this feature.
        This method should be implemented by subclasses to provide
        feature-specific help text.
        """
        if interaction.channel is None or interaction.guild is None:
            msg = (
                "Interaction channel or guild is None. "
                "Cannot proceed with help command."
            )
            raise ValueError(msg)

        await interaction.response.defer(ephemeral=True)

        feature_channel = await FeatureChannel.get(
            guild_id=interaction.guild.id,
            channel_id=interaction.channel.id,
            feature_name=self.feature_name,
        )

        manager = self.ManagerType(feature_channel, config.GOOGLE_SERVICE_ACCOUNT_PATH)

        sheet_config = await manager.get_sheet_config_or_none()
        if sheet_config is None:
            await interaction.followup.send(
                content=f"`{self.feature_name}` is not configured for this channel.",
                ephemeral=True,
            )
            return

        help_text = {
            "en": self.help_text_en,
            "zh_tw": self.help_text_zh_tw,
            "ja": self.help_text_ja,
        }
        for text in help_text.values():
            await interaction.followup.send(
                text.format(sheet_config.sheet_url), ephemeral=False
            )

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
        feature = await FeatureChannel.get_or_none(
            guild_id=guild_id,
            channel_id=channel_id,
            feature_name=feature_name,
        )
        return bool(feature and feature.is_enabled)

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
        if interaction.channel is None or interaction.guild is None:
            msg = (
                "Interaction channel or guild is None. "
                "Cannot proceed with delete command."
            )
            raise ValueError(msg)

        await interaction.response.defer(ephemeral=True)

        user_info = UserInfo(
            username=interaction.user.name,
            display_name=interaction.user.display_name,
        )

        feature_channel = await FeatureChannel.get(
            guild_id=interaction.guild.id,
            channel_id=interaction.channel.id,
            feature_name=self.feature_name,
        )

        manager = self.ManagerType(feature_channel, config.GOOGLE_SERVICE_ACCOUNT_PATH)

        sheet_config = await manager.get_sheet_config_or_none()
        if sheet_config is None:
            await interaction.followup.send(
                content=f"`{self.feature_name}` is not configured for this channel.",
                ephemeral=True,
            )
            return

        async with self.FeatureChannelType.lock(interaction.channel.id):
            metadata = await manager.fetch_google_sheets_metadata()
            await self._delete_user_data(manager, user_info, metadata)

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
        help_text_en: str,
        help_text_ja: str,
        help_text_zh_tw: str,
    ) -> None:
        """
        Show help for team registration.
        """

        if interaction.channel is None or interaction.guild is None:
            msg = (
                "Interaction channel or guild is None. "
                "Cannot proceed with help command."
            )
            raise ValueError(msg)

        feature_channel = await FeatureChannel.get(
            guild_id=interaction.guild.id,
            channel_id=interaction.channel.id,
            feature_name=self.feature_name,
        )

        manager = self.ManagerType(feature_channel, config.GOOGLE_SERVICE_ACCOUNT_PATH)

        sheet_config = await manager.get_sheet_config_or_none()
        if sheet_config is None:
            await interaction.followup.send(
                content=f"`{self.feature_name}` is not configured for this channel.",
                ephemeral=True,
            )
            return

        locale = interaction.locale.value
        if locale.startswith("zh"):
            context = help_text_zh_tw
        elif locale.startswith("ja"):
            context = help_text_ja
        else:
            context = help_text_en

        await interaction.response.send_message(
            context.format(sheet_config.sheet_url), ephemeral=True
        )
