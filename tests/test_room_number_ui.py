from datetime import UTC, datetime

# ruff: noqa: RUF001
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from discord import ButtonStyle, ChannelType, Embed
from discord.ui import ChannelSelect

from components.ui_room_number import (
    PENDING_RENAME_DESCRIPTION,
    RECRUITMENT_TEMPLATE_MISSING,
    RECRUITMENT_TEMPLATE_UNREADABLE,
    RoomNumberFormatModal,
    RoomNumberSettingsSnapshot,
    RoomNumberSettingsView,
    RoomNumberUIActions,
    build_room_number_settings_panel,
    build_room_output_embed,
    build_room_output_view,
    build_room_storage_error_embed,
    build_target_output_failure_embed,
    mark_room_output_rename_failed,
    mark_room_output_superseded,
    remove_pending_rename_description,
)
from tests.fakes import FakeInteraction
from utils.structs_base import UserInfo

UPDATED_AT = datetime(2026, 7, 15, 12, tzinfo=UTC)


def _actions() -> RoomNumberUIActions:
    return RoomNumberUIActions(
        select_target=AsyncMock(),
        save_channel_name_format=AsyncMock(),
        set_recruitment_template_enabled=AsyncMock(),
    )


def _snapshot(**changes: object) -> RoomNumberSettingsSnapshot:
    values = {
        "source_feature_channel_id": 7,
        "source_channel_id": 111,
        "target_channel_id": 222,
        "config_id": 9,
        "updated_at": UPDATED_AT,
        "room_number": "12345",
        "channel_name_format": "部屋番号【{room_number}】",
        "recruitment_template_enabled": True,
        "recruitment_template_channel_id": 222,
        "recruitment_template_message_id": 333,
    }
    values.update(changes)
    return RoomNumberSettingsSnapshot(**values)


def test_settings_panel_renders_configured_values_and_live_pointer() -> None:
    panel = build_room_number_settings_panel(
        SimpleNamespace(id=100),
        _snapshot(),
        _actions(),
    )

    assert isinstance(panel.embed, Embed)
    assert panel.embed.title == "部屋番号設定"
    assert [(field.name, field.value) for field in panel.embed.fields] == [
        ("Sourceチャンネル", "<#111>"),
        ("Targetチャンネル", "<#222>"),
        ("現在の部屋番号", "12345"),
        ("チャンネル名形式", "部屋番号【{room_number}】"),
        ("募集テンプレ", "🟢 有効"),
        (
            "テンプレ元メッセージ",
            (
                "<#222>・[メッセージを表示]"
                "(https://discord.com/channels/100/222/333)・ID: 333"
            ),
        ),
    ]


@pytest.mark.parametrize(
    ("enabled", "channel_id", "message_id", "status", "pointer"),
    [
        (True, None, None, "🟢 有効（未設定）", "未設定"),
        (False, 444, 555, "⚫ 無効", "<#444>"),
    ],
)
def test_settings_panel_renders_template_unset_disabled_and_old_pointer(
    *,
    enabled: bool,
    channel_id: int | None,
    message_id: int | None,
    status: str,
    pointer: str,
) -> None:
    panel = build_room_number_settings_panel(
        SimpleNamespace(id=100),
        _snapshot(
            recruitment_template_enabled=enabled,
            recruitment_template_channel_id=channel_id,
            recruitment_template_message_id=message_id,
        ),
        _actions(),
    )

    assert panel.embed.fields[4].value == status
    assert pointer in panel.embed.fields[5].value


def test_unconfigured_settings_panel_uses_unset_values_and_target_only() -> None:
    panel = build_room_number_settings_panel(
        SimpleNamespace(id=100),
        _snapshot(
            target_channel_id=None,
            config_id=None,
            updated_at=None,
            room_number=None,
            recruitment_template_channel_id=None,
            recruitment_template_message_id=None,
        ),
        _actions(),
    )

    assert [field.value for field in panel.embed.fields[1:3]] == [
        "未設定",
        "未設定",
    ]
    assert isinstance(panel.view, RoomNumberSettingsView)
    assert len(panel.view.children) == 1


def test_settings_view_uses_text_channel_select_and_exact_controls() -> None:
    view = RoomNumberSettingsView(snapshot=_snapshot(), actions=_actions())
    select = view.children[0]

    assert isinstance(select, ChannelSelect)
    assert select.channel_types == [ChannelType.text]
    assert select.placeholder == "Targetチャンネルを選択"
    assert [value.id for value in select.default_values] == [222]
    assert [item.label for item in view.children[1:]] == [
        "チャンネル名形式を編集",
        "募集テンプレを無効化",
    ]


@pytest.mark.asyncio
async def test_target_select_rechecks_permissions_and_routes_snapshot() -> None:
    actions = _actions()
    view = RoomNumberSettingsView(snapshot=_snapshot(), actions=actions)
    select = view.children[0]
    select._values = [SimpleNamespace(id=444)]  # noqa: SLF001

    denied = FakeInteraction(administrator=False)
    await select.callback(denied)
    actions.select_target.assert_not_awaited()

    allowed = FakeInteraction()
    await select.callback(allowed)
    actions.select_target.assert_awaited_once_with(
        allowed,
        7,
        9,
        UPDATED_AT,
        444,
        view,
    )
    assert allowed.response.deferred == [True]


@pytest.mark.asyncio
async def test_format_modal_rechecks_permissions_and_routes_raw_value() -> None:
    actions = _actions()
    view = RoomNumberSettingsView(snapshot=_snapshot(), actions=actions)
    interaction = FakeInteraction()

    await view.children[1].callback(interaction)
    modal = interaction.response.modals[0]
    assert isinstance(modal, RoomNumberFormatModal)
    assert modal.title == "チャンネル名形式を編集"
    assert modal.channel_name_format.default == "部屋番号【{room_number}】"

    modal.channel_name_format._value = "{{部屋}}-{room_number}"  # noqa: SLF001
    denied = FakeInteraction(manage_channels=False)
    await modal.on_submit(denied)
    actions.save_channel_name_format.assert_not_awaited()

    allowed = FakeInteraction()
    await modal.on_submit(allowed)
    actions.save_channel_name_format.assert_awaited_once_with(
        allowed,
        9,
        UPDATED_AT,
        "{{部屋}}-{room_number}",
        view,
    )


@pytest.mark.asyncio
async def test_template_toggle_rechecks_permissions_and_routes_next_state() -> None:
    actions = _actions()
    view = RoomNumberSettingsView(snapshot=_snapshot(), actions=actions)
    toggle = view.children[2]

    denied = FakeInteraction(administrator=False)
    await toggle.callback(denied)
    actions.set_recruitment_template_enabled.assert_not_awaited()

    allowed = FakeInteraction()
    await toggle.callback(allowed)
    actions.set_recruitment_template_enabled.assert_awaited_once_with(
        allowed,
        9,
        UPDATED_AT,
        False,  # noqa: FBT003
        view,
    )


@pytest.mark.asyncio
async def test_settings_timeout_disables_every_control() -> None:
    view = RoomNumberSettingsView(snapshot=_snapshot(), actions=_actions())

    await view.on_timeout()

    assert all(item.disabled for item in view.children)


def test_room_output_builds_exact_template_surface_and_five_links() -> None:
    timestamp = datetime(2026, 7, 15, 12, tzinfo=UTC)
    urls = tuple(f"https://x.example/{index}" for index in range(5))
    embed = build_room_output_embed(
        "12345",
        UserInfo(username="alice", display_name="Alice"),
        description=PENDING_RENAME_DESCRIPTION,
        template_text="12345\n#プロセカ募集",
        timestamp=timestamp,
    )
    view = build_room_output_view(urls)

    assert embed.title == "部屋番号【12345】"
    assert embed.description == PENDING_RENAME_DESCRIPTION
    assert embed.timestamp == timestamp
    assert embed.footer.text == "部屋番号更新：Alice（@alice）"
    assert [(field.name, field.value) for field in embed.fields] == [
        ("ツイ募テンプレ", "12345\n#プロセカ募集")
    ]
    assert view is not None
    assert [button.label for button in view.children] == ["Xに投稿", "1", "2", "3", "4"]
    assert [button.style for button in view.children] == [ButtonStyle.link] * 5
    assert [button.url for button in view.children] == list(urls)
    assert [button.row for button in view.children] == [0] * 5


def test_room_output_template_states_and_embed_mutations() -> None:
    disabled = build_room_output_embed(
        "12345",
        UserInfo(username="alice", display_name="Alice"),
    )
    assert disabled.fields == []
    assert build_room_output_view(()) is None

    for template_state in (
        RECRUITMENT_TEMPLATE_MISSING,
        RECRUITMENT_TEMPLATE_UNREADABLE,
    ):
        embed = build_room_output_embed(
            "12345",
            UserInfo(username="alice", display_name="Alice"),
            template_text=template_state,
        )
        assert embed.fields[0].value == template_state

    pending = build_room_output_embed(
        "12345",
        UserInfo(username="alice", display_name="Alice"),
        description=PENDING_RENAME_DESCRIPTION,
        template_text="template",
    )
    remove_pending_rename_description(pending)
    assert pending.description is None

    mark_room_output_rename_failed(pending, "<@999>")
    assert "<@999>" in pending.description
    assert "⚠️" not in pending.description

    original_footer = pending.footer.text
    original_timestamp = pending.timestamp
    mark_room_output_superseded(pending)
    assert pending.description == (
        "新しい部屋番号が設定されたため、この募集情報は無効です。"
    )
    assert pending.fields == []
    assert pending.footer.text == original_footer
    assert pending.timestamp == original_timestamp


def test_error_only_embeds_have_exact_copy_and_no_template_fields() -> None:
    fallback = build_target_output_failure_embed("12345", "<@999>")
    assert fallback.title == "部屋番号【12345】"
    assert fallback.description == (
        "部屋番号は保存されましたが、設定された送信先チャンネルを利用できませんでした。\n"
        "設定内容と、<@999> の「チャンネルを見る」"
        "「メッセージを送信」権限を確認してください。"
    )
    assert fallback.fields == []

    storage = build_room_storage_error_embed("ABC123")
    assert storage.title == "部屋番号を更新できませんでした"
    assert storage.description == (
        "時間をおいて、もう一度お試しください。\nエラー参照ID：`ABC123`"
    )
    assert storage.fields == []
