from __future__ import annotations

# ruff: noqa: RUF001
from dataclasses import dataclass
from typing import TYPE_CHECKING

from discord import ButtonStyle, ChannelType, Embed, Interaction, Object, TextStyle
from discord.ui import Button, ChannelSelect, Modal, TextInput, View
from discord.utils import utcnow

from bot import config
from components.ui_permissions import require_settings_permissions
from components.ui_settings_flow import SettingsPanel, SettingsTimeoutView
from utils.room_number import CHANNEL_NAME_FORMAT_MAX_LENGTH

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence
    from datetime import datetime

    from discord import Guild

    from utils.structs_base import UserInfo


PENDING_RENAME_DESCRIPTION = (
    "Discord側のチャンネル名変更回数の制限により、反映まで時間がかかる場合があります。\n"
    "現在、チャンネル名を更新しています。"
)
RENAME_SUCCEEDED_DESCRIPTION = "チャンネル名を更新しました。"
INITIAL_SETUP_CONTENT = (
    "部屋番号機能はまだ設定されていません。下のボタンから設定を開始してください。"
)
INITIAL_SETUP_BUTTON_LABEL = "設定を開始"
INITIAL_SETUP_MODAL_TITLE = "チャンネル名形式を設定"
INITIAL_SOURCE_SELECTION_CONTENT = (
    "チャンネル名形式を受け付けました。Sourceチャンネルを選択してください。"
)
INITIAL_SETUP_STALE_MESSAGE = (
    "設定画面が古くなっています。もう一度設定を開始してください。"
)
UNAUTHORIZED_SETUP_MESSAGE = "この設定画面を開いた管理者のみ操作できます。"
RECRUITMENT_TEMPLATE_UNREADABLE = (
    "募集テンプレを読み込めませんでした。設定を確認してください。"
)
TEMPLATE_CLEAR_CONFIRM_CONTENT = (
    "‼️ ツイ募テンプレの設定を解除しますか？\n"
    "保存済みのテンプレ参照だけを解除します。元のメッセージは削除されません。\n"
    "募集テンプレの自動処理が有効なら、次に有効なテンプレをTargetへ投稿すると自動で再設定されます。"
)
TEMPLATE_CLEAR_PROGRESS_MESSAGE = "ツイ募テンプレの設定を解除しています。"
TEMPLATE_CLEAR_CANCELLED_MESSAGE = "✖️ 解除をキャンセルしました。"
SUPERSEDED_DESCRIPTION = "新しい部屋番号が設定されたため、この募集情報は無効です。"


@dataclass(frozen=True, slots=True)
class RoomNumberSettingsSnapshot:
    source_feature_channel_id: int
    source_channel_id: int
    target_channel_id: int | None
    config_id: int | None
    updated_at: datetime | None
    room_number: str | None
    channel_name_format: str
    recruitment_template_enabled: bool
    recruitment_template_channel_id: int | None
    recruitment_template_message_id: int | None


@dataclass(frozen=True, slots=True)
class RoomNumberUIActions:
    initial_setup_is_current: Callable[[int], Awaitable[bool]]
    start_initial_setup: Callable[
        [Interaction, int, int, str],
        Awaitable[None],
    ]
    select_source: Callable[
        [Interaction, int, int, str, int, View],
        Awaitable[None],
    ]
    select_target: Callable[
        [Interaction, int, int | None, datetime | None, int, View],
        Awaitable[None],
    ]
    save_channel_name_format: Callable[
        [Interaction, int, datetime, str, View],
        Awaitable[None],
    ]
    set_recruitment_template_enabled: Callable[
        [Interaction, int, datetime, bool, View],
        Awaitable[None],
    ]
    clear_recruitment_template: Callable[
        [Interaction, int, datetime, View],
        Awaitable[None],
    ]


class RoomNumberTargetSelect(ChannelSelect):
    def __init__(self, target_channel_id: int | None) -> None:
        super().__init__(
            placeholder="Targetチャンネルを選択",
            min_values=1,
            max_values=1,
            channel_types=[ChannelType.text],
            default_values=(
                [Object(id=target_channel_id)]
                if target_channel_id is not None
                else None
            ),
            row=0,
        )

    async def callback(self, interaction: Interaction) -> None:
        if not await require_settings_permissions(interaction):
            return
        view = self.view
        if not isinstance(view, RoomNumberSettingsView):
            return
        await interaction.response.defer(ephemeral=True)
        await view.actions.select_target(
            interaction,
            view.snapshot.source_feature_channel_id,
            view.snapshot.config_id,
            view.snapshot.updated_at,
            self.values[0].id,
            view,
        )


class RoomNumberInitialSetupView(SettingsTimeoutView):
    def __init__(
        self,
        *,
        requesting_user_id: int,
        target_channel_id: int,
        target_feature_channel_id: int,
        actions: RoomNumberUIActions,
    ) -> None:
        super().__init__()
        self.requesting_user_id = requesting_user_id
        self.target_channel_id = target_channel_id
        self.target_feature_channel_id = target_feature_channel_id
        self.actions = actions
        self.add_item(RoomNumberInitialSetupButton())


class RoomNumberInitialSetupButton(Button):
    def __init__(self) -> None:
        super().__init__(label=INITIAL_SETUP_BUTTON_LABEL, style=ButtonStyle.primary)

    async def callback(self, interaction: Interaction) -> None:
        view = self.view
        if not isinstance(view, RoomNumberInitialSetupView):
            return
        if interaction.user.id != view.requesting_user_id:
            await interaction.response.send_message(
                UNAUTHORIZED_SETUP_MESSAGE,
                ephemeral=True,
            )
            return
        if not await require_settings_permissions(interaction):
            return
        if getattr(
            interaction.channel, "id", None
        ) != view.target_channel_id or not await view.actions.initial_setup_is_current(
            view.target_feature_channel_id
        ):
            await interaction.response.send_message(
                INITIAL_SETUP_STALE_MESSAGE,
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(
            RoomNumberInitialFormatModal(
                target_channel_id=view.target_channel_id,
                target_feature_channel_id=view.target_feature_channel_id,
                actions=view.actions,
            )
        )


class RoomNumberInitialFormatModal(Modal):
    def __init__(
        self,
        *,
        target_channel_id: int,
        target_feature_channel_id: int,
        actions: RoomNumberUIActions,
    ) -> None:
        super().__init__(title=INITIAL_SETUP_MODAL_TITLE)
        self.target_channel_id = target_channel_id
        self.target_feature_channel_id = target_feature_channel_id
        self.actions = actions
        self.channel_name_format = TextInput(
            label="チャンネル名形式",
            default="部屋番号【{room_number}】",
            max_length=CHANNEL_NAME_FORMAT_MAX_LENGTH,
            required=True,
            style=TextStyle.short,
        )
        self.add_item(self.channel_name_format)

    async def on_submit(self, interaction: Interaction) -> None:
        if not await require_settings_permissions(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        await self.actions.start_initial_setup(
            interaction,
            self.target_feature_channel_id,
            self.target_channel_id,
            self.channel_name_format.value,
        )


class RoomNumberSourceSelect(ChannelSelect):
    def __init__(self) -> None:
        super().__init__(
            placeholder="Sourceチャンネルを選択",
            min_values=1,
            max_values=1,
            channel_types=[ChannelType.text],
            row=0,
        )

    async def callback(self, interaction: Interaction) -> None:
        if not await require_settings_permissions(interaction):
            return
        view = self.view
        if not isinstance(view, RoomNumberSourceSelectView):
            return
        if interaction.user.id != view.requesting_user_id:
            await interaction.response.send_message(
                UNAUTHORIZED_SETUP_MESSAGE,
                ephemeral=True,
            )
            return
        if getattr(interaction.channel, "id", None) != view.target_channel_id:
            await interaction.response.send_message(
                INITIAL_SETUP_STALE_MESSAGE,
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True)
        await view.actions.select_source(
            interaction,
            view.target_feature_channel_id,
            view.target_channel_id,
            view.channel_name_format,
            self.values[0].id,
            view,
        )


class RoomNumberSourceSelectView(SettingsTimeoutView):
    def __init__(
        self,
        *,
        requesting_user_id: int,
        target_channel_id: int,
        target_feature_channel_id: int,
        channel_name_format: str,
        actions: RoomNumberUIActions,
    ) -> None:
        super().__init__()
        self.requesting_user_id = requesting_user_id
        self.target_channel_id = target_channel_id
        self.target_feature_channel_id = target_feature_channel_id
        self.channel_name_format = channel_name_format
        self.actions = actions
        self.add_item(RoomNumberSourceSelect())


class EditRoomNumberFormatButton(Button):
    def __init__(self) -> None:
        super().__init__(
            label="チャンネル名形式を編集",
            style=ButtonStyle.secondary,
            row=1,
        )

    async def callback(self, interaction: Interaction) -> None:
        if not await require_settings_permissions(interaction):
            return
        view = self.view
        if not isinstance(view, RoomNumberSettingsView):
            return
        snapshot = view.snapshot
        if snapshot.config_id is None or snapshot.updated_at is None:
            return
        await interaction.response.send_modal(
            RoomNumberFormatModal(
                config_id=snapshot.config_id,
                expected_updated_at=snapshot.updated_at,
                current_format=snapshot.channel_name_format,
                actions=view.actions,
                current_view=view,
            )
        )


class ToggleRecruitmentTemplateButton(Button):
    def __init__(self, *, enabled: bool) -> None:
        super().__init__(
            label=("募集テンプレを無効化" if enabled else "募集テンプレを有効化"),
            style=ButtonStyle.secondary if enabled else ButtonStyle.primary,
            row=1,
        )
        self.enabled = enabled

    async def callback(self, interaction: Interaction) -> None:
        if not await require_settings_permissions(interaction):
            return
        view = self.view
        if not isinstance(view, RoomNumberSettingsView):
            return
        snapshot = view.snapshot
        if snapshot.config_id is None or snapshot.updated_at is None:
            return
        await interaction.response.defer(ephemeral=True)
        await view.actions.set_recruitment_template_enabled(
            interaction,
            snapshot.config_id,
            snapshot.updated_at,
            not self.enabled,
            view,
        )


class ClearRecruitmentTemplateButton(Button):
    def __init__(self) -> None:
        super().__init__(
            label="🗑️ ツイ募テンプレの設定を解除",
            style=ButtonStyle.danger,
            row=1,
        )

    async def callback(self, interaction: Interaction) -> None:
        if not await require_settings_permissions(interaction):
            return
        view = self.view
        if not isinstance(view, RoomNumberSettingsView):
            return
        snapshot = view.snapshot
        if snapshot.config_id is None or snapshot.updated_at is None:
            return
        confirmation_view = ClearRecruitmentTemplateConfirmView(
            config_id=snapshot.config_id,
            expected_updated_at=snapshot.updated_at,
            actions=view.actions,
        )
        confirmation_view.message = view.message
        await interaction.response.edit_message(
            content=TEMPLATE_CLEAR_CONFIRM_CONTENT,
            embed=None,
            view=confirmation_view,
        )


class ClearRecruitmentTemplateConfirmView(SettingsTimeoutView):
    def __init__(
        self,
        *,
        config_id: int,
        expected_updated_at: datetime,
        actions: RoomNumberUIActions,
    ) -> None:
        super().__init__()
        self.config_id = config_id
        self.expected_updated_at = expected_updated_at
        self.actions = actions
        self.add_item(ConfirmClearRecruitmentTemplateButton())
        self.add_item(CancelClearRecruitmentTemplateButton())


class ConfirmClearRecruitmentTemplateButton(Button):
    def __init__(self) -> None:
        super().__init__(label="解除する", style=ButtonStyle.danger)

    async def callback(self, interaction: Interaction) -> None:
        if not await require_settings_permissions(interaction):
            return
        view = self.view
        if not isinstance(view, ClearRecruitmentTemplateConfirmView):
            return
        await interaction.response.edit_message(
            content=TEMPLATE_CLEAR_PROGRESS_MESSAGE,
            view=None,
        )
        await view.actions.clear_recruitment_template(
            interaction,
            view.config_id,
            view.expected_updated_at,
            view,
        )


class CancelClearRecruitmentTemplateButton(Button):
    def __init__(self) -> None:
        super().__init__(label="キャンセル", style=ButtonStyle.secondary)

    async def callback(self, interaction: Interaction) -> None:
        view = self.view
        if not isinstance(view, ClearRecruitmentTemplateConfirmView):
            return
        await interaction.response.edit_message(
            content=TEMPLATE_CLEAR_CANCELLED_MESSAGE,
            view=None,
        )
        view.stop()


class RoomNumberFormatModal(Modal):
    def __init__(
        self,
        *,
        config_id: int,
        expected_updated_at: datetime,
        current_format: str,
        actions: RoomNumberUIActions,
        current_view: View,
    ) -> None:
        super().__init__(title="チャンネル名形式を編集")
        self.config_id = config_id
        self.expected_updated_at = expected_updated_at
        self.actions = actions
        self.current_view = current_view
        self.channel_name_format = TextInput(
            label="チャンネル名形式",
            default=current_format,
            max_length=CHANNEL_NAME_FORMAT_MAX_LENGTH,
            required=True,
            style=TextStyle.short,
        )
        self.add_item(self.channel_name_format)

    async def on_submit(self, interaction: Interaction) -> None:
        if not await require_settings_permissions(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        await self.actions.save_channel_name_format(
            interaction,
            self.config_id,
            self.expected_updated_at,
            self.channel_name_format.value,
            self.current_view,
        )


class RoomNumberSettingsView(SettingsTimeoutView):
    def __init__(
        self,
        *,
        snapshot: RoomNumberSettingsSnapshot,
        actions: RoomNumberUIActions,
    ) -> None:
        super().__init__()
        self.snapshot = snapshot
        self.actions = actions
        self.add_item(RoomNumberTargetSelect(snapshot.target_channel_id))
        if snapshot.config_id is not None and snapshot.updated_at is not None:
            self.add_item(EditRoomNumberFormatButton())
            self.add_item(
                ToggleRecruitmentTemplateButton(
                    enabled=snapshot.recruitment_template_enabled
                )
            )
            if (
                snapshot.recruitment_template_channel_id is not None
                and snapshot.recruitment_template_message_id is not None
            ):
                self.add_item(ClearRecruitmentTemplateButton())


def _template_status(snapshot: RoomNumberSettingsSnapshot) -> str:
    if not snapshot.recruitment_template_enabled:
        return "⚫ 無効"
    if snapshot.recruitment_template_message_id is None:
        return "🟢 有効（未設定）"
    return "🟢 有効"


def _template_pointer(guild_id: int, snapshot: RoomNumberSettingsSnapshot) -> str:
    channel_id = snapshot.recruitment_template_channel_id
    message_id = snapshot.recruitment_template_message_id
    if channel_id is None or message_id is None:
        return "未設定"
    jump_url = f"https://discord.com/channels/{guild_id}/{channel_id}/{message_id}"
    return f"<#{channel_id}>・[メッセージを表示]({jump_url})・ID: {message_id}"


def build_room_number_settings_panel(
    guild: Guild,
    snapshot: RoomNumberSettingsSnapshot,
    actions: RoomNumberUIActions,
) -> SettingsPanel:
    embed = Embed(title="部屋番号設定")
    fields = (
        ("Sourceチャンネル", f"<#{snapshot.source_channel_id}>"),
        (
            "Targetチャンネル",
            (
                f"<#{snapshot.target_channel_id}>"
                if snapshot.target_channel_id is not None
                else "未設定"
            ),
        ),
        ("現在の部屋番号", snapshot.room_number or "未設定"),
        ("チャンネル名形式", snapshot.channel_name_format),
        ("募集テンプレ", _template_status(snapshot)),
        ("テンプレ元メッセージ", _template_pointer(guild.id, snapshot)),
    )
    for name, value in fields:
        embed.add_field(name=name, value=value, inline=False)
    return SettingsPanel(
        embed=embed,
        view=RoomNumberSettingsView(snapshot=snapshot, actions=actions),
    )


def build_room_output_embed(
    room_number: str,
    actor: UserInfo,
    *,
    description: str | None = None,
    template_text: str | None = None,
    timestamp: datetime | None = None,
) -> Embed:
    embed = Embed(
        title=f"部屋番号【{room_number}】",
        description=description,
        color=config.DEFAULT_EMBED_COLOR,
        timestamp=timestamp or utcnow(),
    )
    if template_text is not None:
        embed.add_field(name="ツイ募テンプレ", value=template_text, inline=False)
    embed.set_footer(text=f"更新者：{actor.display_name}（@{actor.username}）")
    return embed


def build_room_output_view(intent_urls: Sequence[str]) -> View | None:
    if not intent_urls:
        return None
    view = View(timeout=None)
    for label, url in zip(
        ("Xに投稿", "@1", "@2", "@3", "@4"), intent_urls, strict=True
    ):
        view.add_item(Button(label=label, style=ButtonStyle.link, url=url, row=0))
    return view


def mark_room_output_rename_succeeded(embed: Embed) -> None:
    embed.description = RENAME_SUCCEEDED_DESCRIPTION


def mark_room_output_rename_failed(embed: Embed, bot_mention: str) -> None:
    embed.description = (
        "チャンネル名を更新できませんでした。\n"
        f"設定されたチャンネルと、{bot_mention} の"
        "「チャンネルの管理」権限を確認してください。"
    )


def mark_room_output_superseded(embed: Embed) -> None:
    embed.description = SUPERSEDED_DESCRIPTION
    embed.clear_fields()


def build_target_output_failure_embed(room_number: str, bot_mention: str) -> Embed:
    return Embed(
        title=f"部屋番号【{room_number}】",
        description=(
            "部屋番号は保存されましたが、設定された送信先チャンネルを利用できませんでした。\n"
            f"設定内容と、{bot_mention} の「チャンネルを見る」"
            "「メッセージを送信」権限を確認してください。"
        ),
    )


def build_room_storage_error_embed(reference_id: str) -> Embed:
    return Embed(
        title="部屋番号を更新できませんでした",
        description=(
            f"時間をおいて、もう一度お試しください。\nエラー参照ID：`{reference_id}`"
        ),
    )
