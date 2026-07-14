from __future__ import annotations

import logging
from abc import abstractmethod
from typing import TYPE_CHECKING, ClassVar, Protocol, override

from discord import Interaction, app_commands
from discord.ext import commands

from bot import config
from cogs.base.discord_context import require_guild_channel_source
from cogs.base.feature_channel_base import CogABCMeta, FeatureChannelErrorHandler
from cogs.base.register_feature_channel_context import (
    ConfiguredRegisterFeatureChannelContext,
    RegisterFeatureChannelContextMixin,
)
from components.ui_auto_guide import AutoGuideButtonsView
from components.ui_feature_channel import ConfirmDeleteUserDataView
from components.ui_worksheet_contract_errors import send_worksheet_contract_error
from models.base.sheet_config_base import SheetConfigBase
from utils.google_sheets_urls import google_sheet_url_with_gid
from utils.manager_base import ManagerBase
from utils.message_templates import locale_to_template_code, render_message_template
from utils.register_i18n import register_user_text
from utils.structs_base import GoogleSheetsMetadata, UserInfo, WorksheetContractError

if TYPE_CHECKING:
    from bot import Rhoboto
    from cogs.base.discord_context import GuildChannelSource
    from utils.key_async_lock import KeyAsyncLock


class RegisterFeatureChannelType(Protocol):
    """Class-level capabilities shared by Register admin and public cogs."""

    feature_name: ClassVar[str]
    feature_display_name: ClassVar[str]
    sheet_write_lock: ClassVar[KeyAsyncLock]


@app_commands.guild_only()
class RegisterFeatureChannelUserBase[
    ConfigT: SheetConfigBase,
    MetadataT: GoogleSheetsMetadata,
    ManagerT: ManagerBase[ConfigT, MetadataT],
](
    RegisterFeatureChannelContextMixin[ConfigT, MetadataT, ManagerT],
    FeatureChannelErrorHandler,
    commands.GroupCog,
    metaclass=CogABCMeta,
):
    """Public per-user guide and delete workflow for Register features."""

    feature_name: str
    feature_display_name: str
    FeatureChannelType: type[RegisterFeatureChannelType]
    ManagerType: type[ManagerT]

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
        self, manager: ManagerT, user_info: UserInfo, metadata: MetadataT
    ) -> None: ...

    async def _delete_user_data_transaction(
        self,
        context: ConfiguredRegisterFeatureChannelContext[ConfigT, ManagerT],
        user_info: UserInfo,
    ) -> None:
        """Fetch and delete user data under the feature's default channel lock."""
        async with self.FeatureChannelType.sheet_write_lock(context.channel_id):
            metadata = await context.manager.fetch_google_sheets_metadata()
            await self._delete_user_data(context.manager, user_info, metadata)

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

            feature_channel_context = (
                await self._get_register_feature_channel_context_or_none(
                    guild_id=source.guild.id,
                    channel_id=source.channel.id,
                    require_enabled=True,
                )
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

            context = await self._get_configured_register_feature_channel_context(
                feature_channel_context
            )
            if context is None:
                await self._send_missing_register_config_followup(interaction)
                return None

            await self._delete_user_data_transaction(context, user_info)

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
            feature_channel_context = await self._get_register_feature_channel_context(
                source
            )
            context = await self._get_configured_register_feature_channel_context(
                feature_channel_context
            )
            if context is None:
                await self._send_missing_register_config_followup(interaction)
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
