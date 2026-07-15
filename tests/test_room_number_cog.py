from __future__ import annotations

import asyncio
import logging
import re
from contextlib import asynccontextmanager
from datetime import timedelta
from types import SimpleNamespace
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlsplit

import pytest
from discord import AppCommandType, DiscordException

from bot import config
from cogs import room_number
from cogs.base.feature_channel_base import FeatureNotEnabled
from cogs.room_number import RoomNumber
from components.ui_permissions import MISSING_SETTINGS_PERMISSION_MESSAGE
from components.ui_settings_flow import SettingsTimeoutView
from models.feature_channel import FeatureChannel
from models.room_number import RoomNumberConfig
from tests.fakes import FakeInteraction
from utils.db import close_db, init_db
from utils.storage_errors import StorageError, StorageErrorKind
from utils.structs_base import UserInfo

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


class RecordingTree:
    def __init__(self) -> None:
        self.added: list[object] = []
        self.removed: list[tuple[str, object | None]] = []

    def add_command(self, command: object) -> None:
        self.added.append(command)

    def remove_command(
        self,
        name: str,
        *,
        type: object | None = None,  # noqa: A002
    ) -> None:
        self.removed.append((name, type))


def _bot() -> SimpleNamespace:
    return SimpleNamespace(
        tree=RecordingTree(),
        user=SimpleNamespace(id=999, mention="<@999>"),
        cogs={},
    )


class FakeTextChannel:
    def __init__(
        self,
        channel_id: int,
        *,
        name: str = "room",
        permissions: dict[str, bool] | None = None,
        events: list[tuple[object, ...]] | None = None,
    ) -> None:
        self.id = channel_id
        self.name = name
        self.mention = f"<#{channel_id}>"
        self.guild = None
        self.edited_names: list[str] = []
        self.edit_error: Exception | None = None
        self.send_error: Exception | None = None
        self.fetch_error: Exception | None = None
        self.sent_messages: list[FakeSentMessage] = []
        self.fetch_messages: dict[int, FakeRoomMessage] = {}
        self.fetched_message_ids: list[int] = []
        self.events = events
        self.rename_started: asyncio.Event | None = None
        self.rename_release: asyncio.Event | None = None
        values = {
            "view_channel": True,
            "send_messages": True,
            "embed_links": True,
            "read_message_history": True,
            "manage_channels": True,
        }
        values.update(permissions or {})
        self._permissions = SimpleNamespace(**values)

    def permissions_for(self, member: object) -> SimpleNamespace:
        del member
        return self._permissions

    async def edit(self, *, name: str) -> None:
        if self.rename_started is not None:
            self.rename_started.set()
        if self.rename_release is not None:
            await self.rename_release.wait()
        if self.edit_error is not None:
            raise self.edit_error
        self.name = name
        self.edited_names.append(name)
        if self.events is not None:
            self.events.append(("rename", self.id, name))

    async def send(self, **kwargs: object) -> FakeSentMessage:
        if self.send_error is not None:
            raise self.send_error
        sent = FakeSentMessage(
            message_id=9000 + len(self.sent_messages),
            events=self.events,
            **kwargs,
        )
        self.sent_messages.append(sent)
        if self.events is not None:
            self.events.append(("send", self.id))
        return sent

    async def fetch_message(self, message_id: int) -> FakeRoomMessage:
        self.fetched_message_ids.append(message_id)
        if self.fetch_error is not None:
            raise self.fetch_error
        return self.fetch_messages[message_id]


class FakeSentMessage:
    def __init__(
        self,
        *,
        message_id: int,
        events: list[tuple[object, ...]] | None,
        embed: object | None = None,
        view: object | None = None,
        **kwargs: object,
    ) -> None:
        del kwargs
        self.id = message_id
        self.embed = embed
        self.view = view
        self.edits: list[dict[str, object]] = []
        self.edit_error: Exception | None = None
        self.events = events

    async def edit(self, **kwargs: object) -> None:
        if self.edit_error is not None:
            raise self.edit_error
        self.edits.append(kwargs)
        if "embed" in kwargs:
            self.embed = kwargs["embed"]
        if "view" in kwargs:
            self.view = kwargs["view"]
        if self.events is not None:
            self.events.append(("output_edit", self.id))


class FakeRoomMessage:
    def __init__(  # noqa: PLR0913
        self,
        content: str,
        channel: FakeTextChannel,
        *,
        message_id: int = 5001,
        author_bot: bool = False,
        administrator: bool = False,
        manage_channels: bool = False,
        author_name: str = "alice",
        display_name: str = "Alice",
    ) -> None:
        self.id = message_id
        self.content = content
        self.channel = channel
        self.guild = channel.guild
        self.author = SimpleNamespace(
            bot=author_bot,
            name=author_name,
            display_name=display_name,
            guild_permissions=SimpleNamespace(
                administrator=administrator,
                manage_channels=manage_channels,
            ),
        )
        self.added_reactions: list[str] = []
        self.removed_reactions: list[tuple[str, object]] = []

    async def add_reaction(self, emoji: str) -> None:
        self.added_reactions.append(emoji)
        if self.channel.events is not None:
            self.channel.events.append(("reaction_add", self.id, emoji))

    async def remove_reaction(self, emoji: str, user: object) -> None:
        self.removed_reactions.append((emoji, user))
        if self.channel.events is not None:
            self.channel.events.append(("reaction_remove", self.id, emoji))


class FakeGuild:
    def __init__(self, channels: list[object]) -> None:
        self.id = 1001
        self.me = SimpleNamespace(id=999, mention="<@999>")
        self._channels = {channel.id: channel for channel in channels}
        for channel in channels:
            channel.guild = self

    def get_channel(self, channel_id: int) -> object | None:
        return self._channels.get(channel_id)


def _interaction(
    guild: FakeGuild,
    channel: object,
    *,
    administrator: bool = True,
    manage_channels: bool = True,
) -> FakeInteraction:
    interaction = FakeInteraction(
        guild=guild,
        administrator=administrator,
        manage_channels=manage_channels,
    )
    interaction.channel = channel
    return interaction


@asynccontextmanager
async def _database() -> AsyncIterator[None]:
    db_url = "sqlite://:memory:"
    await asyncio.wait_for(init_db(db_url), timeout=3)
    try:
        yield
    finally:
        await asyncio.wait_for(close_db(db_url), timeout=3)


async def _create_room_config(  # noqa: PLR0913
    guild: FakeGuild,
    source: FakeTextChannel,
    target: FakeTextChannel,
    *,
    room: str | None = None,
    template_enabled: bool = True,
    template_channel_id: int | None = None,
    template_message_id: int | None = None,
) -> RoomNumberConfig:
    source_membership = await FeatureChannel.create(
        guild_id=guild.id,
        channel_id=source.id,
        feature_name="room_number",
        is_enabled=True,
    )
    if target.id != source.id:
        await FeatureChannel.create(
            guild_id=guild.id,
            channel_id=target.id,
            feature_name="room_number",
            is_enabled=True,
        )
    return await RoomNumberConfig.create(
        feature_channel=source_membership,
        target_channel_id=target.id,
        room_number=room,
        recruitment_template_enabled=template_enabled,
        recruitment_template_channel_id=template_channel_id,
        recruitment_template_message_id=template_message_id,
    )


def test_room_number_cog_registers_narrow_command_and_context_menu_surface() -> None:
    bot = _bot()
    cog = RoomNumber(bot)

    assert RoomNumber.feature_name == "room_number"
    assert RoomNumber.feature_display_name == "Room Number"
    assert {command.name for command in RoomNumber.__cog_app_commands__} == {
        "enable",
        "settings",
        "disable",
        "disable_and_clear",
    }
    assert cog.context_menu.name == "部屋番号を設定"
    assert cog.recruitment_template_context_menu.name == "募集テンプレに設定"
    assert bot.tree.added == [
        cog.context_menu,
        cog.recruitment_template_context_menu,
    ]


@pytest.mark.asyncio
async def test_cog_unload_removes_both_context_menus_and_transient_state() -> None:
    bot = _bot()
    cog = RoomNumber(bot)
    cog._delivery_generations[111] = 3  # noqa: SLF001
    state_lock = cog._state_lock  # noqa: SLF001
    delivery_lock = cog._delivery_lock  # noqa: SLF001

    await cog.cog_unload()

    assert bot.tree.removed == [
        ("部屋番号を設定", AppCommandType.message),
        ("募集テンプレに設定", AppCommandType.message),
    ]
    assert cog._delivery_generations == {}  # noqa: SLF001
    assert cog._state_lock is not state_lock  # noqa: SLF001
    assert cog._delivery_lock is not delivery_lock  # noqa: SLF001


@pytest.mark.asyncio
async def test_initial_target_selection_configures_distinct_memberships_and_relinks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(room_number, "TextChannel", FakeTextChannel, raising=False)
    source = FakeTextChannel(111)
    first_target = FakeTextChannel(222)
    second_target = FakeTextChannel(333)
    guild = FakeGuild([source, first_target, second_target])
    bot = _bot()
    bot.user = guild.me
    cog = RoomNumber(bot)

    async with _database():
        await cog._enable_channel(guild.id, source.id)  # noqa: SLF001
        source_membership = await FeatureChannel.get(channel_id=source.id)
        assert await RoomNumberConfig.all().count() == 0

        setup_interaction = _interaction(guild, source)
        await setup_interaction.response.defer(ephemeral=True)
        await cog._select_target(  # noqa: SLF001
            setup_interaction,
            source_membership.id,
            None,
            None,
            first_target.id,
            SettingsTimeoutView(),
        )

        config = await RoomNumberConfig.get(feature_channel_id=source_membership.id)
        assert config.target_channel_id == first_target.id
        assert config.room_number is None
        assert config.channel_name_format == "部屋番号【{room_number}】"
        assert config.recruitment_template_enabled is True
        assert sorted(
            await FeatureChannel.filter(feature_name="room_number").values_list(
                "channel_id",
                flat=True,
            )
        ) == [source.id, first_target.id]
        assert cog._delivery_generations[source.id] == 1  # noqa: SLF001

        config.room_number = "12345"
        config.recruitment_template_channel_id = first_target.id
        config.recruitment_template_message_id = 444
        await config.save(
            update_fields=[
                "room_number",
                "recruitment_template_channel_id",
                "recruitment_template_message_id",
                "updated_at",
            ]
        )
        await config.refresh_from_db()

        relink_interaction = _interaction(guild, source)
        await relink_interaction.response.defer(ephemeral=True)
        await cog._select_target(  # noqa: SLF001
            relink_interaction,
            source_membership.id,
            config.id,
            config.updated_at,
            second_target.id,
            SettingsTimeoutView(),
        )

        await config.refresh_from_db()
        assert config.target_channel_id == second_target.id
        assert config.room_number == "12345"
        assert config.recruitment_template_channel_id == first_target.id
        assert config.recruitment_template_message_id == 444
        assert first_target.id not in await FeatureChannel.all().values_list(
            "channel_id",
            flat=True,
        )
        assert second_target.edited_names == ["部屋番号【12345】"]
        assert cog._delivery_generations[source.id] == 2  # noqa: SLF001


@pytest.mark.asyncio
async def test_self_target_deduplicates_membership_and_pair_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(room_number, "TextChannel", FakeTextChannel, raising=False)
    source = FakeTextChannel(111)
    guild = FakeGuild([source])
    bot = _bot()
    bot.user = guild.me
    cog = RoomNumber(bot)

    async with _database():
        await cog._enable_channel(guild.id, source.id)  # noqa: SLF001
        membership = await FeatureChannel.get(channel_id=source.id)
        interaction = _interaction(guild, source)
        await interaction.response.defer(ephemeral=True)
        await cog._select_target(  # noqa: SLF001
            interaction,
            membership.id,
            None,
            None,
            source.id,
            SettingsTimeoutView(),
        )

        assert await FeatureChannel.all().count() == 1
        config = await RoomNumberConfig.get()
        assert config.target_channel_id == source.id

        assert await cog._disable_channel(guild.id, source.id) is True  # noqa: SLF001
        await membership.refresh_from_db()
        assert membership.is_enabled is False
        assert await RoomNumberConfig.all().count() == 1

        await cog._enable_channel(guild.id, source.id)  # noqa: SLF001
        await membership.refresh_from_db()
        assert membership.is_enabled is True

        await cog._clear_feature_settings(guild.id, source.id)  # noqa: SLF001
        assert await FeatureChannel.all().count() == 0
        assert await RoomNumberConfig.all().count() == 0


@pytest.mark.asyncio
async def test_target_selection_rejects_permissions_conflicts_and_stale_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(room_number, "TextChannel", FakeTextChannel, raising=False)
    source = FakeTextChannel(111)
    missing_permissions = FakeTextChannel(
        222,
        permissions={"embed_links": False, "manage_channels": False},
    )
    occupied_source = FakeTextChannel(333)
    guild = FakeGuild([source, missing_permissions, occupied_source])
    bot = _bot()
    bot.user = guild.me
    cog = RoomNumber(bot)

    async with _database():
        await cog._enable_channel(guild.id, source.id)  # noqa: SLF001
        await cog._enable_channel(guild.id, occupied_source.id)  # noqa: SLF001
        membership = await FeatureChannel.get(channel_id=source.id)

        denied = _interaction(guild, source)
        await denied.response.defer(ephemeral=True)
        await cog._select_target(  # noqa: SLF001
            denied,
            membership.id,
            None,
            None,
            missing_permissions.id,
            SettingsTimeoutView(),
        )
        assert await RoomNumberConfig.all().count() == 0
        assert "埋め込みリンク" in denied.followup.messages[0][0]
        assert "チャンネルの管理" in denied.followup.messages[0][0]

        conflict = _interaction(guild, source)
        await conflict.response.defer(ephemeral=True)
        await cog._select_target(  # noqa: SLF001
            conflict,
            membership.id,
            None,
            None,
            occupied_source.id,
            SettingsTimeoutView(),
        )
        assert await RoomNumberConfig.all().count() == 0
        assert "別の部屋番号設定" in conflict.followup.messages[0][0]

        valid_target = FakeTextChannel(444)
        valid_target.guild = guild
        guild._channels[valid_target.id] = valid_target  # noqa: SLF001
        configured = _interaction(guild, source)
        await configured.response.defer(ephemeral=True)
        await cog._select_target(  # noqa: SLF001
            configured,
            membership.id,
            None,
            None,
            valid_target.id,
            SettingsTimeoutView(),
        )
        config = await RoomNumberConfig.get()

        stale = _interaction(guild, source)
        await stale.response.defer(ephemeral=True)
        await cog._select_target(  # noqa: SLF001
            stale,
            membership.id,
            config.id,
            config.updated_at - timedelta(seconds=1),
            source.id,
            SettingsTimeoutView(),
        )
        await config.refresh_from_db()
        assert config.target_channel_id == valid_target.id
        assert "開き直" in stale.followup.messages[0][0]


@pytest.mark.asyncio
async def test_format_toggle_permissions_and_target_only_lifecycle_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(room_number, "TextChannel", FakeTextChannel, raising=False)
    source = FakeTextChannel(111)
    target = FakeTextChannel(222)
    guild = FakeGuild([source, target])
    bot = _bot()
    bot.user = guild.me
    cog = RoomNumber(bot)

    async with _database():
        await cog._enable_channel(guild.id, source.id)  # noqa: SLF001
        membership = await FeatureChannel.get(channel_id=source.id)
        interaction = _interaction(guild, source)
        await interaction.response.defer(ephemeral=True)
        await cog._select_target(  # noqa: SLF001
            interaction,
            membership.id,
            None,
            None,
            target.id,
            SettingsTimeoutView(),
        )
        config = await RoomNumberConfig.get()
        config.room_number = "12345"
        config.recruitment_template_channel_id = target.id
        config.recruitment_template_message_id = 333
        await config.save(
            update_fields=[
                "room_number",
                "recruitment_template_channel_id",
                "recruitment_template_message_id",
                "updated_at",
            ]
        )
        await config.refresh_from_db()

        denied = _interaction(guild, source, administrator=False)
        await cog._set_recruitment_template_enabled(  # noqa: SLF001
            denied,
            config.id,
            config.updated_at,
            False,  # noqa: FBT003
            SettingsTimeoutView(),
        )
        await config.refresh_from_db()
        assert config.recruitment_template_enabled is True

        format_interaction = _interaction(guild, source)
        await format_interaction.response.defer(ephemeral=True)
        await cog._save_channel_name_format(  # noqa: SLF001
            format_interaction,
            config.id,
            config.updated_at,
            "{{部屋}}-{room_number}",
            SettingsTimeoutView(),
        )
        await config.refresh_from_db()
        assert config.channel_name_format == "{{部屋}}-{room_number}"
        assert target.edited_names == ["{部屋}-12345"]
        assert cog._delivery_generations[source.id] == 2  # noqa: SLF001

        toggle_interaction = _interaction(guild, source)
        await toggle_interaction.response.defer(ephemeral=True)
        await cog._set_recruitment_template_enabled(  # noqa: SLF001
            toggle_interaction,
            config.id,
            config.updated_at,
            False,  # noqa: FBT003
            SettingsTimeoutView(),
        )
        await config.refresh_from_db()
        assert config.recruitment_template_enabled is False
        assert config.recruitment_template_message_id == 333
        assert cog._delivery_generations[source.id] == 2  # noqa: SLF001

        target_source = SimpleNamespace(guild=guild, channel=target)
        with pytest.raises(FeatureNotEnabled):
            await cog._validate_lifecycle_owner(target_source)  # noqa: SLF001


@pytest.mark.asyncio
async def test_enable_rejects_non_text_source_before_persistence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(room_number, "TextChannel", FakeTextChannel, raising=False)
    unsupported = SimpleNamespace(id=111)
    guild = FakeGuild([])
    unsupported.guild = guild
    interaction = _interaction(guild, unsupported)
    cog = RoomNumber(_bot())

    async with _database():
        await RoomNumber.enable.callback(cog, interaction)

        assert await FeatureChannel.all().count() == 0
        assert interaction.response.messages == [
            (
                room_number.INVALID_SOURCE_CHANNEL_MESSAGE,
                {"ephemeral": True},
            )
        ]


@pytest.mark.asyncio
async def test_foreign_setup_snapshot_cannot_configure_another_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(room_number, "TextChannel", FakeTextChannel, raising=False)
    first_source = FakeTextChannel(111)
    second_source = FakeTextChannel(222)
    target = FakeTextChannel(333)
    guild = FakeGuild([first_source, second_source, target])
    cog = RoomNumber(_bot())

    async with _database():
        await cog._enable_channel(guild.id, first_source.id)  # noqa: SLF001
        await cog._enable_channel(guild.id, second_source.id)  # noqa: SLF001
        foreign_membership = await FeatureChannel.get(channel_id=second_source.id)
        interaction = _interaction(guild, first_source)
        await interaction.response.defer(ephemeral=True)

        await cog._select_target(  # noqa: SLF001
            interaction,
            foreign_membership.id,
            None,
            None,
            target.id,
            SettingsTimeoutView(),
        )

        assert await RoomNumberConfig.all().count() == 0
        assert "開き直" in interaction.followup.messages[0][0]


@pytest.mark.asyncio
async def test_format_save_skips_unchanged_name_and_keeps_save_on_rename_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(room_number, "TextChannel", FakeTextChannel, raising=False)
    source = FakeTextChannel(111)
    target = FakeTextChannel(222, name="部屋番号【12345】")
    guild = FakeGuild([source, target])
    bot = _bot()
    bot.user = guild.me
    cog = RoomNumber(bot)

    async with _database():
        await cog._enable_channel(guild.id, source.id)  # noqa: SLF001
        membership = await FeatureChannel.get(channel_id=source.id)
        setup = _interaction(guild, source)
        await setup.response.defer(ephemeral=True)
        await cog._select_target(  # noqa: SLF001
            setup,
            membership.id,
            None,
            None,
            target.id,
            SettingsTimeoutView(),
        )
        config = await RoomNumberConfig.get()
        config.room_number = "12345"
        await config.save(update_fields=["room_number", "updated_at"])
        await config.refresh_from_db()

        unchanged = _interaction(guild, source)
        await unchanged.response.defer(ephemeral=True)
        await cog._save_channel_name_format(  # noqa: SLF001
            unchanged,
            config.id,
            config.updated_at,
            "部屋番号【{room_number}】",
            SettingsTimeoutView(),
        )
        assert target.edited_names == []

        await config.refresh_from_db()
        target.edit_error = DiscordException("rename unavailable")
        failed = _interaction(guild, source)
        await failed.response.defer(ephemeral=True)
        await cog._save_channel_name_format(  # noqa: SLF001
            failed,
            config.id,
            config.updated_at,
            "room-{room_number}",
            SettingsTimeoutView(),
        )

        await config.refresh_from_db()
        assert config.channel_name_format == "room-{room_number}"
        assert failed.followup.messages[0][0] == (
            room_number.RENAME_PARTIAL_SUCCESS_MESSAGE
        )


@pytest.mark.asyncio
async def test_distinct_pair_disable_reenable_and_clear_together(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(room_number, "TextChannel", FakeTextChannel, raising=False)
    source = FakeTextChannel(111)
    target = FakeTextChannel(222)
    guild = FakeGuild([source, target])
    cog = RoomNumber(_bot())

    async with _database():
        await cog._enable_channel(guild.id, source.id)  # noqa: SLF001
        source_membership = await FeatureChannel.get(channel_id=source.id)
        setup = _interaction(guild, source)
        await setup.response.defer(ephemeral=True)
        await cog._select_target(  # noqa: SLF001
            setup,
            source_membership.id,
            None,
            None,
            target.id,
            SettingsTimeoutView(),
        )
        config = await RoomNumberConfig.get()
        config.room_number = "12345"
        config.recruitment_template_channel_id = target.id
        config.recruitment_template_message_id = 444
        await config.save(
            update_fields=[
                "room_number",
                "recruitment_template_channel_id",
                "recruitment_template_message_id",
                "updated_at",
            ]
        )

        assert await cog._disable_channel(guild.id, source.id) is True  # noqa: SLF001
        assert await FeatureChannel.filter(is_enabled=True).count() == 0
        await config.refresh_from_db()
        assert config.recruitment_template_message_id == 444

        await cog._enable_channel(guild.id, source.id)  # noqa: SLF001
        assert await FeatureChannel.filter(is_enabled=True).count() == 2

        await cog._clear_feature_settings(guild.id, source.id)  # noqa: SLF001
        assert await FeatureChannel.all().count() == 0
        assert await RoomNumberConfig.all().count() == 0


@pytest.mark.parametrize("trigger_role", ["source", "target"])
@pytest.mark.asyncio
async def test_automatic_room_capture_accepts_source_or_current_target(
    monkeypatch: pytest.MonkeyPatch,
    trigger_role: str,
) -> None:
    monkeypatch.setattr(room_number, "TextChannel", FakeTextChannel, raising=False)
    source = FakeTextChannel(111)
    target = FakeTextChannel(222)
    guild = FakeGuild([source, target])
    bot = _bot()
    bot.user = guild.me
    cog = RoomNumber(bot)

    async with _database():
        config_row = await _create_room_config(guild, source, target)
        trigger = source if trigger_role == "source" else target
        message = FakeRoomMessage(
            "１２３４５",  # noqa: RUF001
            trigger,
            author_name="alice",
            display_name="Alice",
        )

        await cog.on_message(message)

        await config_row.refresh_from_db()
        assert config_row.room_number == "12345"
        assert target.name == "部屋番号【12345】"
        assert len(target.sent_messages) == 1
        assert target.sent_messages[0].embed.footer.text == (
            "部屋番号更新：Alice（@alice）"  # noqa: RUF001
        )
        assert message.added_reactions == [config.PROCESSING_EMOJI, "✅"]
        assert message.removed_reactions == [(config.PROCESSING_EMOJI, guild.me)]


@pytest.mark.asyncio
async def test_self_target_room_message_is_processed_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(room_number, "TextChannel", FakeTextChannel, raising=False)
    channel = FakeTextChannel(111)
    guild = FakeGuild([channel])
    bot = _bot()
    bot.user = guild.me
    cog = RoomNumber(bot)

    async with _database():
        config_row = await _create_room_config(guild, channel, channel)
        message = FakeRoomMessage("12345", channel)

        await cog.on_message(message)

        await config_row.refresh_from_db()
        assert config_row.room_number == "12345"
        assert len(channel.sent_messages) == 1
        assert message.added_reactions.count(config.PROCESSING_EMOJI) == 1


@pytest.mark.asyncio
async def test_room_listener_silently_ignores_nonmatches_and_missing_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(room_number, "TextChannel", FakeTextChannel, raising=False)
    source = FakeTextChannel(111)
    target = FakeTextChannel(222)
    unconfigured = FakeTextChannel(333)
    guild = FakeGuild([source, target, unconfigured])
    cog = RoomNumber(_bot())

    async with _database():
        config_row = await _create_room_config(guild, source, target)
        await FeatureChannel.create(
            guild_id=guild.id,
            channel_id=unconfigured.id,
            feature_name="room_number",
        )
        nonmatch = FakeRoomMessage("部屋番号【12345】", source)
        missing = FakeRoomMessage("12345", unconfigured, message_id=5002)
        bot_message = FakeRoomMessage(
            "12345",
            source,
            message_id=5003,
            author_bot=True,
        )

        await cog.on_message(nonmatch)
        await cog.on_message(missing)
        await cog.on_message(bot_message)

        await config_row.refresh_from_db()
        assert config_row.room_number is None
        assert target.sent_messages == []
        assert nonmatch.added_reactions == []
        assert missing.added_reactions == []
        assert bot_message.added_reactions == []


@pytest.mark.asyncio
async def test_manual_room_menu_rechecks_permissions_and_reports_invalid_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(room_number, "TextChannel", FakeTextChannel, raising=False)
    source = FakeTextChannel(111)
    guild = FakeGuild([source])
    cog = RoomNumber(_bot())

    async with _database():
        await _create_room_config(guild, source, source)
        selected = FakeRoomMessage("room 12345", source)
        denied = _interaction(guild, source, administrator=False)

        await cog.upsert_from_content_menu(denied, selected)

        assert denied.response.messages[0][0] == MISSING_SETTINGS_PERMISSION_MESSAGE
        assert selected.added_reactions == []

        allowed = _interaction(guild, source)
        await cog.upsert_from_content_menu(allowed, selected)

        assert allowed.response.deferred == [True]
        assert allowed.followup.messages == [
            (
                "メッセージ全体を5〜6桁の数字だけにしてください。",
                {"ephemeral": True},
            )
        ]
        assert selected.added_reactions == []


@pytest.mark.asyncio
async def test_manual_room_menu_reports_enabled_but_unconfigured_channel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(room_number, "TextChannel", FakeTextChannel, raising=False)
    source = FakeTextChannel(111)
    guild = FakeGuild([source])
    cog = RoomNumber(_bot())

    async with _database():
        await FeatureChannel.create(
            guild_id=guild.id,
            channel_id=source.id,
            feature_name="room_number",
        )
        selected = FakeRoomMessage("12345", source)
        interaction = _interaction(guild, source)

        await cog.upsert_from_content_menu(interaction, selected)

        assert interaction.response.deferred == [True]
        assert interaction.followup.messages == [
            (room_number.MANUAL_ROOM_CHANNEL_MESSAGE, {"ephemeral": True})
        ]
        assert selected.added_reactions == []


@pytest.mark.asyncio
async def test_manual_room_lookup_storage_failure_is_ephemeral(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(room_number, "TextChannel", FakeTextChannel, raising=False)
    source = FakeTextChannel(111)
    guild = FakeGuild([source])
    cog = RoomNumber(_bot())

    async def fail_lookup(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise StorageError(StorageErrorKind.DATABASE_UNAVAILABLE)

    monkeypatch.setattr(cog, "_enabled_config_for_channel", fail_lookup)
    selected = FakeRoomMessage("12345", source)
    interaction = _interaction(guild, source)

    await cog.upsert_from_content_menu(interaction, selected)

    assert interaction.response.deferred == [True]
    content, kwargs = interaction.followup.messages[0]
    assert "Reference: `STG-" in content
    assert kwargs == {"ephemeral": True}
    assert selected.added_reactions == []


@pytest.mark.parametrize("trigger_role", ["source", "target"])
@pytest.mark.asyncio
async def test_manual_room_menu_accepts_both_roles_and_attributes_invoker(
    monkeypatch: pytest.MonkeyPatch,
    trigger_role: str,
) -> None:
    monkeypatch.setattr(room_number, "TextChannel", FakeTextChannel, raising=False)
    source = FakeTextChannel(111)
    target = FakeTextChannel(222)
    guild = FakeGuild([source, target])
    bot = _bot()
    bot.user = guild.me
    cog = RoomNumber(bot)

    async with _database():
        config_row = await _create_room_config(guild, source, target)
        trigger = source if trigger_role == "source" else target
        selected = FakeRoomMessage(
            "12345",
            trigger,
            author_name="message_author",
            display_name="Message Author",
        )
        interaction = _interaction(guild, trigger)

        await cog.upsert_from_content_menu(interaction, selected)

        await config_row.refresh_from_db()
        assert config_row.room_number == "12345"
        assert target.sent_messages[0].embed.footer.text == (
            "部屋番号更新：Alice（@alice）"  # noqa: RUF001
        )
        assert interaction.followup.messages[-1] == (
            "部屋番号を「12345」に更新しました。",
            {"ephemeral": True},
        )


@pytest.mark.asyncio
async def test_automatic_template_capture_replaces_pointer_and_reacts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(room_number, "TextChannel", FakeTextChannel, raising=False)
    source = FakeTextChannel(111)
    target = FakeTextChannel(222)
    guild = FakeGuild([source, target])
    cog = RoomNumber(_bot())

    async with _database():
        config_row = await _create_room_config(guild, source, target)
        cog._delivery_generations[source.id] = 7  # noqa: SLF001
        candidate = FakeRoomMessage(
            "@{people} {room_number}\n#プロセカ募集",
            target,
            administrator=True,
            manage_channels=True,
        )

        await cog.on_message(candidate)

        await config_row.refresh_from_db()
        assert config_row.recruitment_template_channel_id == target.id
        assert config_row.recruitment_template_message_id == candidate.id
        assert cog._delivery_generations[source.id] == 7  # noqa: SLF001
        assert candidate.added_reactions == ["🔄"]


@pytest.mark.asyncio
async def test_automatic_template_capture_preserves_pointer_on_invalid_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(room_number, "TextChannel", FakeTextChannel, raising=False)
    source = FakeTextChannel(111)
    target = FakeTextChannel(222)
    guild = FakeGuild([source, target])
    cog = RoomNumber(_bot())

    async with _database():
        config_row = await _create_room_config(
            guild,
            source,
            target,
            template_channel_id=target.id,
            template_message_id=4000,
        )
        invalid = FakeRoomMessage(
            "missing room\n#プロセカ募集",
            target,
            administrator=True,
            manage_channels=True,
        )

        await cog.on_message(invalid)

        await config_row.refresh_from_db()
        assert config_row.recruitment_template_message_id == 4000
        assert invalid.added_reactions == [config.WARNING_EMOJI, "📏"]


@pytest.mark.asyncio
async def test_manual_template_capture_is_target_only_and_works_while_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(room_number, "TextChannel", FakeTextChannel, raising=False)
    source = FakeTextChannel(111)
    target = FakeTextChannel(222)
    guild = FakeGuild([source, target])
    cog = RoomNumber(_bot())

    async with _database():
        config_row = await _create_room_config(
            guild,
            source,
            target,
            template_enabled=False,
        )
        selected = FakeRoomMessage(
            "{room_number}\n#プロセカ協力",
            target,
            administrator=False,
            manage_channels=False,
        )
        interaction = _interaction(guild, target)

        await cog.set_recruitment_template_from_context_menu(interaction, selected)

        await config_row.refresh_from_db()
        assert config_row.recruitment_template_channel_id == target.id
        assert config_row.recruitment_template_message_id == selected.id
        assert interaction.followup.messages == [
            ("募集テンプレに設定しました。", {"ephemeral": True})
        ]
        assert selected.added_reactions == []


@pytest.mark.asyncio
async def test_live_template_is_fetched_once_and_builds_preview_and_links(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(room_number, "TextChannel", FakeTextChannel, raising=False)
    source = FakeTextChannel(111)
    target = FakeTextChannel(222)
    guild = FakeGuild([source, target])
    bot = _bot()
    bot.user = guild.me
    cog = RoomNumber(bot)

    async with _database():
        await _create_room_config(
            guild,
            source,
            target,
            template_channel_id=target.id,
            template_message_id=4000,
        )
        template = FakeRoomMessage(
            "@{people} {room_number} 👨‍👩‍👧‍👦️\n#プロセカ募集",
            target,
            message_id=4000,
        )
        target.fetch_messages[template.id] = template
        trigger = FakeRoomMessage("12345", source)

        await cog.on_message(trigger)

        assert target.fetched_message_ids == [4000]
        sent = target.sent_messages[0]
        assert sent.embed.fields[0].value == ("@ 12345 👨‍👩‍👧‍👦️\n#プロセカ募集")
        assert len(sent.view.children) == 5
        assert parse_qs(urlsplit(sent.view.children[2].url).query)["text"] == [
            "@2 12345 👨‍👩‍👧‍👦️\n#プロセカ募集"
        ]


@pytest.mark.asyncio
async def test_room_delivery_orders_output_before_rename_and_edits_same_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(room_number, "TextChannel", FakeTextChannel, raising=False)
    events: list[tuple[object, ...]] = []
    source = FakeTextChannel(111, events=events)
    target = FakeTextChannel(222, events=events)
    guild = FakeGuild([source, target])
    bot = _bot()
    bot.user = guild.me
    cog = RoomNumber(bot)

    async with _database():
        await _create_room_config(guild, source, target)
        trigger = FakeRoomMessage("12345", source)

        await cog.on_message(trigger)

        sent = target.sent_messages[0]
        assert events == [
            ("reaction_add", trigger.id, config.PROCESSING_EMOJI),
            ("send", target.id),
            ("rename", target.id, "部屋番号【12345】"),
            ("output_edit", sent.id),
            ("reaction_add", trigger.id, "✅"),
            ("reaction_remove", trigger.id, config.PROCESSING_EMOJI),
        ]
        assert sent.edits[-1]["embed"].description is None


@pytest.mark.asyncio
async def test_room_storage_failure_uses_safe_embed_without_error_reactions(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(room_number, "TextChannel", FakeTextChannel, raising=False)
    source = FakeTextChannel(111)
    target = FakeTextChannel(222)
    guild = FakeGuild([source, target])
    bot = _bot()
    bot.user = guild.me
    cog = RoomNumber(bot)
    caplog.set_level(logging.WARNING, logger=cog.logger.name)

    async def fail_persistence(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise StorageError(StorageErrorKind.DATABASE_WRITE)

    monkeypatch.setattr(
        cog,
        "_persist_room_update",
        fail_persistence,
        raising=False,
    )

    async with _database():
        config_row = await _create_room_config(guild, source, target)
        trigger = FakeRoomMessage("12345 private-room-text", source)

        await cog._handle_room_update(  # noqa: SLF001
            trigger,
            config_row,
            "12345",
            UserInfo(username="alice", display_name="Alice"),
        )

        await config_row.refresh_from_db()
        assert config_row.room_number is None
        assert target.sent_messages == []
        assert source.sent_messages[0].embed.title == "部屋番号を更新できませんでした"
        assert "エラー参照ID" in source.sent_messages[0].embed.description
        assert trigger.added_reactions == [config.PROCESSING_EMOJI]
        assert trigger.removed_reactions == [(config.PROCESSING_EMOJI, guild.me)]
        reference = re.search(
            r"`(STG-[0-9a-f]{8})`",
            source.sent_messages[0].embed.description,
        )
        assert reference is not None
        assert f"reference={reference.group(1)}" in caplog.text
        assert "private-room-text" not in caplog.text


@pytest.mark.asyncio
async def test_automatic_template_capture_ignores_wrong_role_permission_and_toggle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(room_number, "TextChannel", FakeTextChannel, raising=False)
    source = FakeTextChannel(111)
    target = FakeTextChannel(222)
    guild = FakeGuild([source, target])
    cog = RoomNumber(_bot())

    async with _database():
        config_row = await _create_room_config(guild, source, target)
        wrong_role = FakeRoomMessage(
            "{room_number}\n#プロセカ募集",
            source,
            administrator=True,
            manage_channels=True,
        )
        unauthorized = FakeRoomMessage(
            "{room_number}\n#プロセカ募集",
            target,
            message_id=5002,
            administrator=True,
            manage_channels=False,
        )

        await cog.on_message(wrong_role)
        await cog.on_message(unauthorized)
        config_row.recruitment_template_enabled = False
        await config_row.save(
            update_fields=["recruitment_template_enabled", "updated_at"]
        )
        disabled = FakeRoomMessage(
            "{room_number}\n#プロセカ募集",
            target,
            message_id=5003,
            administrator=True,
            manage_channels=True,
        )
        await cog.on_message(disabled)

        await config_row.refresh_from_db()
        assert config_row.recruitment_template_message_id is None
        assert wrong_role.added_reactions == []
        assert unauthorized.added_reactions == []
        assert disabled.added_reactions == []


@pytest.mark.asyncio
async def test_automatic_template_storage_failure_preserves_pointer_and_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(room_number, "TextChannel", FakeTextChannel, raising=False)
    source = FakeTextChannel(111)
    target = FakeTextChannel(222)
    guild = FakeGuild([source, target])
    cog = RoomNumber(_bot())

    async def fail_pointer(*args: object, **kwargs: object) -> bool:
        del args, kwargs
        raise StorageError(StorageErrorKind.DATABASE_WRITE)

    monkeypatch.setattr(cog, "_replace_template_pointer", fail_pointer)

    async with _database():
        config_row = await _create_room_config(
            guild,
            source,
            target,
            template_channel_id=target.id,
            template_message_id=4000,
        )
        cog._delivery_generations[source.id] = 7  # noqa: SLF001
        candidate = FakeRoomMessage(
            "{room_number}\n#プロセカ募集",
            target,
            administrator=True,
            manage_channels=True,
        )

        await cog.on_message(candidate)

        await config_row.refresh_from_db()
        assert config_row.recruitment_template_message_id == 4000
        assert cog._delivery_generations[source.id] == 7  # noqa: SLF001
        assert candidate.added_reactions == [config.WARNING_EMOJI, "🛠️"]


@pytest.mark.parametrize(
    ("template_enabled", "pointer", "expected_field", "expected_fetches"),
    [
        (False, True, None, []),
        (True, False, "募集テンプレが設定されていません。", []),
        (
            True,
            True,
            "募集テンプレを読み込めませんでした。設定を確認してください。",
            [4000],
        ),
    ],
)
@pytest.mark.asyncio
async def test_live_template_disabled_missing_and_invalid_states(
    monkeypatch: pytest.MonkeyPatch,
    template_enabled: bool,  # noqa: FBT001
    pointer: bool,  # noqa: FBT001
    expected_field: str | None,
    expected_fetches: list[int],
) -> None:
    monkeypatch.setattr(room_number, "TextChannel", FakeTextChannel, raising=False)
    source = FakeTextChannel(111)
    target = FakeTextChannel(222, name="部屋番号【12345】")
    guild = FakeGuild([source, target])
    bot = _bot()
    bot.user = guild.me
    cog = RoomNumber(bot)

    async with _database():
        await _create_room_config(
            guild,
            source,
            target,
            template_enabled=template_enabled,
            template_channel_id=target.id if pointer else None,
            template_message_id=4000 if pointer else None,
        )
        if pointer:
            target.fetch_messages[4000] = FakeRoomMessage(
                "invalid live edit\n#プロセカ募集",
                target,
                message_id=4000,
            )
        trigger = FakeRoomMessage("12345", source)

        await cog.on_message(trigger)

        sent = target.sent_messages[0]
        assert target.fetched_message_ids == expected_fetches
        if expected_field is None:
            assert sent.embed.fields == []
        else:
            assert sent.embed.fields[0].value == expected_field
        assert sent.view is None
        assert trigger.added_reactions == [config.PROCESSING_EMOJI, "✅"]


@pytest.mark.asyncio
async def test_already_correct_name_skips_rename_and_same_room_refreshes_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(room_number, "TextChannel", FakeTextChannel, raising=False)
    source = FakeTextChannel(111)
    target = FakeTextChannel(222, name="部屋番号【12345】")
    guild = FakeGuild([source, target])
    bot = _bot()
    bot.user = guild.me
    cog = RoomNumber(bot)

    async with _database():
        await _create_room_config(guild, source, target, room="12345")
        first = FakeRoomMessage("12345", source)
        second = FakeRoomMessage("12345", source, message_id=5002)

        await cog.on_message(first)
        await cog.on_message(second)

        assert target.edited_names == []
        assert len(target.sent_messages) == 2
        assert all(
            message.embed.description is None for message in target.sent_messages
        )
        assert target.sent_messages[0].edits == []
        assert first.added_reactions[-1] == "✅"
        assert second.added_reactions[-1] == "✅"


@pytest.mark.asyncio
async def test_rename_failure_edits_output_and_has_no_terminal_reaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(room_number, "TextChannel", FakeTextChannel, raising=False)
    source = FakeTextChannel(111)
    target = FakeTextChannel(222)
    target.edit_error = DiscordException("rename unavailable")
    guild = FakeGuild([source, target])
    bot = _bot()
    bot.user = guild.me
    cog = RoomNumber(bot)

    async with _database():
        await _create_room_config(guild, source, target)
        trigger = FakeRoomMessage("12345", source)

        await cog.on_message(trigger)

        sent = target.sent_messages[0]
        assert sent.embed.description == (
            "チャンネル名を更新できませんでした。\n"
            "設定されたチャンネルと、<@999> の"
            "「チャンネルの管理」権限を確認してください。"
        )
        assert trigger.added_reactions == [config.PROCESSING_EMOJI]
        assert trigger.removed_reactions == [(config.PROCESSING_EMOJI, guild.me)]


@pytest.mark.asyncio
async def test_target_output_failure_still_renames_and_sends_source_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(room_number, "TextChannel", FakeTextChannel, raising=False)
    source = FakeTextChannel(111)
    target = FakeTextChannel(222)
    target.send_error = DiscordException("send unavailable")
    guild = FakeGuild([source, target])
    bot = _bot()
    bot.user = guild.me
    cog = RoomNumber(bot)

    async with _database():
        await _create_room_config(guild, source, target)
        trigger = FakeRoomMessage("12345", source)

        await cog.on_message(trigger)

        assert target.name == "部屋番号【12345】"
        assert target.sent_messages == []
        assert source.sent_messages[0].embed.title == "部屋番号【12345】"
        assert "部屋番号は保存されましたが" in (
            source.sent_messages[0].embed.description
        )
        assert source.sent_messages[0].view is None
        assert trigger.added_reactions == [config.PROCESSING_EMOJI, "✅"]


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        (
            room_number.RoomUpdateResult(
                room_number="12345",
                persisted=True,
                naming_succeeded=True,
                output_succeeded=True,
                superseded=False,
            ),
            "部屋番号を「12345」に更新しました。",
        ),
        (
            room_number.RoomUpdateResult(
                room_number="12345",
                persisted=True,
                naming_succeeded=True,
                output_succeeded=False,
                superseded=False,
            ),
            "部屋番号を「12345」に更新しましたが、募集情報を送信できませんでした。",
        ),
        (
            room_number.RoomUpdateResult(
                room_number="12345",
                persisted=True,
                naming_succeeded=False,
                output_succeeded=True,
                superseded=False,
            ),
            "部屋番号「12345」は保存されましたが、チャンネル名を更新できませんでした。",
        ),
        (
            room_number.RoomUpdateResult(
                room_number="12345",
                persisted=True,
                naming_succeeded=False,
                output_succeeded=False,
                superseded=True,
            ),
            "新しい部屋番号が設定されたため、この更新は募集情報に反映されませんでした。",
        ),
        (
            room_number.RoomUpdateResult(
                room_number="12345",
                persisted=False,
                naming_succeeded=False,
                output_succeeded=False,
                superseded=False,
            ),
            "部屋番号を更新できませんでした。",
        ),
    ],
)
def test_manual_room_result_messages_are_exact(
    result: room_number.RoomUpdateResult,
    expected: str,
) -> None:
    assert RoomNumber._manual_room_result_message(result) == expected  # noqa: SLF001


@pytest.mark.asyncio
async def test_latest_generation_wins_aba_and_invalidates_older_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(room_number, "TextChannel", FakeTextChannel, raising=False)
    source = FakeTextChannel(111)
    target = FakeTextChannel(222)
    target.rename_started = asyncio.Event()
    target.rename_release = asyncio.Event()
    guild = FakeGuild([source, target])
    bot = _bot()
    bot.user = guild.me
    cog = RoomNumber(bot)

    async with _database():
        config_row = await _create_room_config(guild, source, target)
        original_persist = cog._persist_room_update  # noqa: SLF001
        persisted_count = 0
        all_persisted = asyncio.Event()

        async def tracked_persist(*args: object, **kwargs: object) -> object:
            nonlocal persisted_count
            result = await original_persist(*args, **kwargs)
            persisted_count += 1
            if persisted_count == 3:
                all_persisted.set()
            return result

        monkeypatch.setattr(cog, "_persist_room_update", tracked_persist)
        first = FakeRoomMessage("12345", source, message_id=5001)
        middle = FakeRoomMessage("67890", source, message_id=5002)
        latest = FakeRoomMessage("12345", source, message_id=5003)
        actor = UserInfo(username="alice", display_name="Alice")

        first_task = asyncio.create_task(
            cog._handle_room_update(  # noqa: SLF001
                first,
                config_row,
                "12345",
                actor,
            )
        )
        await asyncio.wait_for(target.rename_started.wait(), timeout=1)
        middle_task = asyncio.create_task(
            cog._handle_room_update(  # noqa: SLF001
                middle,
                config_row,
                "67890",
                actor,
            )
        )
        latest_task = asyncio.create_task(
            cog._handle_room_update(  # noqa: SLF001
                latest,
                config_row,
                "12345",
                actor,
            )
        )
        await asyncio.wait_for(all_persisted.wait(), timeout=2)
        assert cog._delivery_generations[source.id] == 3  # noqa: SLF001
        target.rename_release.set()

        await asyncio.wait_for(
            asyncio.gather(first_task, middle_task, latest_task),
            timeout=3,
        )

        await config_row.refresh_from_db()
        assert config_row.room_number == "12345"
        assert target.name == "部屋番号【12345】"
        assert len(target.sent_messages) == 2
        assert target.sent_messages[0].embed.description == (
            "新しい部屋番号が設定されたため、この募集情報は無効です。"
        )
        assert target.sent_messages[0].embed.fields == []
        assert target.sent_messages[0].view is None
        assert "✅" not in first.added_reactions
        assert "✅" not in middle.added_reactions
        assert latest.added_reactions[-1] == "✅"


@pytest.mark.asyncio
async def test_manual_template_menu_rechecks_permission_role_and_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(room_number, "TextChannel", FakeTextChannel, raising=False)
    source = FakeTextChannel(111)
    target = FakeTextChannel(222)
    guild = FakeGuild([source, target])
    cog = RoomNumber(_bot())

    async with _database():
        config_row = await _create_room_config(guild, source, target)
        valid_source_message = FakeRoomMessage(
            "{room_number}\n#プロセカ募集",
            source,
        )
        denied = _interaction(guild, target, manage_channels=False)
        await cog.set_recruitment_template_from_context_menu(
            denied,
            FakeRoomMessage("{room_number}\n#プロセカ募集", target),
        )
        assert denied.response.messages[0][0] == MISSING_SETTINGS_PERMISSION_MESSAGE

        wrong_role = _interaction(guild, source)
        await cog.set_recruitment_template_from_context_menu(
            wrong_role,
            valid_source_message,
        )
        assert wrong_role.followup.messages == [
            (room_number.MANUAL_TEMPLATE_TARGET_MESSAGE, {"ephemeral": True})
        ]

        invalid = _interaction(guild, target)
        await cog.set_recruitment_template_from_context_menu(
            invalid,
            FakeRoomMessage("missing field\n#プロセカ募集", target),
        )
        assert "{room_number}" in invalid.followup.messages[0][0]
        await config_row.refresh_from_db()
        assert config_row.recruitment_template_message_id is None


@pytest.mark.asyncio
async def test_manual_template_storage_failure_is_ephemeral_and_preserves_pointer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(room_number, "TextChannel", FakeTextChannel, raising=False)
    source = FakeTextChannel(111)
    target = FakeTextChannel(222)
    guild = FakeGuild([source, target])
    cog = RoomNumber(_bot())

    async def fail_pointer(*args: object, **kwargs: object) -> bool:
        del args, kwargs
        raise StorageError(StorageErrorKind.DATABASE_WRITE)

    monkeypatch.setattr(cog, "_replace_template_pointer", fail_pointer)

    async with _database():
        config_row = await _create_room_config(
            guild,
            source,
            target,
            template_channel_id=target.id,
            template_message_id=4000,
        )
        selected = FakeRoomMessage(
            "{room_number}\n#プロセカ募集",
            target,
        )
        interaction = _interaction(guild, target)

        await cog.set_recruitment_template_from_context_menu(interaction, selected)

        await config_row.refresh_from_db()
        assert config_row.recruitment_template_message_id == 4000
        content, kwargs = interaction.followup.messages[0]
        assert "Reference: `STG-" in content
        assert kwargs == {"ephemeral": True}
        assert selected.added_reactions == []


@pytest.mark.asyncio
async def test_manual_template_lookup_storage_failure_is_ephemeral(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(room_number, "TextChannel", FakeTextChannel, raising=False)
    target = FakeTextChannel(222)
    guild = FakeGuild([target])
    cog = RoomNumber(_bot())

    async def fail_lookup(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise StorageError(StorageErrorKind.DATABASE_UNAVAILABLE)

    monkeypatch.setattr(cog, "_enabled_config_for_channel", fail_lookup)
    selected = FakeRoomMessage("{room_number}\n#プロセカ募集", target)
    interaction = _interaction(guild, target)

    await cog.set_recruitment_template_from_context_menu(interaction, selected)

    content, kwargs = interaction.followup.messages[0]
    assert "Reference: `STG-" in content
    assert kwargs == {"ephemeral": True}


@pytest.mark.asyncio
async def test_deleted_live_template_is_unreadable_without_clearing_pointer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(room_number, "TextChannel", FakeTextChannel, raising=False)
    source = FakeTextChannel(111)
    target = FakeTextChannel(222, name="部屋番号【12345】")
    target.fetch_error = DiscordException("deleted")
    guild = FakeGuild([source, target])
    bot = _bot()
    bot.user = guild.me
    cog = RoomNumber(bot)

    async with _database():
        config_row = await _create_room_config(
            guild,
            source,
            target,
            template_channel_id=target.id,
            template_message_id=4000,
        )
        trigger = FakeRoomMessage("12345", source)

        await cog.on_message(trigger)

        await config_row.refresh_from_db()
        assert config_row.recruitment_template_message_id == 4000
        assert target.fetched_message_ids == [4000]
        assert target.sent_messages[0].embed.fields[0].value == (
            "募集テンプレを読み込めませんでした。設定を確認してください。"
        )
        assert trigger.added_reactions[-1] == "✅"


@pytest.mark.asyncio
async def test_output_edit_failure_is_not_resent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(room_number, "TextChannel", FakeTextChannel, raising=False)
    source = FakeTextChannel(111)
    target = FakeTextChannel(222)
    target.rename_started = asyncio.Event()
    target.rename_release = asyncio.Event()
    guild = FakeGuild([source, target])
    bot = _bot()
    bot.user = guild.me
    cog = RoomNumber(bot)

    async with _database():
        await _create_room_config(guild, source, target)
        trigger = FakeRoomMessage("12345", source)
        task = asyncio.create_task(cog.on_message(trigger))
        await asyncio.wait_for(target.rename_started.wait(), timeout=1)
        target.sent_messages[0].edit_error = DiscordException("deleted output")
        target.rename_release.set()

        await asyncio.wait_for(task, timeout=2)

        assert len(target.sent_messages) == 1
        assert trigger.added_reactions[-1] == "✅"


@pytest.mark.asyncio
async def test_concurrent_same_room_only_latest_completes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(room_number, "TextChannel", FakeTextChannel, raising=False)
    source = FakeTextChannel(111)
    target = FakeTextChannel(222)
    target.rename_started = asyncio.Event()
    target.rename_release = asyncio.Event()
    guild = FakeGuild([source, target])
    bot = _bot()
    bot.user = guild.me
    cog = RoomNumber(bot)

    async with _database():
        config_row = await _create_room_config(guild, source, target)
        original_persist = cog._persist_room_update  # noqa: SLF001
        second_persisted = asyncio.Event()
        persisted_count = 0

        async def tracked_persist(*args: object, **kwargs: object) -> object:
            nonlocal persisted_count
            result = await original_persist(*args, **kwargs)
            persisted_count += 1
            if persisted_count == 2:
                second_persisted.set()
            return result

        monkeypatch.setattr(cog, "_persist_room_update", tracked_persist)
        actor = UserInfo(username="alice", display_name="Alice")
        first = FakeRoomMessage("12345", source, message_id=5001)
        second = FakeRoomMessage("12345", source, message_id=5002)
        first_task = asyncio.create_task(
            cog._handle_room_update(  # noqa: SLF001
                first,
                config_row,
                "12345",
                actor,
            )
        )
        await asyncio.wait_for(target.rename_started.wait(), timeout=1)
        second_task = asyncio.create_task(
            cog._handle_room_update(  # noqa: SLF001
                second,
                config_row,
                "12345",
                actor,
            )
        )
        await asyncio.wait_for(second_persisted.wait(), timeout=2)
        target.rename_release.set()

        await asyncio.wait_for(asyncio.gather(first_task, second_task), timeout=3)

        assert len(target.sent_messages) == 2
        assert target.sent_messages[0].embed.description == (
            "新しい部屋番号が設定されたため、この募集情報は無効です。"
        )
        assert "✅" not in first.added_reactions
        assert second.added_reactions[-1] == "✅"


@pytest.mark.asyncio
async def test_disable_invalidates_in_flight_delivery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(room_number, "TextChannel", FakeTextChannel, raising=False)
    source = FakeTextChannel(111)
    target = FakeTextChannel(222)
    target.rename_started = asyncio.Event()
    target.rename_release = asyncio.Event()
    guild = FakeGuild([source, target])
    bot = _bot()
    bot.user = guild.me
    cog = RoomNumber(bot)

    async with _database():
        config_row = await _create_room_config(guild, source, target)
        trigger = FakeRoomMessage("12345", source)
        update_task = asyncio.create_task(
            cog._handle_room_update(  # noqa: SLF001
                trigger,
                config_row,
                "12345",
                UserInfo(username="alice", display_name="Alice"),
            )
        )
        await asyncio.wait_for(target.rename_started.wait(), timeout=1)

        assert await cog._disable_channel(guild.id, source.id) is True  # noqa: SLF001
        target.rename_release.set()
        result = await asyncio.wait_for(update_task, timeout=2)

        assert result.superseded is True
        assert "✅" not in trigger.added_reactions
        assert target.sent_messages[0].embed.description == (
            "新しい部屋番号が設定されたため、この募集情報は無効です。"
        )


@pytest.mark.asyncio
async def test_each_settings_action_renders_classified_storage_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(room_number, "TextChannel", FakeTextChannel, raising=False)
    source = FakeTextChannel(111)
    target = FakeTextChannel(222)
    guild = FakeGuild([source, target])
    cog = RoomNumber(_bot())

    async def fail_storage(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise StorageError(StorageErrorKind.DATABASE_WRITE)

    async with _database():
        config_row = await _create_room_config(guild, source, target)
        source_membership = await FeatureChannel.get(channel_id=source.id)

        monkeypatch.setattr(cog, "_persist_target_selection", fail_storage)
        select_interaction = _interaction(guild, source)
        await select_interaction.response.defer(ephemeral=True)
        await cog._select_target(  # noqa: SLF001
            select_interaction,
            source_membership.id,
            config_row.id,
            config_row.updated_at,
            target.id,
            SettingsTimeoutView(),
        )
        assert "Reference: `STG-" in select_interaction.followup.messages[0][0]

        monkeypatch.setattr(cog, "_locked_owned_config", fail_storage)
        format_interaction = _interaction(guild, source)
        await format_interaction.response.defer(ephemeral=True)
        await cog._save_channel_name_format(  # noqa: SLF001
            format_interaction,
            config_row.id,
            config_row.updated_at,
            "room-{room_number}",
            SettingsTimeoutView(),
        )
        assert "Reference: `STG-" in format_interaction.followup.messages[0][0]

        toggle_interaction = _interaction(guild, source)
        await toggle_interaction.response.defer(ephemeral=True)
        await cog._set_recruitment_template_enabled(  # noqa: SLF001
            toggle_interaction,
            config_row.id,
            config_row.updated_at,
            False,  # noqa: FBT003
            SettingsTimeoutView(),
        )
        assert "Reference: `STG-" in toggle_interaction.followup.messages[0][0]
