from __future__ import annotations

from abc import abstractmethod
from typing import TYPE_CHECKING, ClassVar, Protocol, override, runtime_checkable

from discord import (
    Embed,
    Forbidden,
    HTTPException,
    Interaction,
    Message,
    MessageReference,
    NotFound,
)

from bot import config
from cogs.base.discord_context import require_guild_channel_source
from cogs.base.message_upsert_feature_channel_base import (
    INTERNAL_FAILURE_REACTIONS,
    MessageUpsertFeatureChannelBase,
    MessageUpsertOutcome,
    MessageUpsertStatus,
)
from cogs.base.register_feature_channel_context import (
    ConfiguredRegisterFeatureChannelContext,
    RegisterFeatureChannelContext,
    RegisterFeatureChannelContextMixin,
)
from components.ui_auto_guide import (
    LATEST_GUIDE_ENABLE_REFRESH_FAILED_WARNING,
    AutoGuideButtonsView,
    AutoGuideDeleteCallback,
    auto_guide_button_language,
    discord_message_url,
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
from models.base.sheet_config_base import SheetConfigBase
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
from utils.message_templates import render_message_template
from utils.reactions import transition_processing_reaction
from utils.storage_errors import (
    StorageOperationContext,
    classify_storage_exception,
    generate_error_reference,
)
from utils.structs_base import GoogleSheetsMetadata, WorksheetContractError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from discord.ui import View

    from cogs.base.discord_context import GuildChannelSource
    from components.ui_settings_flow import SettingsPanel
    from models.feature_channel import FeatureChannel
    from utils.key_async_lock import KeyAsyncLock


LATEST_GUIDE_DELETE_FAILED_WARNING = (
    "Latest Guide Message was disabled, but the previous guide message could "
    "not be deleted. Check bot permissions and delete it manually if needed."
)
HARD_CLEAR_LATEST_GUIDE_DELETE_FAILED_WARNING = (
    "Feature settings were cleared, but the previous latest guide message could "
    "not be deleted. Check bot permissions and delete it manually if needed."
)


@runtime_checkable
class RegisterMessageChannel(Protocol):
    """Discord channel capabilities used by Register guide messages."""

    async def send(
        self,
        *,
        embeds: list[Embed],
        view: View,
        reference: MessageReference | None = None,
        mention_author: bool = False,
    ) -> Message: ...

    async def fetch_message(self, message_id: int) -> Message: ...


def _require_register_message_channel(channel: object) -> RegisterMessageChannel:
    if not isinstance(channel, RegisterMessageChannel):
        message = "Register message channel cannot send and fetch messages."
        raise TypeError(message)
    return channel


class RegisterFeatureChannelBase[
    ConfigT: SheetConfigBase,
    MetadataT: GoogleSheetsMetadata,
    ManagerT: ManagerBase[ConfigT, MetadataT],
    SubmissionT,
    UpsertResultT,
](
    RegisterFeatureChannelContextMixin[ConfigT, MetadataT, ManagerT],
    MessageUpsertFeatureChannelBase[
        RegisterFeatureChannelContext[ManagerT],
        ConfiguredRegisterFeatureChannelContext[ConfigT, ManagerT],
        SubmissionT,
        UpsertResultT,
    ],
):
    """Administrator workflow shared by Team and Shift Register."""

    sheet_write_lock: ClassVar[KeyAsyncLock]
    auto_guide_lock: ClassVar[KeyAsyncLock]
    ManagerType: type[ManagerT]

    @override
    def _build_message_context(
        self,
        membership: FeatureChannel,
    ) -> RegisterFeatureChannelContext[ManagerT]:
        return self._build_register_feature_channel_context(membership)

    @override
    async def _get_configured_message_context(
        self,
        context: RegisterFeatureChannelContext[ManagerT],
    ) -> ConfiguredRegisterFeatureChannelContext[ConfigT, ManagerT] | None:
        return await self._get_configured_register_feature_channel_context(context)

    @override
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
        await super()._send_interaction_storage_error_or_raise(
            interaction,
            exc,
            source=source,
            operation=operation,
        )

    @override
    async def _process_context_menu_message(
        self,
        interaction: Interaction,
        message: Message,
        source: GuildChannelSource,
    ) -> None:
        try:
            context = await self._get_message_feature_channel_context_or_none(message)
            outcome = (
                MessageUpsertOutcome.ignored()
                if context is None
                else await self._process_feature_channel_message_with_outcome(
                    message,
                    context,
                )
            )
        except WorksheetContractError as error:
            await transition_processing_reaction(
                message,
                WORKSHEET_CONTRACT_FAILURE_REACTIONS,
                processing_emoji=config.PROCESSING_EMOJI,
                user=self.bot.user,
                log=self.logger,
            )
            await send_worksheet_contract_error(
                interaction,
                error,
                operation="context_menu_upsert",
                feature_name=self.feature_name,
                log=self.logger,
            )
            return
        except Exception as exc:
            error = classify_storage_exception(exc)
            if error is None:
                await transition_processing_reaction(
                    message,
                    INTERNAL_FAILURE_REACTIONS,
                    processing_emoji=config.PROCESSING_EMOJI,
                    user=self.bot.user,
                    log=self.logger,
                )
                raise
            reference = generate_error_reference()
            await mark_storage_message_failure(
                message,
                self.bot.user,
                error,
                context=StorageOperationContext(
                    operation="context_menu_upsert",
                    feature_name=self.feature_name,
                    guild_id=source.guild.id,
                    channel_id=source.channel.id,
                    message_id=message.id,
                ),
                reference_id=reference,
                log=self.logger,
            )
            await send_storage_error(
                interaction,
                error,
                context=self._interaction_storage_context(
                    source,
                    "context_menu_upsert",
                ),
                reference_id=reference,
                log=self.logger,
            )
            return

        if outcome.status is MessageUpsertStatus.MISSING_CONFIG:
            await self._send_missing_register_config_followup(interaction)
            return
        if outcome.status is MessageUpsertStatus.INVALID:
            await interaction.followup.send(
                f"⚠️ The message contains an invalid {self.feature_display_name} "
                "format.",
                ephemeral=True,
            )
            return
        if outcome.status is MessageUpsertStatus.IGNORED:
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

    @override
    async def _process_enabled_message(
        self,
        message: Message,
        context: RegisterFeatureChannelContext[ManagerT],
    ) -> None:
        try:
            try:
                await self._process_feature_channel_message_with_outcome(
                    message,
                    context,
                )
            except WorksheetContractError as error:
                self.logger.warning(
                    (
                        "Worksheet contract message action failed. operation=%s "
                        "feature=%s guild=%s channel=%s message=%s hint=%s"
                    ),
                    "message_upsert",
                    self.feature_name,
                    context.guild_id,
                    context.channel_id,
                    message.id,
                    error.log_hint,
                )
                await transition_processing_reaction(
                    message,
                    WORKSHEET_CONTRACT_FAILURE_REACTIONS,
                    processing_emoji=config.PROCESSING_EMOJI,
                    user=self.bot.user,
                    log=self.logger,
                )
            except Exception as exc:
                error = classify_storage_exception(exc)
                if error is None:
                    await transition_processing_reaction(
                        message,
                        INTERNAL_FAILURE_REACTIONS,
                        processing_emoji=config.PROCESSING_EMOJI,
                        user=self.bot.user,
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
                        guild_id=context.guild_id,
                        channel_id=context.channel_id,
                        message_id=message.id,
                    ),
                    log=self.logger,
                )
        finally:
            await self._refresh_auto_guide_if_enabled(
                context,
                _require_register_message_channel(message.channel),
            )

    @override
    async def _cleanup_after_disable(
        self,
        membership: FeatureChannel,
    ) -> str | None:
        context = self._build_register_feature_channel_context(membership)
        deleted = await self._disable_auto_guide_and_delete_message(context)
        return None if deleted else LATEST_GUIDE_DELETE_FAILED_WARNING

    @override
    async def _cleanup_before_clear(
        self,
        membership: FeatureChannel,
    ) -> str | None:
        context = self._build_register_feature_channel_context(membership)
        deleted = await self._delete_auto_guide_message_for_hard_clear(context)
        return None if deleted else HARD_CLEAR_LATEST_GUIDE_DELETE_FAILED_WARNING

    @override
    async def setup_after_enable(self, interaction: Interaction) -> None:
        """Show current settings or prompt to set up if not configured."""
        source = require_guild_channel_source(
            interaction,
            action="set up feature settings",
        )
        initial_setup_view = None
        panel = None
        try:
            feature_channel_context = await self._get_register_feature_channel_context(
                source
            )
            context = await self._get_configured_register_feature_channel_context(
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
    def _build_initial_setup_view(self, manager: ManagerT) -> View:
        """Build the initial setup view for a feature with no sheet config."""
        msg = "Subclasses must implement _build_initial_setup_view method."
        raise NotImplementedError(msg)

    @abstractmethod
    async def _build_settings_panel(
        self,
        interaction: Interaction,
        manager: ManagerT,
        sheet_config: ConfigT,
    ) -> SettingsPanel:
        """Build the current settings panel for a configured feature."""
        msg = "Subclasses must implement _build_settings_panel method."
        raise NotImplementedError(msg)

    async def _auto_guide_is_enabled(self, feature_channel: FeatureChannel) -> bool:
        auto_guide_state = await get_auto_guide_state(feature_channel)
        return bool(auto_guide_state and auto_guide_state.is_enabled)

    def _latest_guide_refresh_callback(
        self,
        manager: ManagerT,
    ) -> Callable[[Interaction, ConfigT], Awaitable[bool]]:
        async def latest_guide_refresh_callback(
            interaction: Interaction,
            feature_config: ConfigT,
        ) -> bool:
            try:
                source = require_guild_channel_source(
                    interaction,
                    action="refresh latest guide message",
                )
                feature_channel_context = RegisterFeatureChannelContext(
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
        feature_config: ConfigT,
    ) -> None:
        source = require_guild_channel_source(
            interaction,
            action="toggle latest guide message",
        )
        try:
            feature_channel_context = await self._get_register_feature_channel_context(
                source
            )
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

    guide_template_key: str
    auto_guide_template_key: str

    def _guide_sheet_url(
        self,
        feature_config: ConfigT,
    ) -> str:
        return google_sheet_url_with_gid(
            feature_config.sheet_url,
            feature_config.landing_worksheet_id,
        )

    async def _guide_template_values(
        self,
        context: ConfiguredRegisterFeatureChannelContext[ConfigT, ManagerT],
    ) -> dict[str, object]:
        bot_mention = self.bot.user.mention if self.bot.user is not None else "@Bot"
        return {
            "bot": bot_mention,
            "sheet_url": self._guide_sheet_url(context.feature_config),
        }

    def _auto_guide_template_values(
        self,
        context: ConfiguredRegisterFeatureChannelContext[ConfigT, ManagerT],
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
        context: ConfiguredRegisterFeatureChannelContext[ConfigT, ManagerT],
        *,
        include_footer: bool = False,
    ) -> list[Embed]:
        return await self._render_localized_embeds(
            context.guild_id,
            template_key=self.auto_guide_template_key,
            values_for_language=lambda language: self._auto_guide_template_values(
                context, language
            ),
            include_footer=include_footer,
        )

    async def _render_localized_embeds(
        self,
        guild_id: int,
        *,
        template_key: str,
        values_for_language: Callable[[str], dict[str, object]],
        include_footer: bool = False,
    ) -> list[Embed]:
        embeds: list[Embed] = []
        languages = await get_announcement_languages(guild_id, self.logger)
        for language in languages:
            values = values_for_language(language)
            embed = Embed(
                title=render_message_template(
                    f"{template_key}.title", language, **values
                ),
                description=render_message_template(
                    f"{template_key}.description", language, **values
                ),
                color=config.DEFAULT_EMBED_COLOR,
            )
            if include_footer:
                embed.set_footer(
                    text=render_message_template(
                        f"{template_key}.footer", language, **values
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

        for cog in self.bot.cogs.values():
            if getattr(cog, "feature_name", None) != self.feature_name:
                continue
            callback = getattr(cog, "delete_callback", None)
            if callback is not None:
                return callback
        return unavailable_callback

    async def _build_auto_guide_buttons_view(
        self,
        context: ConfiguredRegisterFeatureChannelContext[ConfigT, ManagerT],
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
        feature_channel_context: RegisterFeatureChannelContext[ManagerT],
        channel: object,
        *,
        feature_config: ConfigT | None = None,
    ) -> bool:
        async with self.auto_guide_lock(feature_channel_context.channel_id):
            try:
                auto_guide_state = await get_auto_guide_state(
                    feature_channel_context.feature_channel
                )
                if auto_guide_state is None or not auto_guide_state.is_enabled:
                    return True

                if feature_config is None:
                    context = (
                        await self._get_configured_register_feature_channel_context(
                            feature_channel_context
                        )
                    )
                    if context is None:
                        return True
                else:
                    context = ConfiguredRegisterFeatureChannelContext(
                        guild_id=feature_channel_context.guild_id,
                        channel_id=feature_channel_context.channel_id,
                        feature_channel=feature_channel_context.feature_channel,
                        manager=feature_channel_context.manager,
                        feature_config=feature_config,
                    )

                return await self._send_and_record_auto_guide(
                    context,
                    _require_register_message_channel(channel),
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
        context: ConfiguredRegisterFeatureChannelContext[ConfigT, ManagerT],
        channel: RegisterMessageChannel,
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
        context: ConfiguredRegisterFeatureChannelContext[ConfigT, ManagerT],
        channel: RegisterMessageChannel,
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
        channel: RegisterMessageChannel,
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
        feature_channel_context: RegisterFeatureChannelContext[ManagerT],
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

            channel = self.bot.get_channel(feature_channel_context.channel_id)
            if not isinstance(channel, RegisterMessageChannel):
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
        feature_channel_context: RegisterFeatureChannelContext[ManagerT],
    ) -> bool:
        async with self.auto_guide_lock(feature_channel_context.channel_id):
            auto_guide_state = await get_auto_guide_state(
                feature_channel_context.feature_channel
            )
            if auto_guide_state is None or auto_guide_state.message_id is None:
                return True

            channel = self.bot.get_channel(feature_channel_context.channel_id)
            if not isinstance(channel, RegisterMessageChannel):
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
            feature_channel_context = await self._get_register_feature_channel_context(
                source
            )
            context = await self._get_configured_register_feature_channel_context(
                feature_channel_context
            )
            if context is None:
                await self._send_missing_register_config_followup(interaction)
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
