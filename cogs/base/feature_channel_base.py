from __future__ import annotations

import logging
from abc import ABCMeta, abstractmethod
from dataclasses import dataclass
from enum import Enum, auto
from typing import TYPE_CHECKING, Protocol

from discord import (
    Embed,
    Forbidden,
    HTTPException,
    Interaction,
    Message,
    MessageReference,
    NotFound,
    app_commands,
)
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
from components.ui_auto_guide import (
    LATEST_GUIDE_ENABLE_REFRESH_FAILED_WARNING,
    AutoGuideButtonsView,
    AutoGuideDeleteCallback,
    auto_guide_button_language,
    discord_message_url,
)
from components.ui_feature_channel import (
    ConfirmDeleteUserDataView,
    DisableAndClearConfirmView,
)
from components.ui_settings_flow import (
    initial_setup_content,
    prepare_replacement_settings_view,
    send_current_panel_followup,
    send_settings_refresh_failure,
    send_settings_view_followup,
)
from components.ui_storage_errors import (
    mark_storage_message_failure,
    send_storage_error,
)
from components.ui_worksheet_contract_errors import (
    WORKSHEET_CONTRACT_FAILURE_REACTIONS,
    send_worksheet_contract_error,
)
from models.feature_channel import FeatureChannel
from models.feature_channel_message_state import (
    FeatureChannelMessageKind,
    FeatureChannelMessageState,
    get_auto_guide_state,
    get_or_create_auto_guide_state,
    save_manual_guide_anchor,
)
from utils.announcement_languages import (
    ANNOUNCEMENT_RENDER_FAILURE_MESSAGE,
    get_announcement_languages,
    render_announcement_messages,
)
from utils.google_sheets_urls import google_sheet_url_with_gid
from utils.manager_base import ManagerBase
from utils.message_templates import locale_to_template_code, render_message_template
from utils.reactions import add_reaction_if_possible, transition_processing_reaction
from utils.register_i18n import register_user_text
from utils.storage_errors import (
    StorageError,
    StorageOperationContext,
    classify_storage_exception,
    generate_error_reference,
    storage_error_content,
)
from utils.structs_base import (
    GoogleSheetsMetadata,
    SubmissionParseResult,
    UserInfo,
    WorksheetContractError,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

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


LATEST_GUIDE_DELETE_FAILED_WARNING = (
    "Latest Guide Message was disabled, but the previous guide message could "
    "not be deleted. Check bot permissions and delete it manually if needed."
)
HARD_CLEAR_LATEST_GUIDE_DELETE_FAILED_WARNING = (
    "Feature settings were cleared, but the previous latest guide message could "
    "not be deleted. Check bot permissions and delete it manually if needed."
)
INTERNAL_FAILURE_REACTIONS = (config.WARNING_EMOJI, "🚧")


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


class _MessageSubmissionParser[TSubmission](Protocol):
    @classmethod
    def parse_submission(
        cls,
        user_info: UserInfo,
        lines: list[str],
    ) -> SubmissionParseResult[TSubmission]: ...


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
        feature_name (str): Stable feature identifier for i18n lookup.
        feature_display_name (str): Human-facing fallback feature name.
        locale (str): Discord locale value. Defaults to English fallback.
    """

    def __init__(
        self,
        feature_name: str,
        feature_display_name: str,
        *,
        locale: str = "en",
    ) -> None:
        msg = register_user_text(
            feature_name,
            locale,
            "not_enabled",
            fallback_display_name=feature_display_name,
        )
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
        if isinstance(exc, WorksheetContractError):
            await send_worksheet_contract_error(
                interaction,
                exc,
                operation=operation,
                feature_name=self.feature_name,
                log=self.logger,
            )
            return

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
    sheet_write_lock: KeyAsyncLock
    auto_guide_lock: KeyAsyncLock

    ManagerType: type[TManager]  # Type of the manager to use for this feature
    ParserType: type[_MessageSubmissionParser[TSubmission]]

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

    async def _parse_message_submission(
        self,
        message: Message,
    ) -> MessageParseResult[TSubmission]:
        user_info = self._message_user_info(message)
        parse_result = self.ParserType.parse_submission(
            user_info,
            message.content.splitlines(),
        )
        if parse_result.invalid_attempts:
            return MessageParseResult.invalid(user_info=user_info)

        if parse_result.submission is None:
            return MessageParseResult.ignored()

        return MessageParseResult.parsed(
            parse_result.submission,
            user_info=user_info,
        )

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
            (
                "Received feature message. operation=message_receive feature=%s "
                "guild=%s channel=%s message=%s lines=%s characters=%s"
            ),
            self.feature_name,
            message.guild.id,
            message.channel.id,
            message.id,
            len(message.content.splitlines()),
            len(message.content),
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
        except WorksheetContractError as error:
            await transition_processing_reaction(
                message,
                WORKSHEET_CONTRACT_FAILURE_REACTIONS,
                processing_emoji=config.PROCESSING_EMOJI,
                user=getattr(getattr(self, "bot", None), "user", None),
                log=getattr(self, "logger", None),
            )
            await send_worksheet_contract_error(
                interaction,
                error,
                operation="context_menu_upsert",
                feature_name=self.feature_name,
                log=getattr(self, "logger", None),
            )
            return
        except Exception as exc:
            error = classify_storage_exception(exc)
            if error is None:
                await transition_processing_reaction(
                    message,
                    INTERNAL_FAILURE_REACTIONS,
                    processing_emoji=config.PROCESSING_EMOJI,
                    user=getattr(getattr(self, "bot", None), "user", None),
                    log=getattr(self, "logger", None),
                )
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

        if outcome.status is _MessageUpsertStatus.INVALID:
            await interaction.followup.send(
                f"⚠️ The message contains an invalid {self.feature_display_name} "
                "format.",
                ephemeral=True,
            )
            return

        if outcome.status is _MessageUpsertStatus.IGNORED:
            await interaction.followup.send(
                f"⚠️ No {self.feature_display_name} data was recognized in this "
                "message.",
                ephemeral=True,
            )
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
            try:
                await self._process_feature_channel_message_with_outcome(
                    message,
                    feature_channel_context,
                )
            except WorksheetContractError as error:
                self.logger.warning(
                    (
                        "Worksheet contract message action failed. operation=%s "
                        "feature=%s guild=%s channel=%s message=%s hint=%s"
                    ),
                    "message_upsert",
                    self.feature_name,
                    message.guild.id,
                    message.channel.id,
                    message.id,
                    error.log_hint,
                )
                await transition_processing_reaction(
                    message,
                    WORKSHEET_CONTRACT_FAILURE_REACTIONS,
                    processing_emoji=config.PROCESSING_EMOJI,
                    user=getattr(getattr(self, "bot", None), "user", None),
                    log=self.logger,
                )
            except Exception as exc:
                error = classify_storage_exception(exc)
                if error is None:
                    await transition_processing_reaction(
                        message,
                        INTERNAL_FAILURE_REACTIONS,
                        processing_emoji=config.PROCESSING_EMOJI,
                        user=getattr(getattr(self, "bot", None), "user", None),
                        log=self.logger,
                    )
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
        finally:
            await self._refresh_auto_guide_if_enabled(
                feature_channel_context,
                message.channel,
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

    async def _auto_guide_is_enabled(self, feature_channel: FeatureChannel) -> bool:
        auto_guide_state = await get_auto_guide_state(feature_channel)
        return bool(auto_guide_state and auto_guide_state.is_enabled)

    def _latest_guide_refresh_callback(
        self,
        manager: TManager,
    ) -> Callable[[Interaction, SheetConfigBase], Awaitable[bool]]:
        async def latest_guide_refresh_callback(
            interaction: Interaction,
            feature_config: SheetConfigBase,
        ) -> bool:
            try:
                source = require_guild_channel_source(
                    interaction,
                    action="refresh latest guide message",
                )
                feature_channel_context = FeatureChannelContext(
                    guild_id=source.guild.id,
                    channel_id=source.channel.id,
                    feature_channel=manager.feature_channel,
                    manager=manager,
                )
                return await self._refresh_auto_guide_if_enabled(
                    feature_channel_context,
                    source.channel,
                    feature_config=feature_config,
                )
            except Exception:
                self.logger.exception(
                    "Failed to refresh auto guide after settings save for Feature: "
                    "`%s`",
                    self.feature_name,
                )
                return False

        return latest_guide_refresh_callback

    async def toggle_auto_guide_from_settings(
        self,
        interaction: Interaction,
        *,
        enabled: bool,
        current_view: View | None,
        feature_config: SheetConfigBase,
    ) -> None:
        source = require_guild_channel_source(
            interaction,
            action="toggle latest guide message",
        )
        try:
            feature_channel_context = await self._get_feature_channel_context(source)
            auto_guide_state = await get_or_create_auto_guide_state(
                feature_channel_context.feature_channel
            )
            auto_guide_state.is_enabled = enabled
            await auto_guide_state.save()
        except Exception as exc:  # noqa: BLE001
            await self._send_interaction_storage_error_or_raise(
                interaction,
                exc,
                source=source,
                operation="latest_guide_toggle_save",
            )
            return

        try:
            panel = await self._build_settings_panel(
                interaction,
                feature_channel_context.manager,
                feature_config,
            )
        except Exception as exc:  # noqa: BLE001
            if current_view is not None:
                current_view.stop()
            deleted = True
            if not enabled:
                deleted = await self._disable_auto_guide_and_delete_message(
                    feature_channel_context
                )
            await send_settings_refresh_failure(
                interaction,
                exc,
                operation="latest_guide_toggle_refresh_panel",
                feature_name=self.feature_name,
                log=self.logger,
                clear_current_message=True,
            )
            if not deleted:
                await interaction.followup.send(
                    LATEST_GUIDE_DELETE_FAILED_WARNING,
                    ephemeral=True,
                )
            return

        replacement_view = (
            panel.view
            if current_view is None
            else prepare_replacement_settings_view(current_view, panel.view)
        )
        await interaction.edit_original_response(
            content=None,
            embed=panel.embed,
            view=replacement_view,
        )

        if enabled:
            refreshed = await self._refresh_auto_guide_if_enabled(
                feature_channel_context,
                source.channel,
                feature_config=feature_config,
            )
            if not refreshed:
                await interaction.followup.send(
                    LATEST_GUIDE_ENABLE_REFRESH_FAILED_WARNING,
                    ephemeral=True,
                )
            return

        deleted = await self._disable_auto_guide_and_delete_message(
            feature_channel_context
        )
        if not deleted:
            await interaction.followup.send(
                LATEST_GUIDE_DELETE_FAILED_WARNING,
                ephemeral=True,
            )

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
        auto_guide_deleted = True
        try:
            feature_channel_context = await self._get_feature_channel_context_or_none(
                guild_id=source.guild.id,
                channel_id=source.channel.id,
                require_enabled=True,
            )
            if feature_channel_context is None:
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
                    auto_guide_deleted = (
                        await self._disable_auto_guide_and_delete_message(
                            feature_channel_context,
                        )
                    )
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
        if result and not auto_guide_deleted:
            await interaction.followup.send(
                LATEST_GUIDE_DELETE_FAILED_WARNING,
                ephemeral=True,
            )

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
            auto_guide_deleted = True
            try:
                feature_channel_context = (
                    await self._get_feature_channel_context_or_none(
                        guild_id=source.guild.id,
                        channel_id=source.channel.id,
                    )
                )
                if feature_channel_context is not None:
                    auto_guide_deleted = (
                        await self._delete_auto_guide_message_for_hard_clear(
                            feature_channel_context,
                        )
                    )
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
            if not auto_guide_deleted:
                await interaction.followup.send(
                    HARD_CLEAR_LATEST_GUIDE_DELETE_FAILED_WARNING,
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
    auto_guide_template_key: str

    def _guide_sheet_url(
        self,
        feature_config: SheetConfigBase,
    ) -> str:
        return google_sheet_url_with_gid(
            feature_config.sheet_url,
            feature_config.landing_worksheet_id,
        )

    async def _guide_template_values(
        self,
        context: ConfiguredFeatureChannelContext[TManager],
    ) -> dict[str, object]:
        bot_mention = self.bot.user.mention if self.bot.user is not None else "@Bot"
        return {
            "bot": bot_mention,
            "sheet_url": self._guide_sheet_url(context.feature_config),
        }

    def _auto_guide_template_values(
        self,
        context: ConfiguredFeatureChannelContext[TManager],
        language: str,
    ) -> dict[str, object]:
        del language
        bot_mention = self.bot.user.mention if self.bot.user is not None else "@Bot"
        return {
            "bot": bot_mention,
            "sheet_url": self._guide_sheet_url(context.feature_config),
        }

    async def _render_auto_guide_embeds(
        self,
        context: ConfiguredFeatureChannelContext[TManager],
        *,
        include_footer: bool = False,
    ) -> list[Embed]:
        embeds: list[Embed] = []
        languages = await get_announcement_languages(context.guild_id, self.logger)
        for language in languages:
            values = self._auto_guide_template_values(context, language)
            embed = Embed(
                title=render_message_template(
                    f"{self.auto_guide_template_key}.title",
                    language,
                    **values,
                ),
                description=render_message_template(
                    f"{self.auto_guide_template_key}.description",
                    language,
                    **values,
                ),
                color=config.DEFAULT_EMBED_COLOR,
            )
            if include_footer:
                embed.set_footer(
                    text=render_message_template(
                        f"{self.auto_guide_template_key}.footer",
                        language,
                        **values,
                    )
                )
            embeds.append(embed)
        return embeds

    def _auto_guide_delete_callback(self) -> AutoGuideDeleteCallback:
        async def unavailable_callback(interaction: Interaction) -> None:
            await interaction.response.send_message(
                "⚠️ Delete is temporarily unavailable. Try the slash command instead.",
                ephemeral=True,
            )

        for cog in getattr(self.bot, "cogs", {}).values():
            if getattr(cog, "feature_name", None) != self.feature_name:
                continue
            callback = getattr(cog, "delete_callback", None)
            if callback is not None:
                return callback
        return unavailable_callback

    async def _build_auto_guide_buttons_view(
        self,
        context: ConfiguredFeatureChannelContext[TManager],
        *,
        full_guide_url: str | None = None,
    ) -> AutoGuideButtonsView:
        languages = await get_announcement_languages(context.guild_id, self.logger)
        return AutoGuideButtonsView(
            feature_name=self.feature_name,
            language=auto_guide_button_language(languages),
            delete_callback=self._auto_guide_delete_callback(),
            sheet_url=self._guide_sheet_url(context.feature_config),
            full_guide_url=full_guide_url,
        )

    async def _refresh_auto_guide_if_enabled(
        self,
        feature_channel_context: FeatureChannelContext[TManager],
        channel: object,
        *,
        feature_config: SheetConfigBase | None = None,
    ) -> bool:
        async with self.auto_guide_lock(feature_channel_context.channel_id):
            try:
                auto_guide_state = await get_auto_guide_state(
                    feature_channel_context.feature_channel
                )
                if auto_guide_state is None or not auto_guide_state.is_enabled:
                    return True

                if feature_config is None:
                    context = await self._get_configured_feature_channel_context(
                        feature_channel_context
                    )
                    if context is None:
                        return True
                else:
                    context = ConfiguredFeatureChannelContext(
                        guild_id=feature_channel_context.guild_id,
                        channel_id=feature_channel_context.channel_id,
                        feature_channel=feature_channel_context.feature_channel,
                        manager=feature_channel_context.manager,
                        feature_config=feature_config,
                    )

                return await self._send_and_record_auto_guide(
                    context,
                    channel,
                    auto_guide_state,
                )
            except Exception:
                self.logger.exception(
                    "Failed to refresh auto guide for Feature: `%s` in "
                    "Guild: `%s` Channel: `%s`",
                    self.feature_name,
                    feature_channel_context.guild_id,
                    feature_channel_context.channel_id,
                )
                return False

    async def _send_and_record_auto_guide(
        self,
        context: ConfiguredFeatureChannelContext[TManager],
        channel: object,
        auto_guide_state: FeatureChannelMessageState,
    ) -> bool:
        message = await self._send_auto_guide_message(
            context,
            channel,
        )
        if auto_guide_state.message_id is not None:
            await self._delete_auto_guide_message(channel, auto_guide_state.message_id)

        state = await get_or_create_auto_guide_state(context.feature_channel)
        state.message_id = message.id
        await state.save()
        return True

    async def _send_auto_guide_message(
        self,
        context: ConfiguredFeatureChannelContext[TManager],
        channel: object,
    ) -> Message:
        manual_anchor = await FeatureChannelMessageState.get_or_none(
            feature_channel=context.feature_channel,
            message_kind=FeatureChannelMessageKind.MANUAL_GUIDE,
            message_id__not_isnull=True,
        )
        if manual_anchor is not None:
            full_guide_url = discord_message_url(
                guild_id=context.guild_id,
                channel_id=context.channel_id,
                message_id=manual_anchor.message_id,
            )
            reference = MessageReference(
                message_id=manual_anchor.message_id,
                channel_id=context.channel_id,
                guild_id=context.guild_id,
            )
            try:
                return await channel.send(
                    embeds=await self._render_auto_guide_embeds(
                        context,
                        include_footer=True,
                    ),
                    reference=reference,
                    mention_author=False,
                    view=await self._build_auto_guide_buttons_view(
                        context,
                        full_guide_url=full_guide_url,
                    ),
                )
            except (NotFound, Forbidden, HTTPException):
                pass

        return await channel.send(
            embeds=await self._render_auto_guide_embeds(
                context,
            ),
            view=await self._build_auto_guide_buttons_view(context),
        )

    async def _delete_auto_guide_message(
        self,
        channel: object,
        message_id: int,
    ) -> bool:
        try:
            message = await channel.fetch_message(message_id)
            await message.delete()
        except NotFound:
            return True
        except (Forbidden, HTTPException):
            self.logger.warning(
                "Failed to delete previous auto guide message `%s`.",
                message_id,
                exc_info=True,
            )
            return False
        return True

    async def _disable_auto_guide_and_delete_message(
        self,
        feature_channel_context: FeatureChannelContext[TManager],
    ) -> bool:
        async with self.auto_guide_lock(feature_channel_context.channel_id):
            auto_guide_state = await get_auto_guide_state(
                feature_channel_context.feature_channel
            )
            if auto_guide_state is None:
                return True

            auto_guide_state.is_enabled = False
            await auto_guide_state.save()
            if auto_guide_state.message_id is None:
                return True

            get_channel = getattr(self.bot, "get_channel", None)
            channel = (
                get_channel(feature_channel_context.channel_id)
                if callable(get_channel)
                else None
            )
            if channel is None:
                self.logger.warning(
                    "Failed to delete latest auto guide message `%s`; channel `%s` "
                    "was not available.",
                    auto_guide_state.message_id,
                    feature_channel_context.channel_id,
                )
                return False

            deleted = await self._delete_auto_guide_message(
                channel,
                auto_guide_state.message_id,
            )
            if not deleted:
                return False

            auto_guide_state.message_id = None
            await auto_guide_state.save()
            return True

    async def _delete_auto_guide_message_for_hard_clear(
        self,
        feature_channel_context: FeatureChannelContext[TManager],
    ) -> bool:
        async with self.auto_guide_lock(feature_channel_context.channel_id):
            auto_guide_state = await get_auto_guide_state(
                feature_channel_context.feature_channel
            )
            if auto_guide_state is None or auto_guide_state.message_id is None:
                return True

            get_channel = getattr(self.bot, "get_channel", None)
            channel = (
                get_channel(feature_channel_context.channel_id)
                if callable(get_channel)
                else None
            )
            if channel is None:
                self.logger.warning(
                    "Failed to delete latest auto guide message `%s`; channel `%s` "
                    "was not available.",
                    auto_guide_state.message_id,
                    feature_channel_context.channel_id,
                )
                return False

            return await self._delete_auto_guide_message(
                channel,
                auto_guide_state.message_id,
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

            announcements = await render_announcement_messages(
                self.guide_template_key,
                context.guild_id,
                self.logger,
                **await self._guide_template_values(context),
            )
        except Exception as exc:  # noqa: BLE001
            await self._send_interaction_storage_error_or_raise(
                interaction,
                exc,
                source=source,
                operation="send_guide_announcement",
            )
            return

        if not announcements:
            await interaction.followup.send(
                ANNOUNCEMENT_RENDER_FAILURE_MESSAGE,
                ephemeral=True,
            )
            return

        anchor_saved = False
        for announcement in announcements:
            message = await interaction.followup.send(
                announcement.content,
                ephemeral=False,
                wait=True,
            )
            if anchor_saved:
                continue
            anchor_saved = True
            try:
                await save_manual_guide_anchor(context.feature_channel, message.id)
            except Exception:  # noqa: BLE001
                self.logger.warning(
                    (
                        "Failed to save manual guide anchor for Feature: `%s` in "
                        "Guild: `%s` Channel: `%s` MessageKind: `%s` "
                        "Message: `%s`"
                    ),
                    self.feature_name,
                    context.guild_id,
                    context.channel_id,
                    FeatureChannelMessageKind.MANUAL_GUIDE.value,
                    message.id,
                    exc_info=True,
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
                raise FeatureNotEnabled(feature_name, feature_display_name)
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
                raise FeatureNotEnabled(
                    feature_name,
                    feature_display_name,
                    locale=interaction.locale.value,
                )
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

    def build_auto_guide_delete_view(self) -> AutoGuideButtonsView:
        return AutoGuideButtonsView(
            feature_name=self.feature_name,
            language="en",
            delete_callback=self.delete_callback,
            sheet_url=None,
            delete_only=True,
            timeout=None,
        )

    def register_persistent_views(self) -> None:
        self.bot.add_view(self.build_auto_guide_delete_view())

    @abstractmethod
    async def _delete_user_data(
        self, manager: TManager, user_info: UserInfo, metadata: TGoogleSheetsMetadata
    ) -> None: ...

    def _guide_sheet_url(
        self,
        feature_config: SheetConfigBase,
    ) -> str:
        return google_sheet_url_with_gid(
            feature_config.sheet_url,
            feature_config.landing_worksheet_id,
        )

    async def _guide_template_values(
        self,
        context: ConfiguredFeatureChannelContext[TManager],
    ) -> dict[str, object]:
        bot_mention = self.bot.user.mention if self.bot.user is not None else "@Bot"
        return {
            "bot": bot_mention,
            "sheet_url": self._guide_sheet_url(context.feature_config),
        }

    async def delete_callback(self, interaction: Interaction) -> None:
        """
        Ask for confirmation before deleting the user's data for this feature.
        """
        source = require_guild_channel_source(
            interaction,
            action="delete feature user data",
        )
        locale = interaction.locale.value
        prompt = register_user_text(
            self.feature_name,
            locale,
            "delete_confirm_prompt",
            fallback_display_name=self.feature_display_name,
        )
        view = ConfirmDeleteUserDataView(
            requesting_user_id=interaction.user.id,
            confirm_label=register_user_text(
                self.feature_name,
                locale,
                "delete_confirm_button",
                fallback_display_name=self.feature_display_name,
            ),
            cancel_label=register_user_text(
                self.feature_name,
                locale,
                "delete_cancel_button",
                fallback_display_name=self.feature_display_name,
            ),
            in_progress_message=register_user_text(
                self.feature_name,
                locale,
                "delete_in_progress",
                fallback_display_name=self.feature_display_name,
                processing_emoji=config.PROCESSING_EMOJI,
            ),
            cancelled_message=register_user_text(
                self.feature_name,
                locale,
                "delete_cancelled",
                fallback_display_name=self.feature_display_name,
            ),
            unauthorized_message=register_user_text(
                self.feature_name,
                locale,
                "delete_unauthorized",
                fallback_display_name=self.feature_display_name,
            ),
        )
        await interaction.response.send_message(
            prompt,
            view=view,
            ephemeral=True,
        )
        await view.wait()
        if view.value is True:
            success_content = await self._delete_user_data_after_confirmation(
                interaction,
                source,
            )
            if success_content is not None:
                await interaction.edit_original_response(
                    content=success_content,
                    view=None,
                )
        elif view.value is None:
            await interaction.followup.send(
                register_user_text(
                    self.feature_name,
                    locale,
                    "delete_timeout",
                    fallback_display_name=self.feature_display_name,
                ),
                ephemeral=True,
            )

    async def _delete_user_data_after_confirmation(
        self,
        interaction: Interaction,
        source: GuildChannelSource,
    ) -> str | None:
        """
        Delete the user's data for this feature after UI confirmation.
        """
        try:
            user_info = UserInfo(
                username=interaction.user.name,
                display_name=interaction.user.display_name,
            )

            feature_channel_context = await self._get_feature_channel_context_or_none(
                guild_id=source.guild.id,
                channel_id=source.channel.id,
                require_enabled=True,
            )
            if feature_channel_context is None:
                await interaction.followup.send(
                    register_user_text(
                        self.feature_name,
                        interaction.locale.value,
                        "not_enabled",
                        fallback_display_name=self.feature_display_name,
                    ),
                    ephemeral=True,
                )
                return None

            context = await self._get_configured_feature_channel_context(
                feature_channel_context
            )
            if context is None:
                await self._send_missing_config_followup(interaction)
                return None

            manager = context.manager

            async with self.FeatureChannelType.sheet_write_lock(context.channel_id):
                metadata = await manager.fetch_google_sheets_metadata()
                await self._delete_user_data(manager, user_info, metadata)

            content = register_user_text(
                self.feature_name,
                interaction.locale.value,
                "delete_success",
                fallback_display_name=self.feature_display_name,
            )
        except Exception as exc:  # noqa: BLE001
            await self._send_interaction_storage_error_or_raise(
                interaction,
                exc,
                source=source,
                operation="delete_user_data",
            )
            return None

        return content

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
            content = render_message_template(
                template_key,
                locale,
                **await self._guide_template_values(context),
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
