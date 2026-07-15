from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, override

from discord import (
    AppCommandType,
    DiscordException,
    Interaction,
    Message,
    TextChannel,
    app_commands,
)
from discord.app_commands import locale_str
from tortoise.transactions import in_transaction

from bot import config
from cogs.base.discord_context import require_guild_channel_source
from cogs.base.feature_channel_base import FeatureChannelBase, FeatureNotEnabled
from cogs.base.message_upsert_feature_channel_base import (
    MessageUpsertFeatureChannelBase,
)
from components.ui_permissions import (
    MISSING_SETTINGS_PERMISSION_MESSAGE,
    has_settings_permissions,
)
from components.ui_room_number import (
    INITIAL_SETUP_CONTENT,
    INITIAL_SETUP_STALE_MESSAGE,
    INITIAL_SOURCE_SELECTION_CONTENT,
    PENDING_RENAME_DESCRIPTION,
    RECRUITMENT_TEMPLATE_UNREADABLE,
    RoomNumberInitialSetupView,
    RoomNumberSettingsSnapshot,
    RoomNumberSourceSelectView,
    RoomNumberUIActions,
    build_room_number_settings_panel,
    build_room_output_embed,
    build_room_output_view,
    build_room_storage_error_embed,
    build_target_output_failure_embed,
    mark_room_output_rename_failed,
    mark_room_output_rename_succeeded,
    mark_room_output_superseded,
)
from components.ui_settings_flow import (
    SETTINGS_STORAGE_EXCEPTIONS,
    prepare_replacement_settings_view,
    send_current_panel_followup,
    send_settings_view_followup,
)
from components.ui_storage_errors import send_storage_error
from models.feature_channel import FeatureChannel
from models.room_number import RoomNumberConfig
from utils.key_async_lock import KeyAsyncLock
from utils.reactions import (
    add_reaction_if_possible,
    add_reactions_if_possible,
    remove_reaction_if_present,
    transition_processing_reaction,
)
from utils.room_number import (
    DEFAULT_CHANNEL_NAME_FORMAT,
    RoomNumberFormatError,
    RoomNumberParser,
    is_recruitment_template_candidate,
    parse_room_number_text,
    render_channel_name,
    render_recruitment_template,
    validate_channel_name_format,
)
from utils.storage_errors import (
    StorageError,
    StorageOperationContext,
    classify_storage_exception,
    generate_error_reference,
)
from utils.structs_base import UserInfo

if TYPE_CHECKING:
    from datetime import datetime

    from discord import Guild
    from discord.ui import View

    from bot import Rhoboto
    from cogs.base.discord_context import GuildChannelSource


ROOM_NUMBER_FEATURE_NAME = "room_number"
ROOM_NUMBER_DISPLAY_NAME = "Room Number"
INVALID_SOURCE_CHANNEL_MESSAGE = "通常のテキストチャンネルで実行してください。"
INVALID_TARGET_CHANNEL_MESSAGE = "通常のテキストチャンネルを選択してください。"
STALE_SETTINGS_MESSAGE = "設定が更新されています。設定画面を開き直してください。"
TARGET_CONFLICT_MESSAGE = "このチャンネルは別の部屋番号設定で使用されています。"
RENAME_PARTIAL_SUCCESS_MESSAGE = (
    "設定は保存されましたが、チャンネル名を変更できませんでした。"
    "Bot の「チャンネルの管理」権限を確認してください。"
)
INVALID_MANUAL_ROOM_MESSAGE = "メッセージ全体を5〜6桁の数字だけにしてください。"
MANUAL_ROOM_CHANNEL_MESSAGE = "このチャンネルでは部屋番号が設定されていません。"
MANUAL_TEMPLATE_SUCCESS_MESSAGE = "募集テンプレに設定しました。"
MANUAL_TEMPLATE_TARGET_MESSAGE = (
    "募集テンプレは現在のTargetチャンネルで設定してください。"
)
MANUAL_ROOM_PERSISTENCE_FAILURE_MESSAGE = "部屋番号を更新できませんでした。"
MANUAL_ROOM_SUPERSEDED_MESSAGE = (
    "新しい部屋番号が設定されたため、この更新は募集情報に反映されませんでした。"
)
_GUILD_REQUIRED_ERROR = "Room Number settings require a guild."

_TARGET_PERMISSION_LABELS = (
    ("view_channel", "チャンネルを見る"),
    ("send_messages", "メッセージを送信"),
    ("embed_links", "埋め込みリンク"),
    ("read_message_history", "メッセージ履歴を読む"),
    ("manage_channels", "チャンネルの管理"),
)


class _StaleRoomSettingsError(RuntimeError):
    """A settings callback no longer owns the persisted Room snapshot."""


class _RoomChannelConflictError(RuntimeError):
    """A channel already participates in another Room configuration."""


@dataclass(frozen=True, slots=True)
class RoomUpdateResult:
    room_number: str
    persisted: bool
    naming_succeeded: bool
    output_succeeded: bool
    superseded: bool


class RoomNumber(
    MessageUpsertFeatureChannelBase[
        FeatureChannel,
        RoomNumberConfig,
        str,
        RoomUpdateResult,
    ],
    group_name=locale_str(ROOM_NUMBER_FEATURE_NAME),
    group_description=locale_str(
        "Configure and update the current room number.",
    ),
):
    feature_name = ROOM_NUMBER_FEATURE_NAME
    feature_display_name = ROOM_NUMBER_DISPLAY_NAME
    context_menu_name = "部屋番号を設定"
    ParserType = RoomNumberParser

    @override
    def __init__(self, bot: Rhoboto) -> None:
        super().__init__(bot)
        self._state_lock = KeyAsyncLock()
        self._delivery_lock = KeyAsyncLock()
        self._delivery_generations: dict[int, int] = {}
        self.recruitment_template_context_menu = app_commands.ContextMenu(
            name="募集テンプレに設定",
            callback=self.set_recruitment_template_from_context_menu,
        )
        self.recruitment_template_context_menu.add_check(
            self.feature_enabled_app_command_predicate(
                self.feature_name,
                self.feature_display_name,
            )
        )
        self.recruitment_template_context_menu.error(self.cog_app_command_error)
        bot.tree.add_command(self.recruitment_template_context_menu)

    def _ui_actions(self) -> RoomNumberUIActions:
        return RoomNumberUIActions(
            initial_setup_is_current=self._initial_setup_is_current,
            start_initial_setup=self._start_initial_setup,
            select_source=self._select_source,
            select_target=self._select_target,
            save_channel_name_format=self._save_channel_name_format,
            set_recruitment_template_enabled=(self._set_recruitment_template_enabled),
            clear_recruitment_template=self._clear_recruitment_template,
        )

    async def _send_ephemeral(
        self,
        interaction: Interaction,
        content: str,
    ) -> None:
        if interaction.response.is_done():
            await interaction.followup.send(content, ephemeral=True)
            return
        await interaction.response.send_message(content, ephemeral=True)

    async def _require_settings_permissions(
        self,
        interaction: Interaction,
    ) -> bool:
        if has_settings_permissions(interaction):
            return True
        await self._send_ephemeral(interaction, MISSING_SETTINGS_PERMISSION_MESSAGE)
        return False

    @app_commands.command(
        name="enable",
        description="Enable this feature in the current channel.",
    )
    async def enable(self, interaction: Interaction) -> None:
        if not await self._require_settings_permissions(interaction):
            return
        source = require_guild_channel_source(
            interaction,
            action="enable Room Number",
        )
        if not isinstance(source.channel, TextChannel):
            await self._send_ephemeral(interaction, INVALID_SOURCE_CHANNEL_MESSAGE)
            return
        await FeatureChannelBase.enable.callback(self, interaction)

    @app_commands.command(
        name="settings",
        description="Show and edit current feature settings for this channel.",
    )
    @app_commands.check(
        FeatureChannelBase.feature_enabled_app_command_predicate(
            ROOM_NUMBER_FEATURE_NAME,
            ROOM_NUMBER_DISPLAY_NAME,
        )
    )
    async def settings(self, interaction: Interaction) -> None:
        if not await self._require_settings_permissions(interaction):
            return
        source = require_guild_channel_source(
            interaction,
            action="show Room Number settings",
        )
        await self._validate_lifecycle_owner(source)
        await interaction.response.defer(ephemeral=True)
        await self.setup_after_enable(interaction)

    @override
    async def setup_after_enable(self, interaction: Interaction) -> None:
        source = require_guild_channel_source(
            interaction,
            action="show Room Number settings",
        )
        await self._validate_lifecycle_owner(source)
        membership = await FeatureChannel.get_or_none(
            guild_id=source.guild.id,
            channel_id=source.channel.id,
            feature_name=self.feature_name,
        )
        if membership is None:
            raise FeatureNotEnabled(self.feature_name, self.feature_display_name)
        config = await RoomNumberConfig.get_or_none(
            target_channel_id=source.channel.id,
        )
        if config is None:
            config = await RoomNumberConfig.get_or_none(
                feature_channel_id=membership.id,
            )
        if config is None:
            await send_settings_view_followup(
                interaction,
                content=INITIAL_SETUP_CONTENT,
                view=RoomNumberInitialSetupView(
                    requesting_user_id=interaction.user.id,
                    target_channel_id=source.channel.id,
                    target_feature_channel_id=membership.id,
                    actions=self._ui_actions(),
                ),
            )
            return
        snapshot = await self._settings_snapshot(config.feature_channel_id)
        panel = build_room_number_settings_panel(
            source.guild,
            snapshot,
            self._ui_actions(),
        )
        await send_current_panel_followup(interaction, panel)

    async def _initial_setup_is_current(
        self,
        target_feature_channel_id: int,
    ) -> bool:
        membership = await FeatureChannel.get_or_none(
            id=target_feature_channel_id,
            feature_name=self.feature_name,
        )
        if membership is None or not membership.is_enabled:
            return False
        if (
            await RoomNumberConfig.get_or_none(
                feature_channel_id=membership.id,
            )
            is not None
        ):
            return False
        return (
            await RoomNumberConfig.get_or_none(
                target_channel_id=membership.channel_id,
            )
            is None
        )

    async def _start_initial_setup(
        self,
        interaction: Interaction,
        target_feature_channel_id: int,
        target_channel_id: int,
        channel_name_format: str,
    ) -> None:
        try:
            validated_format = validate_channel_name_format(channel_name_format)
        except RoomNumberFormatError as exc:
            await self._send_ephemeral(interaction, str(exc))
            return
        source = require_guild_channel_source(
            interaction,
            action="continue Room Number setup",
        )
        if (
            source.channel.id != target_channel_id
            or not await self._initial_setup_is_current(target_feature_channel_id)
        ):
            await self._send_ephemeral(interaction, INITIAL_SETUP_STALE_MESSAGE)
            return
        await send_settings_view_followup(
            interaction,
            content=INITIAL_SOURCE_SELECTION_CONTENT,
            view=RoomNumberSourceSelectView(
                requesting_user_id=interaction.user.id,
                target_channel_id=target_channel_id,
                target_feature_channel_id=target_feature_channel_id,
                channel_name_format=validated_format,
                actions=self._ui_actions(),
            ),
        )

    async def _settings_snapshot(
        self,
        source_feature_channel_id: int,
    ) -> RoomNumberSettingsSnapshot:
        membership = await FeatureChannel.get_or_none(
            id=source_feature_channel_id,
            feature_name=self.feature_name,
        )
        if membership is None:
            raise _StaleRoomSettingsError
        config = await RoomNumberConfig.get_or_none(
            feature_channel_id=membership.id,
        )
        return RoomNumberSettingsSnapshot(
            source_feature_channel_id=membership.id,
            source_channel_id=membership.channel_id,
            target_channel_id=(config.target_channel_id if config else None),
            config_id=(config.id if config else None),
            updated_at=(config.updated_at if config else None),
            room_number=(config.room_number if config else None),
            channel_name_format=(
                config.channel_name_format
                if config is not None
                else DEFAULT_CHANNEL_NAME_FORMAT
            ),
            recruitment_template_enabled=(
                config.recruitment_template_enabled if config is not None else True
            ),
            recruitment_template_channel_id=(
                config.recruitment_template_channel_id if config else None
            ),
            recruitment_template_message_id=(
                config.recruitment_template_message_id if config else None
            ),
        )

    async def _refresh_settings(
        self,
        interaction: Interaction,
        source_feature_channel_id: int,
        current_view: View,
    ) -> None:
        guild = interaction.guild
        if guild is None:
            raise ValueError(_GUILD_REQUIRED_ERROR)
        snapshot = await self._settings_snapshot(source_feature_channel_id)
        panel = build_room_number_settings_panel(
            guild,
            snapshot,
            self._ui_actions(),
        )
        replacement = prepare_replacement_settings_view(current_view, panel.view)
        await interaction.edit_original_response(
            content=None,
            embed=panel.embed,
            view=replacement,
        )

    @override
    async def _validate_lifecycle_owner(
        self,
        source: GuildChannelSource,
    ) -> None:
        if hasattr(source, "user") and not has_settings_permissions(source):
            raise app_commands.MissingPermissions(["administrator", "manage_channels"])
        membership = await FeatureChannel.get_or_none(
            guild_id=source.guild.id,
            channel_id=source.channel.id,
            feature_name=self.feature_name,
        )
        if membership is None:
            return
        target_config = await RoomNumberConfig.get_or_none(
            target_channel_id=source.channel.id,
        )
        if target_config is not None:
            return
        source_config = await RoomNumberConfig.get_or_none(
            feature_channel_id=membership.id,
        )
        if source_config is not None:
            raise FeatureNotEnabled(self.feature_name, self.feature_display_name)

    def _missing_target_permissions(
        self,
        guild: Guild,
        target: TextChannel,
    ) -> tuple[str, ...]:
        bot_member = getattr(guild, "me", None)
        if bot_member is None:
            return tuple(label for _, label in _TARGET_PERMISSION_LABELS)
        permissions = target.permissions_for(bot_member)
        return tuple(
            label
            for attribute, label in _TARGET_PERMISSION_LABELS
            if not getattr(permissions, attribute, False)
        )

    async def _locked_source_membership(
        self,
        connection: object,
        *,
        source_feature_channel_id: int,
        guild_id: int,
        source_channel_id: int,
    ) -> FeatureChannel:
        membership = await (
            FeatureChannel.filter(
                id=source_feature_channel_id,
                guild_id=guild_id,
                channel_id=source_channel_id,
                feature_name=self.feature_name,
            )
            .using_db(connection)
            .select_for_update()
            .first()
        )
        if membership is None:
            raise _StaleRoomSettingsError
        return membership

    async def _persist_initial_source_selection(
        self,
        *,
        guild_id: int,
        target_channel_id: int,
        target_feature_channel_id: int,
        source_channel_id: int,
        channel_name_format: str,
    ) -> RoomNumberConfig:
        async with self._state_lock(target_channel_id):
            async with in_transaction() as connection:
                target_membership = await (
                    FeatureChannel.filter(
                        id=target_feature_channel_id,
                        guild_id=guild_id,
                        channel_id=target_channel_id,
                        feature_name=self.feature_name,
                    )
                    .using_db(connection)
                    .select_for_update()
                    .first()
                )
                if target_membership is None or not target_membership.is_enabled:
                    raise _StaleRoomSettingsError
                if (
                    await RoomNumberConfig.filter(
                        target_channel_id=target_channel_id,
                    )
                    .using_db(connection)
                    .select_for_update()
                    .first()
                    is not None
                ):
                    raise _StaleRoomSettingsError

                source_as_target = await (
                    RoomNumberConfig.filter(target_channel_id=source_channel_id)
                    .using_db(connection)
                    .select_for_update()
                    .first()
                )
                if source_as_target is not None:
                    raise _RoomChannelConflictError
                source_membership = await (
                    FeatureChannel.filter(
                        guild_id=guild_id,
                        channel_id=source_channel_id,
                        feature_name=self.feature_name,
                    )
                    .using_db(connection)
                    .select_for_update()
                    .first()
                )
                if source_membership is None:
                    source_membership = await FeatureChannel.create(
                        using_db=connection,
                        guild_id=guild_id,
                        channel_id=source_channel_id,
                        feature_name=self.feature_name,
                        is_enabled=True,
                    )
                else:
                    source_config = await (
                        RoomNumberConfig.filter(
                            feature_channel_id=source_membership.id,
                        )
                        .using_db(connection)
                        .select_for_update()
                        .first()
                    )
                    if source_config is not None:
                        raise _RoomChannelConflictError
                    if not source_membership.is_enabled:
                        source_membership.is_enabled = True
                        await source_membership.save(
                            using_db=connection,
                            update_fields=["is_enabled", "updated_at"],
                        )
                config = await RoomNumberConfig.create(
                    using_db=connection,
                    feature_channel_id=source_membership.id,
                    target_channel_id=target_channel_id,
                    channel_name_format=channel_name_format,
                )
            self._delivery_generations[source_membership.channel_id] = (
                self._delivery_generations.get(source_membership.channel_id, 0) + 1
            )
        return config

    @staticmethod
    def _require_current_config(
        config: RoomNumberConfig | None,
        *,
        expected_config_id: int | None,
        expected_updated_at: datetime | None,
    ) -> None:
        if expected_config_id is None:
            if config is not None or expected_updated_at is not None:
                raise _StaleRoomSettingsError
            return
        if (
            config is None
            or config.id != expected_config_id
            or config.updated_at != expected_updated_at
        ):
            raise _StaleRoomSettingsError

    async def _available_target_membership(  # noqa: PLR0913
        self,
        connection: object,
        *,
        guild_id: int,
        source_membership: FeatureChannel,
        source_channel_id: int,
        target_channel_id: int,
        config: RoomNumberConfig | None,
    ) -> FeatureChannel | None:
        source_as_other_target = await (
            RoomNumberConfig.filter(target_channel_id=source_channel_id)
            .using_db(connection)
            .select_for_update()
            .first()
        )
        if source_as_other_target is not None and (
            config is None or source_as_other_target.id != config.id
        ):
            raise _RoomChannelConflictError

        other_target_config = await (
            RoomNumberConfig.filter(target_channel_id=target_channel_id)
            .using_db(connection)
            .select_for_update()
            .first()
        )
        if other_target_config is not None and (
            config is None or other_target_config.id != config.id
        ):
            raise _RoomChannelConflictError

        target_membership = await (
            FeatureChannel.filter(
                guild_id=guild_id,
                channel_id=target_channel_id,
                feature_name=self.feature_name,
            )
            .using_db(connection)
            .select_for_update()
            .first()
        )
        current_target_id = config.target_channel_id if config else None
        allowed_existing_ids = {source_membership.id}
        if current_target_id == target_channel_id and target_membership is not None:
            allowed_existing_ids.add(target_membership.id)
        if (
            target_membership is not None
            and target_membership.id not in allowed_existing_ids
        ):
            raise _RoomChannelConflictError
        return target_membership

    async def _persist_target_selection(  # noqa: PLR0913
        self,
        *,
        guild_id: int,
        source_channel_id: int,
        source_feature_channel_id: int,
        target_channel_id: int,
        expected_config_id: int | None,
        expected_updated_at: datetime | None,
    ) -> RoomNumberConfig:
        async with self._state_lock(source_channel_id):
            async with in_transaction() as connection:
                source_membership = await self._locked_source_membership(
                    connection,
                    source_feature_channel_id=source_feature_channel_id,
                    guild_id=guild_id,
                    source_channel_id=source_channel_id,
                )
                config = await (
                    RoomNumberConfig.filter(feature_channel_id=source_membership.id)
                    .using_db(connection)
                    .select_for_update()
                    .first()
                )
                self._require_current_config(
                    config,
                    expected_config_id=expected_config_id,
                    expected_updated_at=expected_updated_at,
                )
                target_membership = await self._available_target_membership(
                    connection,
                    guild_id=guild_id,
                    source_membership=source_membership,
                    source_channel_id=source_channel_id,
                    target_channel_id=target_channel_id,
                    config=config,
                )
                old_target_id = config.target_channel_id if config else None
                if target_membership is None:
                    target_membership = await FeatureChannel.create(
                        using_db=connection,
                        guild_id=guild_id,
                        channel_id=target_channel_id,
                        feature_name=self.feature_name,
                        is_enabled=True,
                    )
                elif not target_membership.is_enabled:
                    target_membership.is_enabled = True
                    await target_membership.save(
                        using_db=connection,
                        update_fields=["is_enabled", "updated_at"],
                    )
                if not source_membership.is_enabled:
                    source_membership.is_enabled = True
                    await source_membership.save(
                        using_db=connection,
                        update_fields=["is_enabled", "updated_at"],
                    )
                if config is None:
                    config = await RoomNumberConfig.create(
                        using_db=connection,
                        feature_channel_id=source_membership.id,
                        target_channel_id=target_channel_id,
                    )
                else:
                    config.target_channel_id = target_channel_id
                    await config.save(
                        using_db=connection,
                        update_fields=["target_channel_id", "updated_at"],
                    )
                if old_target_id is not None and old_target_id not in {
                    source_channel_id,
                    target_channel_id,
                }:
                    await (
                        FeatureChannel.filter(
                            guild_id=guild_id,
                            channel_id=old_target_id,
                            feature_name=self.feature_name,
                        )
                        .using_db(connection)
                        .delete()
                    )
            self._delivery_generations[source_channel_id] = (
                self._delivery_generations.get(source_channel_id, 0) + 1
            )
        return config

    async def _select_target(  # noqa: PLR0911, PLR0913
        self,
        interaction: Interaction,
        source_feature_channel_id: int,
        expected_config_id: int | None,
        expected_updated_at: datetime | None,
        target_channel_id: int,
        current_view: View,
    ) -> None:
        if not await self._require_settings_permissions(interaction):
            return
        source = require_guild_channel_source(
            interaction,
            action="select a Room Number target",
        )
        target = source.guild.get_channel(target_channel_id)
        if not isinstance(target, TextChannel) or target.guild.id != source.guild.id:
            await self._send_ephemeral(interaction, INVALID_TARGET_CHANNEL_MESSAGE)
            return
        missing_permissions = self._missing_target_permissions(source.guild, target)
        if missing_permissions:
            await self._send_ephemeral(
                interaction,
                "Bot に次の権限が必要です: " + "、".join(missing_permissions),
            )
            return
        source_membership = await FeatureChannel.get_or_none(
            id=source_feature_channel_id,
            guild_id=source.guild.id,
            feature_name=self.feature_name,
        )
        if source_membership is None:
            await self._send_ephemeral(interaction, STALE_SETTINGS_MESSAGE)
            return
        source_channel_id = source_membership.channel_id
        if expected_config_id is None and source.channel.id != source_channel_id:
            await self._send_ephemeral(interaction, STALE_SETTINGS_MESSAGE)
            return

        try:
            config = await self._persist_target_selection(
                guild_id=source.guild.id,
                source_channel_id=source_channel_id,
                source_feature_channel_id=source_feature_channel_id,
                target_channel_id=target.id,
                expected_config_id=expected_config_id,
                expected_updated_at=expected_updated_at,
            )
        except _StaleRoomSettingsError:
            await self._send_ephemeral(interaction, STALE_SETTINGS_MESSAGE)
            return
        except _RoomChannelConflictError:
            await self._send_ephemeral(interaction, TARGET_CONFLICT_MESSAGE)
            return
        except SETTINGS_STORAGE_EXCEPTIONS as exc:
            await self._send_interaction_storage_error_or_raise(
                interaction,
                exc,
                source=source,
                operation="room_target_selection",
            )
            return

        async with self._delivery_lock(source_channel_id):
            naming_succeeded = await self._apply_current_name(source.guild, config)
        if not naming_succeeded:
            await self._send_ephemeral(interaction, RENAME_PARTIAL_SUCCESS_MESSAGE)
        await self._refresh_settings(
            interaction,
            source_feature_channel_id,
            current_view,
        )

    async def _select_source(  # noqa: PLR0911, PLR0913
        self,
        interaction: Interaction,
        target_feature_channel_id: int,
        target_channel_id: int,
        channel_name_format: str,
        source_channel_id: int,
        current_view: View,
    ) -> None:
        if not await self._require_settings_permissions(interaction):
            return
        source = require_guild_channel_source(
            interaction,
            action="select a Room Number source",
        )
        selected_source = source.guild.get_channel(source_channel_id)
        target = source.guild.get_channel(target_channel_id)
        if not isinstance(selected_source, TextChannel):
            await self._send_ephemeral(interaction, INVALID_SOURCE_CHANNEL_MESSAGE)
            return
        if not isinstance(target, TextChannel) or target.guild.id != source.guild.id:
            await self._send_ephemeral(interaction, INVALID_TARGET_CHANNEL_MESSAGE)
            return
        missing_permissions = self._missing_target_permissions(source.guild, target)
        if missing_permissions:
            await self._send_ephemeral(
                interaction,
                "Bot に次の権限が必要です: " + "、".join(missing_permissions),
            )
            return
        try:
            validated_format = validate_channel_name_format(channel_name_format)
            config = await self._persist_initial_source_selection(
                guild_id=source.guild.id,
                target_channel_id=target_channel_id,
                target_feature_channel_id=target_feature_channel_id,
                source_channel_id=selected_source.id,
                channel_name_format=validated_format,
            )
        except RoomNumberFormatError as exc:
            await self._send_ephemeral(interaction, str(exc))
            return
        except _StaleRoomSettingsError:
            await self._send_ephemeral(interaction, INITIAL_SETUP_STALE_MESSAGE)
            return
        except _RoomChannelConflictError:
            await self._send_ephemeral(interaction, TARGET_CONFLICT_MESSAGE)
            return
        except SETTINGS_STORAGE_EXCEPTIONS as exc:
            await self._send_interaction_storage_error_or_raise(
                interaction,
                exc,
                source=source,
                operation="room_source_selection",
            )
            return

        async with self._delivery_lock(selected_source.id):
            naming_succeeded = await self._apply_current_name(source.guild, config)
        if not naming_succeeded:
            await self._send_ephemeral(interaction, RENAME_PARTIAL_SUCCESS_MESSAGE)
        await self._refresh_settings(
            interaction,
            config.feature_channel_id,
            current_view,
        )

    async def _locked_owned_config(
        self,
        connection: object,
        *,
        config_id: int,
        expected_updated_at: datetime,
        guild_id: int,
        source_channel_id: int,
    ) -> tuple[FeatureChannel, RoomNumberConfig]:
        config = await (
            RoomNumberConfig.filter(id=config_id)
            .using_db(connection)
            .select_for_update()
            .first()
        )
        if config is None or config.updated_at != expected_updated_at:
            raise _StaleRoomSettingsError
        membership = await self._locked_source_membership(
            connection,
            source_feature_channel_id=config.feature_channel_id,
            guild_id=guild_id,
            source_channel_id=source_channel_id,
        )
        return membership, config

    async def _source_channel_id_for_config(
        self,
        config_id: int,
        expected_updated_at: datetime,
    ) -> int:
        config = await RoomNumberConfig.get_or_none(id=config_id)
        if config is None or config.updated_at != expected_updated_at:
            raise _StaleRoomSettingsError
        membership = await FeatureChannel.get_or_none(
            id=config.feature_channel_id,
            feature_name=self.feature_name,
        )
        if membership is None:
            raise _StaleRoomSettingsError
        return membership.channel_id

    async def _save_channel_name_format(
        self,
        interaction: Interaction,
        config_id: int,
        expected_updated_at: datetime,
        channel_name_format: str,
        current_view: View,
    ) -> None:
        if not await self._require_settings_permissions(interaction):
            return
        try:
            validated_format = validate_channel_name_format(channel_name_format)
        except RoomNumberFormatError as exc:
            await self._send_ephemeral(interaction, str(exc))
            return
        source = require_guild_channel_source(
            interaction,
            action="save the Room Number channel format",
        )
        try:
            source_channel_id = await self._source_channel_id_for_config(
                config_id,
                expected_updated_at,
            )
            async with self._state_lock(source_channel_id):
                async with in_transaction() as connection:
                    membership, config = await self._locked_owned_config(
                        connection,
                        config_id=config_id,
                        expected_updated_at=expected_updated_at,
                        guild_id=source.guild.id,
                        source_channel_id=source_channel_id,
                    )
                    config.channel_name_format = validated_format
                    await config.save(
                        using_db=connection,
                        update_fields=["channel_name_format", "updated_at"],
                    )
                self._delivery_generations[source_channel_id] = (
                    self._delivery_generations.get(source_channel_id, 0) + 1
                )
        except _StaleRoomSettingsError:
            await self._send_ephemeral(interaction, STALE_SETTINGS_MESSAGE)
            return
        except SETTINGS_STORAGE_EXCEPTIONS as exc:
            await self._send_interaction_storage_error_or_raise(
                interaction,
                exc,
                source=source,
                operation="room_channel_format",
            )
            return

        async with self._delivery_lock(source_channel_id):
            naming_succeeded = await self._apply_current_name(source.guild, config)
        if not naming_succeeded:
            await self._send_ephemeral(interaction, RENAME_PARTIAL_SUCCESS_MESSAGE)
        await self._refresh_settings(interaction, membership.id, current_view)

    async def _set_recruitment_template_enabled(
        self,
        interaction: Interaction,
        config_id: int,
        expected_updated_at: datetime,
        enabled: bool,  # noqa: FBT001
        current_view: View,
    ) -> None:
        if not await self._require_settings_permissions(interaction):
            return
        source = require_guild_channel_source(
            interaction,
            action="toggle the Room Number recruitment template",
        )
        try:
            source_channel_id = await self._source_channel_id_for_config(
                config_id,
                expected_updated_at,
            )
            async with (
                self._state_lock(source_channel_id),
                in_transaction() as connection,
            ):
                membership, config = await self._locked_owned_config(
                    connection,
                    config_id=config_id,
                    expected_updated_at=expected_updated_at,
                    guild_id=source.guild.id,
                    source_channel_id=source_channel_id,
                )
                config.recruitment_template_enabled = enabled
                await config.save(
                    using_db=connection,
                    update_fields=[
                        "recruitment_template_enabled",
                        "updated_at",
                    ],
                )
        except _StaleRoomSettingsError:
            await self._send_ephemeral(interaction, STALE_SETTINGS_MESSAGE)
            return
        except SETTINGS_STORAGE_EXCEPTIONS as exc:
            await self._send_interaction_storage_error_or_raise(
                interaction,
                exc,
                source=source,
                operation="room_template_toggle",
            )
            return
        await self._refresh_settings(interaction, membership.id, current_view)

    async def _clear_recruitment_template(
        self,
        interaction: Interaction,
        config_id: int,
        expected_updated_at: datetime,
        current_view: View,
    ) -> None:
        if not await self._require_settings_permissions(interaction):
            return
        source = require_guild_channel_source(
            interaction,
            action="clear the Room Number recruitment template",
        )
        try:
            source_channel_id = await self._source_channel_id_for_config(
                config_id,
                expected_updated_at,
            )
            async with (
                self._state_lock(source_channel_id),
                in_transaction() as connection,
            ):
                membership, config = await self._locked_owned_config(
                    connection,
                    config_id=config_id,
                    expected_updated_at=expected_updated_at,
                    guild_id=source.guild.id,
                    source_channel_id=source_channel_id,
                )
                config.recruitment_template_channel_id = None
                config.recruitment_template_message_id = None
                await config.save(
                    using_db=connection,
                    update_fields=[
                        "recruitment_template_channel_id",
                        "recruitment_template_message_id",
                        "updated_at",
                    ],
                )
            self._delivery_generations[source_channel_id] = (
                self._delivery_generations.get(source_channel_id, 0) + 1
            )
        except _StaleRoomSettingsError:
            await self._send_ephemeral(interaction, STALE_SETTINGS_MESSAGE)
            return
        except SETTINGS_STORAGE_EXCEPTIONS as exc:
            await self._send_interaction_storage_error_or_raise(
                interaction,
                exc,
                source=source,
                operation="room_template_clear",
            )
            return
        await self._refresh_settings(interaction, membership.id, current_view)

    async def _apply_current_name(
        self,
        guild: Guild,
        config: RoomNumberConfig,
    ) -> bool:
        if config.room_number is None:
            return True
        target = guild.get_channel(config.target_channel_id)
        if not isinstance(target, TextChannel):
            return False
        desired_name = render_channel_name(
            config.channel_name_format,
            config.room_number,
        )
        if target.name == desired_name:
            return True
        try:
            await target.edit(name=desired_name)
        except Exception:
            self.logger.exception(
                "Failed to apply Room Number channel name. guild=%s channel=%s",
                guild.id,
                target.id,
            )
            return False
        return True

    async def _state_channel_id_for_channel(
        self,
        guild_id: int,
        channel_id: int,
    ) -> int:
        membership = await FeatureChannel.get_or_none(
            guild_id=guild_id,
            channel_id=channel_id,
            feature_name=self.feature_name,
        )
        config = None
        if membership is not None:
            config = await RoomNumberConfig.get_or_none(
                feature_channel_id=membership.id,
            )
        if config is None:
            config = await RoomNumberConfig.get_or_none(
                target_channel_id=channel_id,
            )
        if config is None:
            return channel_id
        source_membership = await FeatureChannel.get_or_none(
            id=config.feature_channel_id,
            feature_name=self.feature_name,
        )
        return (
            source_membership.channel_id
            if source_membership is not None
            else channel_id
        )

    @override
    async def _enable_channel(self, guild_id: int, channel_id: int) -> None:
        state_channel_id = await self._state_channel_id_for_channel(
            guild_id,
            channel_id,
        )
        async with (
            self._state_lock(state_channel_id),
            in_transaction() as connection,
        ):
            membership = await (
                FeatureChannel.filter(
                    guild_id=guild_id,
                    channel_id=channel_id,
                    feature_name=self.feature_name,
                )
                .using_db(connection)
                .select_for_update()
                .first()
            )
            if membership is None:
                membership = await FeatureChannel.create(
                    using_db=connection,
                    guild_id=guild_id,
                    channel_id=channel_id,
                    feature_name=self.feature_name,
                    is_enabled=True,
                )
            elif not membership.is_enabled:
                membership.is_enabled = True
                await membership.save(
                    using_db=connection,
                    update_fields=["is_enabled", "updated_at"],
                )
            config = await (
                RoomNumberConfig.filter(feature_channel_id=membership.id)
                .using_db(connection)
                .select_for_update()
                .first()
            )
            if config is None:
                config = await (
                    RoomNumberConfig.filter(target_channel_id=channel_id)
                    .using_db(connection)
                    .select_for_update()
                    .first()
                )
            if config is None:
                return
            source_membership = await (
                FeatureChannel.filter(
                    id=config.feature_channel_id,
                    guild_id=guild_id,
                    feature_name=self.feature_name,
                )
                .using_db(connection)
                .select_for_update()
                .first()
            )
            if source_membership is None:
                return
            if not source_membership.is_enabled:
                source_membership.is_enabled = True
                await source_membership.save(
                    using_db=connection,
                    update_fields=["is_enabled", "updated_at"],
                )
            target_membership = await (
                FeatureChannel.filter(
                    guild_id=guild_id,
                    channel_id=config.target_channel_id,
                    feature_name=self.feature_name,
                )
                .using_db(connection)
                .select_for_update()
                .first()
            )
            if target_membership is None:
                await FeatureChannel.create(
                    using_db=connection,
                    guild_id=guild_id,
                    channel_id=config.target_channel_id,
                    feature_name=self.feature_name,
                    is_enabled=True,
                )
            elif not target_membership.is_enabled:
                target_membership.is_enabled = True
                await target_membership.save(
                    using_db=connection,
                    update_fields=["is_enabled", "updated_at"],
                )

    @override
    async def _disable_channel(self, guild_id: int, channel_id: int) -> bool:
        state_channel_id = await self._state_channel_id_for_channel(
            guild_id,
            channel_id,
        )
        async with self._state_lock(state_channel_id):
            async with in_transaction() as connection:
                membership = await (
                    FeatureChannel.filter(
                        guild_id=guild_id,
                        channel_id=channel_id,
                        feature_name=self.feature_name,
                    )
                    .using_db(connection)
                    .select_for_update()
                    .first()
                )
                if membership is None:
                    return False
                config = await (
                    RoomNumberConfig.filter(feature_channel_id=membership.id)
                    .using_db(connection)
                    .select_for_update()
                    .first()
                )
                if config is None:
                    config = await (
                        RoomNumberConfig.filter(target_channel_id=channel_id)
                        .using_db(connection)
                        .select_for_update()
                        .first()
                    )
                memberships = [membership]
                if config is not None:
                    source_membership = await (
                        FeatureChannel.filter(
                            id=config.feature_channel_id,
                            guild_id=guild_id,
                            feature_name=self.feature_name,
                        )
                        .using_db(connection)
                        .select_for_update()
                        .first()
                    )
                    target_membership = await (
                        FeatureChannel.filter(
                            guild_id=guild_id,
                            channel_id=config.target_channel_id,
                            feature_name=self.feature_name,
                        )
                        .using_db(connection)
                        .select_for_update()
                        .first()
                    )
                    if source_membership is not None and all(
                        item.id != source_membership.id for item in memberships
                    ):
                        memberships.append(source_membership)
                    if target_membership is not None and all(
                        item.id != target_membership.id for item in memberships
                    ):
                        memberships.append(target_membership)
                for paired_membership in memberships:
                    if paired_membership.is_enabled:
                        paired_membership.is_enabled = False
                        await paired_membership.save(
                            using_db=connection,
                            update_fields=["is_enabled", "updated_at"],
                        )
            self._delivery_generations.pop(state_channel_id, None)
            return True

    @override
    async def _clear_feature_settings(
        self,
        guild_id: int,
        channel_id: int,
    ) -> None:
        state_channel_id = await self._state_channel_id_for_channel(
            guild_id,
            channel_id,
        )
        async with self._state_lock(state_channel_id):
            async with in_transaction() as connection:
                membership = await (
                    FeatureChannel.filter(
                        guild_id=guild_id,
                        channel_id=channel_id,
                        feature_name=self.feature_name,
                    )
                    .using_db(connection)
                    .select_for_update()
                    .first()
                )
                if membership is None:
                    return
                config = await (
                    RoomNumberConfig.filter(feature_channel_id=membership.id)
                    .using_db(connection)
                    .select_for_update()
                    .first()
                )
                if config is None:
                    config = await (
                        RoomNumberConfig.filter(target_channel_id=channel_id)
                        .using_db(connection)
                        .select_for_update()
                        .first()
                    )
                if config is None:
                    await (
                        FeatureChannel.filter(
                            id=membership.id,
                        )
                        .using_db(connection)
                        .delete()
                    )
                else:
                    source_membership = await (
                        FeatureChannel.filter(
                            id=config.feature_channel_id,
                            guild_id=guild_id,
                            feature_name=self.feature_name,
                        )
                        .using_db(connection)
                        .select_for_update()
                        .first()
                    )
                    await (
                        RoomNumberConfig.filter(id=config.id)
                        .using_db(connection)
                        .delete()
                    )
                    await (
                        FeatureChannel.filter(
                            guild_id=guild_id,
                            channel_id=config.target_channel_id,
                            feature_name=self.feature_name,
                        )
                        .using_db(connection)
                        .delete()
                    )
                    if source_membership is not None:
                        await (
                            FeatureChannel.filter(id=source_membership.id)
                            .using_db(connection)
                            .delete()
                        )
            self._delivery_generations.pop(state_channel_id, None)

    @override
    def _build_message_context(self, membership: FeatureChannel) -> FeatureChannel:
        return membership

    @override
    async def _get_configured_message_context(
        self,
        context: FeatureChannel,
    ) -> RoomNumberConfig | None:
        config_row = await (
            RoomNumberConfig.filter(feature_channel_id=context.id)
            .select_related("feature_channel")
            .first()
        )
        if config_row is None:
            config_row = await (
                RoomNumberConfig.filter(target_channel_id=context.channel_id)
                .select_related("feature_channel")
                .first()
            )
        if config_row is None:
            return None
        source_membership = config_row.feature_channel
        if (
            source_membership.guild_id != context.guild_id
            or not source_membership.is_enabled
            or context.channel_id
            not in {source_membership.channel_id, config_row.target_channel_id}
        ):
            return None
        return config_row

    @override
    async def _process_configured_message_submission(
        self,
        message: Message,
        context: RoomNumberConfig,
        submission: str,
        user_info: UserInfo,
    ) -> RoomUpdateResult | None:
        return await self._handle_room_update(
            message,
            context,
            submission,
            user_info,
        )

    @override
    async def _process_enabled_message(
        self,
        message: Message,
        context: FeatureChannel,
    ) -> None:
        await self._process_feature_channel_message_with_outcome(message, context)
        if not is_recruitment_template_candidate(message.content):
            return
        config_row = await self._get_configured_message_context(context)
        if config_row is not None:
            await self._capture_automatic_template(message, config_row)

    @override
    async def _process_context_menu_message(
        self,
        interaction: Interaction,
        message: Message,
        source: GuildChannelSource,
    ) -> None:
        if (
            message.guild is None
            or message.channel is None
            or message.guild.id != source.guild.id
            or message.channel.id != source.channel.id
        ):
            await self._send_ephemeral(interaction, MANUAL_ROOM_CHANNEL_MESSAGE)
            return
        room_number = parse_room_number_text(message.content)
        if room_number is None:
            await self._send_ephemeral(interaction, INVALID_MANUAL_ROOM_MESSAGE)
            return
        try:
            config_row = await self._enabled_config_for_channel(
                source.guild.id,
                source.channel.id,
            )
        except SETTINGS_STORAGE_EXCEPTIONS as exc:
            await self._send_interaction_storage_error_or_raise(
                interaction,
                exc,
                source=source,
                operation="room_context_menu_lookup",
            )
            return
        if config_row is None:
            await self._send_ephemeral(interaction, MANUAL_ROOM_CHANNEL_MESSAGE)
            return
        actor = UserInfo(
            username=interaction.user.name,
            display_name=interaction.user.display_name,
        )
        result = await self._handle_room_update(
            message,
            config_row,
            room_number,
            actor,
        )
        await self._send_ephemeral(
            interaction,
            self._manual_room_result_message(result),
        )

    @override
    @app_commands.default_permissions(administrator=True, manage_channels=True)
    async def upsert_from_content_menu(
        self,
        interaction: Interaction,
        message: Message,
    ) -> None:
        if not await self._require_settings_permissions(interaction):
            return
        source = require_guild_channel_source(
            interaction,
            action="set the Room Number from a context menu",
        )
        await interaction.response.defer(ephemeral=True)
        await self._process_context_menu_message(interaction, message, source)

    @app_commands.default_permissions(administrator=True, manage_channels=True)
    async def set_recruitment_template_from_context_menu(
        self,
        interaction: Interaction,
        message: Message,
    ) -> None:
        if not await self._require_settings_permissions(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        if message.guild is None or message.channel is None:
            await self._send_ephemeral(interaction, MANUAL_TEMPLATE_TARGET_MESSAGE)
            return
        storage_context = StorageOperationContext(
            operation="room_template_context_menu",
            feature_name=self.feature_name,
            guild_id=message.guild.id,
            channel_id=message.channel.id,
            message_id=message.id,
        )
        try:
            config_row = await self._enabled_config_for_channel(
                message.guild.id,
                message.channel.id,
            )
        except SETTINGS_STORAGE_EXCEPTIONS as exc:
            storage_error = classify_storage_exception(exc)
            if storage_error is None:
                raise
            await send_storage_error(
                interaction,
                storage_error,
                context=storage_context,
                log=self.logger,
            )
            return
        if config_row is None or config_row.target_channel_id != message.channel.id:
            await self._send_ephemeral(interaction, MANUAL_TEMPLATE_TARGET_MESSAGE)
            return
        try:
            render_recruitment_template(message.content, "123456")
        except RoomNumberFormatError as exc:
            await self._send_ephemeral(interaction, str(exc))
            return
        try:
            replaced = await self._replace_template_pointer(
                config_row,
                message,
                require_enabled=False,
            )
        except SETTINGS_STORAGE_EXCEPTIONS as exc:
            storage_error = classify_storage_exception(exc)
            if storage_error is None:
                raise
            await send_storage_error(
                interaction,
                storage_error,
                context=storage_context,
                log=self.logger,
            )
            return
        if replaced:
            await self._refresh_current_room_template_output(
                message,
                config_row,
                UserInfo(
                    username=interaction.user.name,
                    display_name=interaction.user.display_name,
                ),
            )
        message_text = (
            MANUAL_TEMPLATE_SUCCESS_MESSAGE
            if replaced
            else MANUAL_TEMPLATE_TARGET_MESSAGE
        )
        await self._send_ephemeral(interaction, message_text)

    async def _enabled_config_for_channel(
        self,
        guild_id: int,
        channel_id: int,
    ) -> RoomNumberConfig | None:
        membership = await FeatureChannel.get_or_none(
            guild_id=guild_id,
            channel_id=channel_id,
            feature_name=self.feature_name,
            is_enabled=True,
        )
        if membership is None:
            return None
        return await self._get_configured_message_context(membership)

    @staticmethod
    def _template_author_is_authorized(message: Message) -> bool:
        permissions = getattr(message.author, "guild_permissions", None)
        return bool(
            getattr(permissions, "administrator", False)
            and getattr(permissions, "manage_channels", False)
        )

    async def _capture_automatic_template(
        self,
        message: Message,
        config_row: RoomNumberConfig,
    ) -> None:
        if (
            not config_row.recruitment_template_enabled
            or message.channel.id != config_row.target_channel_id
            or not self._template_author_is_authorized(message)
        ):
            return
        try:
            render_recruitment_template(message.content, "123456")
        except RoomNumberFormatError:
            await add_reactions_if_possible(
                message,
                (config.WARNING_EMOJI, "📏"),
                log=self.logger,
            )
            return
        try:
            replaced = await self._replace_template_pointer(
                config_row,
                message,
                require_enabled=True,
            )
        except Exception as exc:
            storage_error = classify_storage_exception(exc)
            if storage_error is None:
                raise
            await add_reactions_if_possible(
                message,
                (config.WARNING_EMOJI, "🛠️"),
                log=self.logger,
            )
            return
        if replaced:
            await self._refresh_current_room_template_output(
                message,
                config_row,
                self._message_user_info(message),
            )
            await add_reaction_if_possible(message, "🔄", log=self.logger)

    async def _replace_template_pointer(
        self,
        config_row: RoomNumberConfig,
        message: Message,
        *,
        require_enabled: bool,
    ) -> bool:
        source_membership = config_row.feature_channel
        async with (
            self._state_lock(source_membership.channel_id),
            in_transaction() as connection,
        ):
            fresh_config = await (
                RoomNumberConfig.filter(id=config_row.id)
                .using_db(connection)
                .select_for_update()
                .first()
            )
            if fresh_config is None or message.guild is None:
                return False
            fresh_source = await (
                FeatureChannel.filter(
                    id=fresh_config.feature_channel_id,
                    guild_id=message.guild.id,
                    feature_name=self.feature_name,
                    is_enabled=True,
                )
                .using_db(connection)
                .select_for_update()
                .first()
            )
            fresh_target = await (
                FeatureChannel.filter(
                    guild_id=message.guild.id,
                    channel_id=message.channel.id,
                    feature_name=self.feature_name,
                    is_enabled=True,
                )
                .using_db(connection)
                .select_for_update()
                .first()
            )
            if (
                fresh_source is None
                or fresh_target is None
                or fresh_config.target_channel_id != message.channel.id
                or (require_enabled and not fresh_config.recruitment_template_enabled)
            ):
                return False
            fresh_config.recruitment_template_channel_id = message.channel.id
            fresh_config.recruitment_template_message_id = message.id
            await fresh_config.save(
                using_db=connection,
                update_fields=[
                    "recruitment_template_channel_id",
                    "recruitment_template_message_id",
                    "updated_at",
                ],
            )
        return True

    async def _persist_room_update(
        self,
        message: Message,
        config_id: int,
        source_channel_id: int,
        room_number: str,
    ) -> tuple[RoomNumberConfig, int] | None:
        if message.guild is None:
            return None
        async with self._state_lock(source_channel_id):
            async with in_transaction() as connection:
                fresh_config = await (
                    RoomNumberConfig.filter(id=config_id)
                    .using_db(connection)
                    .select_for_update()
                    .first()
                )
                if fresh_config is None:
                    return None
                source_membership = await (
                    FeatureChannel.filter(
                        id=fresh_config.feature_channel_id,
                        guild_id=message.guild.id,
                        channel_id=source_channel_id,
                        feature_name=self.feature_name,
                        is_enabled=True,
                    )
                    .using_db(connection)
                    .select_for_update()
                    .first()
                )
                trigger_membership = await (
                    FeatureChannel.filter(
                        guild_id=message.guild.id,
                        channel_id=message.channel.id,
                        feature_name=self.feature_name,
                        is_enabled=True,
                    )
                    .using_db(connection)
                    .select_for_update()
                    .first()
                )
                target_membership = await (
                    FeatureChannel.filter(
                        guild_id=message.guild.id,
                        channel_id=fresh_config.target_channel_id,
                        feature_name=self.feature_name,
                        is_enabled=True,
                    )
                    .using_db(connection)
                    .select_for_update()
                    .first()
                )
                if (
                    source_membership is None
                    or trigger_membership is None
                    or target_membership is None
                    or message.channel.id
                    not in {source_channel_id, fresh_config.target_channel_id}
                ):
                    return None
                fresh_config.room_number = room_number
                await fresh_config.save(
                    using_db=connection,
                    update_fields=["room_number", "updated_at"],
                )
            generation = self._delivery_generations.get(source_channel_id, 0) + 1
            self._delivery_generations[source_channel_id] = generation
        return fresh_config, generation

    async def _fresh_delivery_config(
        self,
        config_id: int,
        guild_id: int,
        source_channel_id: int,
    ) -> RoomNumberConfig | None:
        config_row = await (
            RoomNumberConfig.filter(id=config_id)
            .select_related("feature_channel")
            .first()
        )
        if config_row is None:
            return None
        source_membership = config_row.feature_channel
        if (
            source_membership.guild_id != guild_id
            or source_membership.channel_id != source_channel_id
            or not source_membership.is_enabled
        ):
            return None
        target_membership = await FeatureChannel.get_or_none(
            guild_id=guild_id,
            channel_id=config_row.target_channel_id,
            feature_name=self.feature_name,
            is_enabled=True,
        )
        return config_row if target_membership is not None else None

    async def _live_template_output(
        self,
        guild: Guild,
        config_row: RoomNumberConfig,
        room_number: str,
    ) -> tuple[str | None, tuple[str, ...]]:
        if not config_row.recruitment_template_enabled:
            return None, ()
        if config_row.recruitment_template_message_id is None:
            return None, ()
        channel = guild.get_channel(config_row.recruitment_template_channel_id)
        if not isinstance(channel, TextChannel):
            return RECRUITMENT_TEMPLATE_UNREADABLE, ()
        try:
            template_message = await channel.fetch_message(
                config_row.recruitment_template_message_id
            )
        except DiscordException:
            self.logger.info(
                "Room recruitment template fetch failed. guild=%s channel=%s "
                "message=%s",
                guild.id,
                config_row.recruitment_template_channel_id,
                config_row.recruitment_template_message_id,
            )
            return RECRUITMENT_TEMPLATE_UNREADABLE, ()
        try:
            rendered = render_recruitment_template(
                template_message.content,
                room_number,
            )
        except RoomNumberFormatError:
            return RECRUITMENT_TEMPLATE_UNREADABLE, ()
        return rendered.preview, rendered.intent_urls

    async def _refresh_current_room_template_output(
        self,
        message: Message,
        config_row: RoomNumberConfig,
        actor: UserInfo,
    ) -> bool:
        if message.guild is None:
            return False
        try:
            fresh_config = await self._fresh_delivery_config(
                config_row.id,
                message.guild.id,
                config_row.feature_channel.channel_id,
            )
        except SETTINGS_STORAGE_EXCEPTIONS as exc:
            storage_error = classify_storage_exception(exc)
            if storage_error is None:
                raise
            self.logger.warning(
                "Room template refresh config lookup failed. guild=%s "
                "channel=%s kind=%s",
                message.guild.id,
                config_row.feature_channel.channel_id,
                storage_error.kind.value,
            )
            fresh_config = None
        if (
            fresh_config is None
            or not fresh_config.recruitment_template_enabled
            or fresh_config.room_number is None
        ):
            return False
        target = message.guild.get_channel(fresh_config.target_channel_id)
        if not isinstance(target, TextChannel) or not self._can_send_output(
            message.guild,
            target,
        ):
            return False
        try:
            rendered = render_recruitment_template(
                message.content,
                fresh_config.room_number,
            )
        except RoomNumberFormatError:
            return False
        embed = build_room_output_embed(
            fresh_config.room_number,
            actor,
            template_text=rendered.preview,
        )
        view = build_room_output_view(rendered.intent_urls)
        try:
            await target.send(embed=embed, view=view)
        except Exception:
            self.logger.exception(
                "Failed to send Room template refresh. guild=%s channel=%s",
                message.guild.id,
                target.id,
            )
            return False
        return True

    @staticmethod
    def _can_send_output(guild: Guild, channel: TextChannel) -> bool:
        bot_member = getattr(guild, "me", None)
        if bot_member is None:
            return False
        permissions = channel.permissions_for(bot_member)
        return bool(
            getattr(permissions, "view_channel", False)
            and getattr(permissions, "send_messages", False)
            and getattr(permissions, "embed_links", False)
        )

    def _bot_mention(self, guild: Guild) -> str:
        guild_member = getattr(guild, "me", None)
        if guild_member is not None:
            return guild_member.mention
        return self.bot.user.mention

    def _is_current_generation(
        self,
        source_channel_id: int,
        generation: int,
    ) -> bool:
        return self._delivery_generations.get(source_channel_id) == generation

    async def _finish_processing(
        self,
        message: Message,
        terminal_emojis: tuple[str, ...] = (),
    ) -> None:
        await transition_processing_reaction(
            message,
            terminal_emojis,
            processing_emoji=config.PROCESSING_EMOJI,
            user=self.bot.user,
            log=self.logger,
        )

    async def _edit_output(
        self,
        output_message: Message,
        *,
        embed: object,
        view: object,
    ) -> None:
        try:
            await output_message.edit(embed=embed, view=view)
        except Exception:
            self.logger.exception(
                "Failed to edit operation-local Room output. message=%s",
                getattr(output_message, "id", None),
            )

    async def _invalidate_output(
        self,
        output_message: Message | None,
        embed: object | None,
    ) -> None:
        if output_message is None or embed is None:
            return
        mark_room_output_superseded(embed)
        await self._edit_output(output_message, embed=embed, view=None)

    async def _send_room_storage_failure(
        self,
        message: Message,
        storage_error: StorageError,
    ) -> None:
        reference = generate_error_reference()
        self.logger.warning(
            "Room persistence failed. reference=%s guild=%s channel=%s "
            "message=%s kind=%s",
            reference,
            getattr(message.guild, "id", None),
            getattr(message.channel, "id", None),
            getattr(message, "id", None),
            storage_error.kind.value,
        )
        await self._finish_processing(message)
        try:
            await message.channel.send(embed=build_room_storage_error_embed(reference))
        except Exception:
            self.logger.exception(
                "Failed to send Room storage failure embed. reference=%s",
                reference,
            )

    async def _handle_room_update(
        self,
        message: Message,
        config_row: RoomNumberConfig,
        room_number: str,
        actor: UserInfo,
    ) -> RoomUpdateResult:
        await add_reaction_if_possible(
            message,
            config.PROCESSING_EMOJI,
            log=self.logger,
        )
        source_channel_id = config_row.feature_channel.channel_id
        try:
            persisted = await self._persist_room_update(
                message,
                config_row.id,
                source_channel_id,
                room_number,
            )
        except Exception as exc:
            storage_error = classify_storage_exception(exc)
            if storage_error is None:
                await self._finish_processing(message)
                raise
            await self._send_room_storage_failure(message, storage_error)
            return RoomUpdateResult(
                room_number=room_number,
                persisted=False,
                naming_succeeded=False,
                output_succeeded=False,
                superseded=False,
            )
        if persisted is None:
            await self._finish_processing(message)
            return RoomUpdateResult(
                room_number=room_number,
                persisted=False,
                naming_succeeded=False,
                output_succeeded=False,
                superseded=True,
            )
        fresh_config, generation = persisted
        return await self._deliver_room_update(
            message,
            fresh_config,
            room_number,
            actor,
            source_channel_id,
            generation,
        )

    async def _deliver_room_update(  # noqa: C901, PLR0911, PLR0912, PLR0913, PLR0915
        self,
        message: Message,
        persisted_config: RoomNumberConfig,
        room_number: str,
        actor: UserInfo,
        source_channel_id: int,
        generation: int,
    ) -> RoomUpdateResult:
        guild = message.guild
        if guild is None or not self._is_current_generation(
            source_channel_id,
            generation,
        ):
            await self._finish_processing(message)
            return RoomUpdateResult(
                room_number=room_number,
                persisted=True,
                naming_succeeded=False,
                output_succeeded=False,
                superseded=True,
            )
        config_row = await self._fresh_delivery_config(
            persisted_config.id,
            guild.id,
            source_channel_id,
        )
        if config_row is None or not self._is_current_generation(
            source_channel_id,
            generation,
        ):
            await self._finish_processing(message)
            return RoomUpdateResult(
                room_number=room_number,
                persisted=True,
                naming_succeeded=False,
                output_succeeded=False,
                superseded=True,
            )

        target = guild.get_channel(config_row.target_channel_id)
        target_is_text = isinstance(target, TextChannel)
        desired_name = render_channel_name(
            config_row.channel_name_format,
            room_number,
        )
        rename_required = target_is_text and target.name != desired_name
        template_text, intent_urls = await self._live_template_output(
            guild,
            config_row,
            room_number,
        )
        if not self._is_current_generation(source_channel_id, generation):
            await self._finish_processing(message)
            return RoomUpdateResult(
                room_number=room_number,
                persisted=True,
                naming_succeeded=False,
                output_succeeded=False,
                superseded=True,
            )

        embed = build_room_output_embed(
            room_number,
            actor,
            description=PENDING_RENAME_DESCRIPTION if rename_required else None,
            template_text=template_text,
        )
        view = build_room_output_view(intent_urls)
        output_message = None
        if target_is_text and self._can_send_output(guild, target):
            try:
                output_message = await target.send(embed=embed, view=view)
            except Exception:
                self.logger.exception(
                    "Failed to send Room output. guild=%s channel=%s",
                    guild.id,
                    target.id,
                )
        output_succeeded = output_message is not None
        if not self._is_current_generation(source_channel_id, generation):
            await self._invalidate_output(output_message, embed)
            await self._finish_processing(message)
            return RoomUpdateResult(
                room_number=room_number,
                persisted=True,
                naming_succeeded=False,
                output_succeeded=output_succeeded,
                superseded=True,
            )

        naming_succeeded = False
        if target_is_text:
            async with self._delivery_lock(source_channel_id):
                if not rename_required or target.name == desired_name:
                    naming_succeeded = True
                else:
                    try:
                        await target.edit(name=desired_name)
                    except Exception:
                        self.logger.exception(
                            "Failed to rename Room target. guild=%s channel=%s",
                            guild.id,
                            target.id,
                        )
                    else:
                        naming_succeeded = True
        if not self._is_current_generation(source_channel_id, generation):
            await self._invalidate_output(output_message, embed)
            await self._finish_processing(message)
            return RoomUpdateResult(
                room_number=room_number,
                persisted=True,
                naming_succeeded=naming_succeeded,
                output_succeeded=output_succeeded,
                superseded=True,
            )

        fallback_message = None
        fallback_embed = None
        if not output_succeeded and config_row.target_channel_id != source_channel_id:
            source = guild.get_channel(source_channel_id)
            if isinstance(source, TextChannel) and self._can_send_output(guild, source):
                fallback_embed = build_target_output_failure_embed(
                    room_number,
                    self._bot_mention(guild),
                )
                try:
                    fallback_message = await source.send(embed=fallback_embed)
                except Exception:
                    self.logger.exception(
                        "Failed to send Room source fallback. guild=%s channel=%s",
                        guild.id,
                        source_channel_id,
                    )
        if not self._is_current_generation(source_channel_id, generation):
            await self._invalidate_output(output_message, embed)
            await self._invalidate_output(fallback_message, fallback_embed)
            await self._finish_processing(message)
            return RoomUpdateResult(
                room_number=room_number,
                persisted=True,
                naming_succeeded=naming_succeeded,
                output_succeeded=output_succeeded,
                superseded=True,
            )

        if output_message is not None and rename_required:
            if naming_succeeded:
                mark_room_output_rename_succeeded(embed)
            else:
                mark_room_output_rename_failed(embed, self._bot_mention(guild))
            await self._edit_output(output_message, embed=embed, view=view)
        if not self._is_current_generation(source_channel_id, generation):
            await self._invalidate_output(output_message, embed)
            await self._invalidate_output(fallback_message, fallback_embed)
            await self._finish_processing(message)
            return RoomUpdateResult(
                room_number=room_number,
                persisted=True,
                naming_succeeded=naming_succeeded,
                output_succeeded=output_succeeded,
                superseded=True,
            )

        await self._finish_processing(
            message,
            ("✅",) if naming_succeeded else (),
        )
        if not self._is_current_generation(source_channel_id, generation):
            if naming_succeeded and self.bot.user is not None:
                await remove_reaction_if_present(
                    message,
                    "✅",
                    self.bot.user,
                    log=self.logger,
                )
            await self._invalidate_output(output_message, embed)
            await self._invalidate_output(fallback_message, fallback_embed)
            return RoomUpdateResult(
                room_number=room_number,
                persisted=True,
                naming_succeeded=naming_succeeded,
                output_succeeded=output_succeeded,
                superseded=True,
            )
        return RoomUpdateResult(
            room_number=room_number,
            persisted=True,
            naming_succeeded=naming_succeeded,
            output_succeeded=output_succeeded,
            superseded=False,
        )

    @staticmethod
    def _manual_room_result_message(result: RoomUpdateResult) -> str:
        if not result.persisted:
            return MANUAL_ROOM_PERSISTENCE_FAILURE_MESSAGE
        if result.superseded:
            return MANUAL_ROOM_SUPERSEDED_MESSAGE
        if not result.naming_succeeded:
            return (
                f"部屋番号「{result.room_number}」は保存されましたが、"
                "チャンネル名を更新できませんでした。"
            )
        if not result.output_succeeded:
            return (
                f"部屋番号を「{result.room_number}」に更新しましたが、"
                "募集情報を送信できませんでした。"
            )
        return f"部屋番号を「{result.room_number}」に更新しました。"

    async def cog_unload(self) -> None:
        self.bot.tree.remove_command(
            self.context_menu.name,
            type=AppCommandType.message,
        )
        self.bot.tree.remove_command(
            self.recruitment_template_context_menu.name,
            type=AppCommandType.message,
        )
        self._delivery_generations.clear()
        self._state_lock = KeyAsyncLock()
        self._delivery_lock = KeyAsyncLock()


async def setup(bot: Rhoboto) -> None:
    await bot.add_cog(RoomNumber(bot))
