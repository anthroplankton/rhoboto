from __future__ import annotations

# ruff: noqa: ANN001, ANN202, ARG005, E501, RUF001, SLF001
import asyncio
from dataclasses import replace
from datetime import datetime, timedelta
from io import BytesIO
from types import MappingProxyType, SimpleNamespace
from unittest.mock import ANY, AsyncMock, Mock

import pytest
from discord import AllowedMentions, Embed, File
from tortoise.exceptions import DBConnectionError, OperationalError

from cogs import shift_notice
from cogs.base.feature_channel_base import FeatureNotEnabled
from cogs.shift_notice import (
    INVALID_DESTINATION_MESSAGE,
    ShiftNotice,
    configured_elsewhere_message,
    is_usable_shift_notice_destination,
)
from components.ui_shift_notice import (
    ReplaceShiftNoticeDestinationView,
    ShiftNoticeSettingsBundle,
    ShiftNoticeSettingsView,
)
from tests.fakes import FakeInteraction
from utils.google_sheets_errors import GoogleSheetsError, GoogleSheetsErrorKind
from utils.shift_notice import (
    JST,
    ShiftNoticeCaseKind,
    ShiftNoticeCatalog,
    ShiftNoticeCutWindow,
    ShiftNoticeFrame,
    ShiftNoticeFrameState,
    ShiftNoticePerson,
    ShiftNoticeSnapshot,
)

NOW = datetime(2026, 8, 1, 13, 45, tzinfo=JST)
ENVELOPE_START = datetime(2026, 8, 1, 13, tzinfo=JST)
ENVELOPE_END = datetime(2026, 8, 1, 15, tzinfo=JST)


class FakeTextChannel:
    def __init__(
        self,
        channel_id: int,
        *,
        view_channel: bool = True,
        send_messages: bool = True,
        embed_links: bool = True,
        attach_files: bool = True,
    ) -> None:
        self.id = channel_id
        self.mention = f"<#{channel_id}>"
        self._permissions = SimpleNamespace(
            view_channel=view_channel,
            send_messages=send_messages,
            embed_links=embed_links,
            attach_files=attach_files,
        )
        self.sent: list[dict[str, object]] = []
        self.send_error: Exception | None = None

    def permissions_for(self, member: object) -> SimpleNamespace:
        del member
        return self._permissions

    async def send(self, **kwargs: object) -> SimpleNamespace:
        self.sent.append(kwargs)
        if self.send_error is not None:
            raise self.send_error
        return SimpleNamespace(id=9001)


class FakeGuild:
    def __init__(
        self,
        channels: list[object],
        *,
        members: list[object] | None = None,
        filesize_limit: int = 8_000_000,
    ) -> None:
        self.id = 1001
        self.me = object()
        self.members = members or []
        self.filesize_limit = filesize_limit
        self._channels = {channel.id: channel for channel in channels}

    def get_channel(self, channel_id: int) -> object | None:
        return self._channels.get(channel_id)


def _bot() -> SimpleNamespace:
    return SimpleNamespace(add_cog=AsyncMock())


def _config(
    *,
    channel_id: int = 222,
    minute: int | None = 45,
    enabled: bool = True,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=10,
        guild_id=1001,
        updated_at=datetime(2026, 8, 1, 12, tzinfo=JST),
        minute_of_hour=minute,
        feature_channel=SimpleNamespace(
            id=20,
            guild_id=1001,
            channel_id=channel_id,
            is_enabled=enabled,
        ),
    )


def _catalog(
    *,
    start: datetime | None = ENVELOPE_START,
    end: datetime | None = ENVELOPE_END,
) -> ShiftNoticeCatalog:
    return ShiftNoticeCatalog(
        complete_sources=(),
        incomplete_sources=(),
        slot_owners={},
        envelope_start=start,
        envelope_end=end,
        overlap_losses=(),
    )


def _snapshot(target: datetime = datetime(2026, 8, 1, 14, tzinfo=JST)):
    alice = ShiftNoticePerson(("member", 101), "Alice", (101,))
    bob = ShiftNoticePerson(("member", 102), "Bob", (102,))
    previous = ShiftNoticeFrame(
        civil_start=target.replace(hour=target.hour - 1),
        event_hour=target.hour - 1,
        source_id=1,
        state=ShiftNoticeFrameState.ACTIVE_STAFFED,
        lanes=(alice, None, None, None, None),
    )
    next_frame = ShiftNoticeFrame(
        civil_start=target,
        event_hour=target.hour,
        source_id=1,
        state=ShiftNoticeFrameState.ACTIVE_STAFFED,
        lanes=(alice, bob, None, None, None),
    )
    return ShiftNoticeSnapshot(
        target_boundary=target,
        case=ShiftNoticeCaseKind.TRANSITION,
        previous=previous,
        next=next_frame,
        ending=(),
        continuing=(alice,),
        starting=(bob,),
        cumulative_hours=MappingProxyType({alice.key: 2}),
        remaining_hours=MappingProxyType({alice.key: 1, bob.key: 1}),
        cut_window=None,
    )


def test_cut_render_input_focuses_the_next_interval_in_a_fixed_window() -> None:
    rows = tuple(
        ShiftNoticeFrame(
            civil_start=datetime(2026, 8, 1, hour, tzinfo=JST),
            event_hour=hour,
            source_id=1,
            state=ShiftNoticeFrameState.CUT,
            lanes=(None,) * 5,
        )
        for hour in range(12, 19)
    )
    snapshot = ShiftNoticeSnapshot(
        target_boundary=rows[-1].civil_start,
        case=ShiftNoticeCaseKind.CUT,
        previous=rows[-2],
        next=rows[-1],
        ending=(),
        continuing=(),
        starting=(),
        cumulative_hours=MappingProxyType({}),
        remaining_hours=MappingProxyType({}),
        cut_window=ShiftNoticeCutWindow(
            rows=rows,
            truncated_before=False,
            truncated_after=False,
        ),
    )

    value = shift_notice._snapshot_render_input(snapshot)

    assert value.next is not None
    assert value.next.range_label == "18–19"


def _manager(*, catalog: ShiftNoticeCatalog | None = None, snapshot=None):
    return SimpleNamespace(
        load_source_catalog=AsyncMock(return_value=catalog or _catalog()),
        build_snapshot=AsyncMock(return_value=snapshot or _snapshot()),
    )


def _interaction(guild: FakeGuild, channel_id: int = 222) -> FakeInteraction:
    interaction = FakeInteraction(guild=guild)
    interaction.channel = guild.get_channel(channel_id)
    return interaction


@pytest.fixture(autouse=True)
def _patch_text_channel(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shift_notice, "TextChannel", FakeTextChannel)
    monkeypatch.setattr(
        shift_notice.asyncio,
        "to_thread",
        AsyncMock(side_effect=lambda function, value: function(value)),
    )


def test_command_surface_and_group_permissions_are_exact() -> None:
    names = {command.name for command in ShiftNotice.__cog_app_commands__}
    assert names == {
        "enable",
        "settings",
        "disable",
        "disable_and_clear",
        "send_latest",
    }
    permissions = ShiftNotice.__discord_app_commands_default_permissions__
    assert permissions.administrator is True
    assert permissions.manage_channels is True
    assert ShiftNotice.__discord_app_commands_guild_only__ is True
    send_latest = next(
        command
        for command in ShiftNotice.__cog_app_commands__
        if command.name == "send_latest"
    )
    assert str(send_latest.description) == (
        "Resend the latest eligible shift handoff notice."
    )


@pytest.mark.parametrize(
    "permission",
    ["view_channel", "send_messages", "embed_links", "attach_files"],
)
def test_destination_requires_normal_text_channel_and_all_four_permissions(
    permission: str,
) -> None:
    values = {
        "view_channel": True,
        "send_messages": True,
        "embed_links": True,
        "attach_files": True,
    }
    values[permission] = False
    channel = FakeTextChannel(222, **values)
    guild = FakeGuild([channel])
    assert is_usable_shift_notice_destination(channel, guild) is False
    assert is_usable_shift_notice_destination(SimpleNamespace(id=222), guild) is False
    assert "view the channel" in INVALID_DESTINATION_MESSAGE


@pytest.mark.asyncio
async def test_first_enable_claims_current_channel_and_offers_setup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _manager(catalog=_catalog(start=None, end=None))
    guild = FakeGuild([FakeTextChannel(222)])
    interaction = _interaction(guild)
    deferred_at_claim: list[tuple[bool, ...]] = []

    async def claim_destination(_guild_id: int, _channel_id: int) -> object:
        deferred_at_claim.append(tuple(interaction.response.deferred))
        return SimpleNamespace(
            config_id=10,
            feature_channel_id=20,
            channel_id=222,
            created=True,
            owns_requested_destination=True,
        )

    monkeypatch.setattr(
        shift_notice,
        "claim_destination",
        claim_destination,
    )
    monkeypatch.setattr(
        shift_notice,
        "get_destination_config",
        AsyncMock(return_value=_config(minute=None)),
    )
    cog = ShiftNotice(_bot(), manager=manager)

    await cog.enable.callback(cog, interaction)

    assert deferred_at_claim == [(True,)]
    assert interaction.response.deferred == [True]
    assert interaction.original_response_edits[0][0] == (
        "Feature Shift Notice enabled in this channel."
    )
    followup = interaction.followup.messages[0]
    assert followup[1]["embeds"][0].title == "Shift Notice Settings"
    assert followup[1]["view"].children[0].label == "Set Up Shift Notice"


@pytest.mark.asyncio
async def test_usable_existing_destination_rejects_second_claim_with_pointer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        shift_notice,
        "claim_destination",
        AsyncMock(
            return_value=SimpleNamespace(
                config_id=10,
                feature_channel_id=20,
                channel_id=777,
                created=False,
                owns_requested_destination=False,
            )
        ),
    )
    guild = FakeGuild([FakeTextChannel(222), FakeTextChannel(777)])
    interaction = _interaction(guild)
    cog = ShiftNotice(_bot(), manager=_manager())

    await cog.enable.callback(cog, interaction)

    content, kwargs = interaction.original_response_edits[0]
    assert content == configured_elsewhere_message(777)
    assert "<#777>" in content
    assert "/shift_notice settings" in content
    assert kwargs.get("view") is None


@pytest.mark.asyncio
async def test_unusable_existing_destination_offers_bound_destructive_replacement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        shift_notice,
        "claim_destination",
        AsyncMock(
            return_value=SimpleNamespace(
                config_id=10,
                feature_channel_id=20,
                channel_id=777,
                created=False,
                owns_requested_destination=False,
            )
        ),
    )
    monkeypatch.setattr(
        shift_notice,
        "get_guild_config",
        AsyncMock(return_value=_config(channel_id=777)),
    )
    guild = FakeGuild([FakeTextChannel(222), FakeTextChannel(777, attach_files=False)])
    interaction = _interaction(guild)
    cog = ShiftNotice(_bot(), manager=_manager())

    await cog.enable.callback(cog, interaction)

    content, kwargs = interaction.original_response_edits[0]
    assert content.startswith("‼️")
    assert isinstance(kwargs["view"], ReplaceShiftNoticeDestinationView)
    assert kwargs["view"].requesting_user_id == interaction.user.id
    assert kwargs["view"].replacement_channel_id == 222


@pytest.mark.asyncio
async def test_replacement_rechecks_old_unusable_state_retains_minute_and_enables_new_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(channel_id=777, minute=30, enabled=False)
    monkeypatch.setattr(
        shift_notice, "get_guild_config", AsyncMock(return_value=config)
    )
    replace = AsyncMock(return_value=_config(channel_id=222, minute=30, enabled=True))
    monkeypatch.setattr(shift_notice, "replace_unavailable_destination", replace)
    refreshed = _config(channel_id=222, minute=30, enabled=True)
    monkeypatch.setattr(
        shift_notice,
        "get_destination_config",
        AsyncMock(return_value=refreshed),
    )
    guild = FakeGuild([FakeTextChannel(222), FakeTextChannel(777, send_messages=False)])
    interaction = _interaction(guild)
    cog = ShiftNotice(_bot(), manager=_manager(catalog=_catalog(start=None, end=None)))

    await cog._replace_destination(interaction, 10, 777)

    replace.assert_awaited_once_with(10, 777, 222)
    assert refreshed.minute_of_hour == 30
    assert refreshed.feature_channel.is_enabled is True
    assert interaction.original_response_edits[0][0] == (
        "Feature Shift Notice enabled in this channel."
    )


@pytest.mark.asyncio
async def test_replacement_refuses_when_old_destination_becomes_usable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        shift_notice,
        "get_guild_config",
        AsyncMock(return_value=_config(channel_id=777)),
    )
    replace = AsyncMock()
    monkeypatch.setattr(shift_notice, "replace_unavailable_destination", replace)
    guild = FakeGuild([FakeTextChannel(222), FakeTextChannel(777)])
    interaction = _interaction(guild)
    cog = ShiftNotice(_bot(), manager=_manager())

    await cog._replace_destination(interaction, 10, 777)

    replace.assert_not_awaited()
    assert "changed while" in interaction.original_response_edits[0][0]


@pytest.mark.asyncio
async def test_settings_is_database_only_and_controls_only_first_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config()
    manager = _manager(catalog=_catalog(start=None, end=None))
    manager.build_snapshot.side_effect = AssertionError("Sheets must not be opened")
    monkeypatch.setattr(
        shift_notice,
        "get_destination_config",
        AsyncMock(return_value=config),
    )
    guild = FakeGuild([FakeTextChannel(222)])
    interaction = _interaction(guild)
    cog = ShiftNotice(_bot(), manager=manager)
    first = Embed(title="Shift Notice Settings")
    second = Embed(title="Shift Notice Settings")
    view = ShiftNoticeSettingsView(
        requesting_user_id=333,
        config=config,
        expected_channel_id=222,
        actions=cog._ui_actions(),
    )
    bundle = ShiftNoticeSettingsBundle(((first,), (second,)), view)
    monkeypatch.setattr(
        shift_notice, "build_shift_notice_settings_bundle", Mock(return_value=bundle)
    )

    await cog.setup_after_enable(interaction)

    manager.load_source_catalog.assert_awaited_once_with(1001)
    manager.build_snapshot.assert_not_awaited()
    assert interaction.followup.messages[0][1]["view"] is view
    assert interaction.followup.messages[1][1].get("view") is None
    assert view.continuation_messages == interaction.followup.sent_message_objects[1:]


@pytest.mark.asyncio
async def test_saved_panel_deletes_old_continuations_before_replacing_and_attaches_new(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    config = _config(minute=15)
    monkeypatch.setattr(
        shift_notice,
        "get_destination_config",
        AsyncMock(return_value=config),
    )
    guild = FakeGuild([FakeTextChannel(222)])
    interaction = _interaction(guild)

    async def edit_original_response(*args: object, **kwargs: object) -> None:
        del args, kwargs
        events.append("edit")

    interaction.edit_original_response = edit_original_response
    old_view = ShiftNoticeSettingsView(
        requesting_user_id=333,
        config=_config(),
        expected_channel_id=222,
        actions=ShiftNotice(_bot(), manager=_manager())._ui_actions(),
    )
    old_messages = []
    for index in range(2):
        message = SimpleNamespace()

        async def delete(index: int = index) -> None:
            events.append(f"delete-{index}")

        message.delete = delete
        old_messages.append(message)
    old_view.continuation_messages.extend(old_messages)

    manager = _manager(catalog=_catalog(start=None, end=None))
    cog = ShiftNotice(_bot(), manager=manager)
    new_view = ShiftNoticeSettingsView(
        requesting_user_id=333,
        config=config,
        expected_channel_id=222,
        actions=cog._ui_actions(),
    )
    bundle = ShiftNoticeSettingsBundle(
        ((Embed(title="Saved"),), (Embed(title="Continued"),)),
        new_view,
    )
    monkeypatch.setattr(
        shift_notice, "build_shift_notice_settings_bundle", Mock(return_value=bundle)
    )

    await cog._refresh_settings_response(
        interaction,
        guild,
        guild.get_channel(222),
        current_view=old_view,
    )

    assert events == ["delete-0", "delete-1", "edit"]
    assert new_view.continuation_messages == interaction.followup.sent_message_objects


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_boundary", ["config", "catalog"])
async def test_saved_minute_refresh_storage_failure_reports_partial_success(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    failure_boundary: str,
) -> None:
    config = _config()
    refresh_error = DBConnectionError("Alice private database host")
    get_config = AsyncMock(return_value=config)
    manager = _manager(catalog=_catalog(start=None, end=None))
    if failure_boundary == "config":
        get_config.side_effect = [config, refresh_error]
    else:
        manager.load_source_catalog.side_effect = refresh_error
    persist = AsyncMock(return_value=_config(minute=30))
    monkeypatch.setattr(shift_notice, "get_destination_config", get_config)
    monkeypatch.setattr(shift_notice, "save_minute", persist)
    guild = FakeGuild([FakeTextChannel(222)])
    interaction = _interaction(guild)
    cog = ShiftNotice(_bot(), manager=manager)
    reschedule = Mock()
    cog._reschedule_future_tick = reschedule
    current_view = ShiftNoticeSettingsView(
        requesting_user_id=interaction.user.id,
        config=config,
        expected_channel_id=222,
        actions=cog._ui_actions(),
    )

    with caplog.at_level("WARNING", logger="ShiftNotice"):
        await cog._save_minute(
            interaction,
            config.id,
            config.updated_at,
            config.minute_of_hour,
            "30",
            current_view,
        )

    persist.assert_awaited_once_with(
        config.id,
        expected_updated_at=config.updated_at,
        expected_minute=45,
        new_minute=30,
        setup_only=False,
    )
    reschedule.assert_called_once_with(1001)
    content, kwargs = interaction.followup.messages[-1]
    assert "Some changes may have been saved" in content
    assert "settings view could not be refreshed" in content
    assert "Reference: `STG-" in content
    assert kwargs == {"ephemeral": True}
    assert "operation=shift_notice_settings_refresh" in caplog.text
    assert "partial_success" in caplog.text
    assert "Alice" not in caplog.text
    assert "private database host" not in caplog.text


@pytest.mark.asyncio
async def test_lifecycle_owner_rejects_non_owner_channel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        shift_notice,
        "get_guild_config",
        AsyncMock(return_value=_config(channel_id=777)),
    )
    source = SimpleNamespace(
        guild=SimpleNamespace(id=1001), channel=SimpleNamespace(id=222)
    )
    cog = ShiftNotice(_bot(), manager=_manager())

    with pytest.raises(FeatureNotEnabled):
        await cog._validate_lifecycle_owner(source)


@pytest.mark.asyncio
async def test_send_latest_missing_setup_no_envelope_and_before_first_tick_are_ephemeral(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guild = FakeGuild([FakeTextChannel(222)])

    monkeypatch.setattr(
        shift_notice,
        "get_destination_config",
        AsyncMock(return_value=_config(minute=None)),
    )
    missing = _interaction(guild)
    manager = _manager()
    await ShiftNotice(_bot(), manager=manager, now=lambda: NOW).send_latest.callback(
        ShiftNotice(_bot(), manager=manager, now=lambda: NOW), missing
    )
    assert "minute" in missing.response.messages[0][0].lower()
    manager.load_source_catalog.assert_not_awaited()

    monkeypatch.setattr(
        shift_notice,
        "get_destination_config",
        AsyncMock(return_value=_config()),
    )
    no_envelope = _interaction(guild)
    empty_manager = _manager(catalog=_catalog(start=None, end=None))
    empty_cog = ShiftNotice(_bot(), manager=empty_manager, now=lambda: NOW)
    await empty_cog.send_latest.callback(empty_cog, no_envelope)
    assert "eligible boundary" in no_envelope.response.messages[0][0].lower()
    empty_manager.build_snapshot.assert_not_awaited()

    before = _interaction(guild)
    future_start = datetime(2026, 8, 1, 14, tzinfo=JST)
    future_manager = _manager(
        catalog=_catalog(start=future_start, end=future_start.replace(hour=15))
    )
    future_cog = ShiftNotice(
        _bot(),
        manager=future_manager,
        now=lambda: NOW.replace(minute=44),
    )
    await future_cog.send_latest.callback(future_cog, before)
    assert "available yet" in before.response.messages[0][0].lower()
    future_manager.build_snapshot.assert_not_awaited()


@pytest.mark.asyncio
async def test_send_latest_unusable_destination_logs_safe_ids_before_catalog(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(
        shift_notice,
        "get_destination_config",
        AsyncMock(return_value=_config()),
    )
    channel = FakeTextChannel(222, attach_files=False)
    guild = FakeGuild(
        [channel],
        members=[SimpleNamespace(display_name="Alice private sheet cell")],
    )
    interaction = _interaction(guild)
    manager = _manager()
    cog = ShiftNotice(_bot(), manager=manager, now=lambda: NOW)

    with caplog.at_level("WARNING", logger="ShiftNotice"):
        await cog.send_latest.callback(cog, interaction)

    manager.load_source_catalog.assert_not_awaited()
    manager.build_snapshot.assert_not_awaited()
    assert channel.sent == []
    content, kwargs = interaction.response.messages[-1]
    assert content == INVALID_DESTINATION_MESSAGE
    assert kwargs == {"ephemeral": True}
    assert "operation=send_latest stage=destination" in caplog.text
    assert "guild_id=1001" in caplog.text
    assert "config_id=10" in caplog.text
    assert "destination_channel_id=222" in caplog.text
    assert "Alice" not in caplog.text
    assert "private sheet cell" not in caplog.text


@pytest.mark.parametrize(
    ("now", "expected_target"),
    [
        (NOW, datetime(2026, 8, 1, 14, tzinfo=JST)),
        (datetime(2026, 8, 1, 18, tzinfo=JST), ENVELOPE_END),
    ],
)
@pytest.mark.asyncio
async def test_send_latest_selects_latest_reached_or_final_boundary(
    monkeypatch: pytest.MonkeyPatch,
    now: datetime,
    expected_target: datetime,
) -> None:
    config = _config()
    monkeypatch.setattr(
        shift_notice,
        "get_destination_config",
        AsyncMock(side_effect=[config, config]),
    )
    monkeypatch.setattr(
        shift_notice, "get_announcement_languages", AsyncMock(return_value=["en"])
    )
    manager = _manager()

    async def build_snapshot(catalog, target, resolver):
        del catalog, resolver
        return _snapshot(target)

    manager.build_snapshot.side_effect = build_snapshot
    guild = FakeGuild([FakeTextChannel(222)])
    interaction = _interaction(guild)
    cog = ShiftNotice(
        _bot(), manager=manager, renderer=lambda value: b"png", now=lambda: now
    )

    await cog.send_latest.callback(cog, interaction)

    assert manager.build_snapshot.await_args.args[1] == expected_target
    assert len(guild.get_channel(222).sent) == 1


@pytest.mark.asyncio
async def test_send_latest_uses_exact_resolver_to_thread_upload_limit_and_one_send(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    members = [SimpleNamespace(id=101, name="alice", display_name="Alice")]
    guild = FakeGuild([FakeTextChannel(222)], members=members, filesize_limit=1234)
    config = _config()
    monkeypatch.setattr(
        shift_notice,
        "get_destination_config",
        AsyncMock(side_effect=[config, config]),
    )
    exact_resolver = Mock(
        return_value=(SimpleNamespace(label="Alice", member_ids=(101,)),)
    )
    monkeypatch.setattr(
        shift_notice, "resolve_schedule_role_label_matches", exact_resolver
    )
    monkeypatch.setattr(
        shift_notice,
        "get_announcement_languages",
        AsyncMock(return_value=["ja", "en"]),
    )
    build_message = Mock(wraps=shift_notice.build_normal_message)
    monkeypatch.setattr(shift_notice, "build_normal_message", build_message)
    renderer = Mock(return_value=b"png")
    to_thread = AsyncMock(side_effect=lambda function, value: function(value))
    monkeypatch.setattr(shift_notice.asyncio, "to_thread", to_thread)
    manager = _manager()

    async def build_snapshot(catalog, target, resolver):
        del catalog
        resolver(("Alice",))
        return _snapshot(target)

    manager.build_snapshot.side_effect = build_snapshot
    interaction = _interaction(guild)
    cog = ShiftNotice(_bot(), manager=manager, renderer=renderer, now=lambda: NOW)

    await cog.send_latest.callback(cog, interaction)

    exact_resolver.assert_called_once_with(("Alice",), tuple(members))
    to_thread.assert_awaited_once_with(renderer, ANY)
    build_message.assert_called_once_with(
        ANY,
        b"png",
        ["ja", "en"],
        upload_limit=1234,
    )
    sent = guild.get_channel(222).sent
    assert len(sent) == 1
    assert isinstance(sent[0]["file"], File)
    assert isinstance(sent[0]["file"].fp, BytesIO)
    assert sent[0]["file"].filename == "shift-handoff.png"
    assert [embed.title for embed in sent[0]["embeds"]] == [
        "🕑 14時｜シフト交代インフォ",
        "🕑 14:00｜Shift Handoff Info",
    ]
    allowed_mentions = sent[0]["allowed_mentions"]
    assert isinstance(allowed_mentions, AllowedMentions)
    assert allowed_mentions.to_dict() == AllowedMentions.none().to_dict()
    assert "sent" in interaction.followup.messages[-1][0].lower()


@pytest.mark.asyncio
async def test_send_latest_pre_send_failure_is_ephemeral_log_only_and_safe(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(
        shift_notice,
        "get_destination_config",
        AsyncMock(return_value=_config()),
    )
    manager = _manager()
    manager.build_snapshot.side_effect = RuntimeError("Alice secret-sheet-cell")
    guild = FakeGuild([FakeTextChannel(222)])
    interaction = _interaction(guild)
    cog = ShiftNotice(_bot(), manager=manager, now=lambda: NOW)

    with caplog.at_level("WARNING"):
        await cog.send_latest.callback(cog, interaction)

    assert guild.get_channel(222).sent == []
    assert interaction.followup.messages
    assert "could not be sent" in interaction.followup.messages[-1][0].lower()
    assert "RuntimeError" in caplog.text
    assert "Alice" not in caplog.text
    assert "secret-sheet-cell" not in caplog.text


@pytest.mark.asyncio
async def test_send_latest_catalog_failure_is_ephemeral_log_only_and_safe(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(
        shift_notice,
        "get_destination_config",
        AsyncMock(return_value=_config()),
    )
    manager = _manager()
    manager.load_source_catalog.side_effect = RuntimeError("Alice source-secret")
    guild = FakeGuild([FakeTextChannel(222)])
    interaction = _interaction(guild)
    cog = ShiftNotice(_bot(), manager=manager, now=lambda: NOW)

    with caplog.at_level("WARNING"):
        await cog.send_latest.callback(cog, interaction)

    assert guild.get_channel(222).sent == []
    assert interaction.response.messages
    assert "could not be sent" in interaction.response.messages[-1][0].lower()
    assert "RuntimeError" in caplog.text
    assert "Alice" not in caplog.text
    assert "source-secret" not in caplog.text


@pytest.mark.asyncio
async def test_send_latest_config_failure_is_ephemeral_log_only_and_safe(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(
        shift_notice,
        "get_destination_config",
        AsyncMock(side_effect=RuntimeError("Alice config-secret")),
    )
    guild = FakeGuild([FakeTextChannel(222)])
    interaction = _interaction(guild)
    cog = ShiftNotice(_bot(), manager=_manager(), now=lambda: NOW)

    with caplog.at_level("WARNING"):
        await cog.send_latest.callback(cog, interaction)

    assert guild.get_channel(222).sent == []
    assert interaction.response.messages
    assert "could not be sent" in interaction.response.messages[-1][0].lower()
    assert "RuntimeError" in caplog.text
    assert "Alice" not in caplog.text
    assert "config-secret" not in caplog.text


@pytest.mark.asyncio
async def test_send_latest_revalidates_before_send_and_rejects_oversize(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guild = FakeGuild([FakeTextChannel(222)], filesize_limit=2)
    interaction = _interaction(guild)
    monkeypatch.setattr(
        shift_notice,
        "get_destination_config",
        AsyncMock(return_value=_config()),
    )
    monkeypatch.setattr(
        shift_notice, "get_announcement_languages", AsyncMock(return_value=["en"])
    )
    cog = ShiftNotice(
        _bot(), manager=_manager(), renderer=lambda value: b"png", now=lambda: NOW
    )
    await cog.send_latest.callback(cog, interaction)
    assert guild.get_channel(222).sent == []

    guild.filesize_limit = 1234
    interaction = _interaction(guild)
    stale = _config(minute=30)
    monkeypatch.setattr(
        shift_notice,
        "get_destination_config",
        AsyncMock(side_effect=[_config(), stale]),
    )
    await cog.send_latest.callback(cog, interaction)
    assert guild.get_channel(222).sent == []
    assert "changed" in interaction.followup.messages[-1][0].lower()


@pytest.mark.asyncio
async def test_send_exception_is_not_retried_but_second_invocation_may_duplicate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    channel = FakeTextChannel(222)
    guild = FakeGuild([channel])
    config = _config()
    monkeypatch.setattr(
        shift_notice, "get_announcement_languages", AsyncMock(return_value=["en"])
    )
    monkeypatch.setattr(
        shift_notice,
        "get_destination_config",
        AsyncMock(side_effect=[config, config]),
    )
    channel.send_error = RuntimeError("ambiguous")
    cog = ShiftNotice(
        _bot(), manager=_manager(), renderer=lambda value: b"png", now=lambda: NOW
    )
    await cog.send_latest.callback(cog, _interaction(guild))
    assert len(channel.sent) == 1

    channel.send_error = None
    monkeypatch.setattr(
        shift_notice,
        "get_destination_config",
        AsyncMock(side_effect=[config, config, config, config]),
    )
    await cog.send_latest.callback(cog, _interaction(guild))
    await cog.send_latest.callback(cog, _interaction(guild))
    assert len(channel.sent) == 3


@pytest.mark.asyncio
async def test_send_latest_non_owner_raises_centralized_feature_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        shift_notice, "get_destination_config", AsyncMock(return_value=None)
    )
    guild = FakeGuild([FakeTextChannel(222)])
    interaction = _interaction(guild)
    cog = ShiftNotice(_bot(), manager=_manager(), now=lambda: NOW)

    with pytest.raises(FeatureNotEnabled):
        await cog.send_latest.callback(cog, interaction)


# Task 9 scheduler contract tests.  These intentionally target the cog-owned
# finite-task registry; the implementation must keep the automatic path
# independent from the manual send_latest flow above.
def _scheduler_bot(guild: FakeGuild) -> SimpleNamespace:
    bot = _bot()
    bot.get_guild = lambda guild_id: guild if guild_id == guild.id else None
    bot.guilds = [guild]
    bot.wait_until_ready = AsyncMock()
    return bot


def _tick_spec(*, tick: datetime, target: datetime, minute: int = 45):
    return shift_notice.ShiftNoticeTickSpec(
        config_id=10,
        feature_channel_id=20,
        guild_id=1001,
        channel_id=222,
        minute_of_hour=minute,
        scheduled_tick=tick,
        target_boundary=target,
    )


def test_following_tick_uses_next_exact_minute_and_boundary_semantics() -> None:
    assert shift_notice._following_exact_minute(
        datetime(2026, 8, 1, 13, 44, 10, tzinfo=JST)
    ) == datetime(2026, 8, 1, 13, 45, tzinfo=JST)
    assert shift_notice._following_exact_minute(
        datetime(2026, 8, 1, 13, 59, 59, tzinfo=JST)
    ) == datetime(2026, 8, 1, 14, tzinfo=JST)
    assert shift_notice.boundary_for_scheduled_tick(
        datetime(2026, 8, 1, 13, 45, tzinfo=JST), 45
    ) == datetime(2026, 8, 1, 14, tzinfo=JST)
    assert shift_notice.boundary_for_scheduled_tick(
        datetime(2026, 8, 1, 14, tzinfo=JST), 0
    ) == datetime(2026, 8, 1, 14, tzinfo=JST)


def test_strict_future_tick_excludes_reached_and_one_minute_boundary() -> None:
    now = datetime(2026, 8, 1, 13, 44, tzinfo=JST)
    assert shift_notice._strict_future_tick(now, 45) is None
    assert shift_notice._strict_future_tick(now + timedelta(seconds=1), 45) is not None
    assert shift_notice._strict_future_tick(
        datetime(2026, 8, 1, 13, 44, 30, tzinfo=JST), 45
    ) == datetime(2026, 8, 1, 13, 45, tzinfo=JST)


@pytest.mark.asyncio
async def test_dispatcher_selects_only_following_minute_and_boundary_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 8, 1, 13, 44, 10, tzinfo=JST)
    configs = [
        _config(minute=45),
        _config(minute=46),
        _config(minute=None),
        _config(minute=45, enabled=False),
    ]
    guild = FakeGuild([FakeTextChannel(222)])
    cog = ShiftNotice(_scheduler_bot(guild), manager=_manager(), now=lambda: now)
    monkeypatch.setattr(cog, "_enabled_configs", AsyncMock(return_value=configs))
    schedule = AsyncMock()
    monkeypatch.setattr(cog, "_schedule_tick", schedule)

    await cog._dispatcher_pass()

    schedule.assert_awaited_once()
    spec = schedule.await_args.args[0]
    assert spec.scheduled_tick == datetime(2026, 8, 1, 13, 45, tzinfo=JST)
    assert spec.target_boundary == datetime(2026, 8, 1, 14, tzinfo=JST)


@pytest.mark.asyncio
async def test_dispatcher_isolates_one_guild_schedule_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configs = [_config(minute=45), _config(minute=45)]
    configs[1].id = 11
    guild = FakeGuild([FakeTextChannel(222)])
    cog = ShiftNotice(
        _scheduler_bot(guild),
        manager=_manager(),
        now=lambda: datetime(2026, 8, 1, 13, 44, 10, tzinfo=JST),
    )
    monkeypatch.setattr(cog, "_enabled_configs", AsyncMock(return_value=configs))
    schedule = AsyncMock(side_effect=[RuntimeError("private"), None])
    monkeypatch.setattr(cog, "_schedule_tick", schedule)

    await cog._dispatcher_pass()

    assert schedule.await_count == 2


@pytest.mark.asyncio
async def test_bootstrap_uses_strict_future_window_without_backfill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 8, 1, 13, 44, 30, tzinfo=JST)
    configs = [_config(minute=45), _config(minute=0)]
    configs[1].id = 11
    guild = FakeGuild([FakeTextChannel(222)])
    bot = _scheduler_bot(guild)
    cog = ShiftNotice(bot, manager=_manager(), now=lambda: now)
    monkeypatch.setattr(cog, "_enabled_configs", AsyncMock(return_value=configs))
    schedule = AsyncMock()
    monkeypatch.setattr(cog, "_schedule_tick", schedule)

    await cog._bootstrap_shift_notice()

    schedule.assert_awaited_once()
    assert schedule.await_args.args[0].scheduled_tick == datetime(
        2026, 8, 1, 13, 45, tzinfo=JST
    )


@pytest.mark.asyncio
async def test_duplicate_tick_is_ignored_and_new_tick_replaces_old_task() -> None:
    guild = FakeGuild([FakeTextChannel(222)])
    cog = ShiftNotice(_scheduler_bot(guild), manager=_manager())
    first = _tick_spec(
        tick=datetime(2026, 8, 1, 13, 45, tzinfo=JST),
        target=datetime(2026, 8, 1, 14, tzinfo=JST),
    )
    second = _tick_spec(
        tick=datetime(2026, 8, 1, 13, 46, tzinfo=JST),
        target=datetime(2026, 8, 1, 14, tzinfo=JST),
    )
    cog._run_tick = AsyncMock()
    await cog._schedule_tick(first)
    old_task = cog._tick_tasks[1001]
    await cog._schedule_tick(first)
    assert cog._tick_tasks[1001] is old_task
    await cog._schedule_tick(second)
    assert old_task.cancelled() or old_task.cancelling()
    assert cog._tick_specs[1001] == second
    await cog.cog_unload()


@pytest.mark.asyncio
async def test_same_tick_identity_replacement_cancels_old_destination_task() -> None:
    guild = FakeGuild([FakeTextChannel(222), FakeTextChannel(333)])
    cog = ShiftNotice(_scheduler_bot(guild), manager=_manager())
    first = _tick_spec(
        tick=datetime(2026, 8, 1, 13, 45, tzinfo=JST),
        target=datetime(2026, 8, 1, 14, tzinfo=JST),
    )
    replacement = replace(first, channel_id=333)
    cog._run_tick = AsyncMock()

    await cog._schedule_tick(first)
    old_task = cog._tick_tasks[1001]
    await cog._schedule_tick(replacement)

    assert old_task.cancelled() or old_task.cancelling()
    assert cog._tick_specs[1001] == replacement
    assert cog._tick_tasks[1001] is not old_task
    await cog.cog_unload()


@pytest.mark.asyncio
async def test_concurrent_replacements_keep_newest_registry_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guild = FakeGuild(
        [FakeTextChannel(222), FakeTextChannel(333), FakeTextChannel(444)]
    )
    cog = ShiftNotice(_scheduler_bot(guild), manager=_manager())
    first = _tick_spec(
        tick=datetime(2026, 8, 1, 13, 45, tzinfo=JST),
        target=datetime(2026, 8, 1, 14, tzinfo=JST),
    )
    replacement = replace(first, channel_id=333)
    newest = replace(first, channel_id=444)

    async def hold(_spec: object) -> None:
        await asyncio.Event().wait()

    cog._run_tick = hold
    await cog._schedule_tick(first)

    release = asyncio.Event()
    original_gather = shift_notice.asyncio.gather

    async def gated_gather(*args: object, **kwargs: object) -> object:
        await release.wait()
        return await original_gather(*args, **kwargs)

    monkeypatch.setattr(shift_notice.asyncio, "gather", gated_gather)
    older = asyncio.create_task(cog._schedule_tick(replacement))
    await asyncio.sleep(0)
    newer = asyncio.create_task(cog._schedule_tick(newest))
    await asyncio.sleep(0)
    release.set()
    await asyncio.gather(older, newer)

    assert cog._tick_specs[1001] == newest
    await cog.cog_unload()


@pytest.mark.asyncio
async def test_finite_tick_prepares_thirty_seconds_early_then_waits_for_tick(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tick = datetime(2026, 8, 1, 13, 45, tzinfo=JST)
    target = datetime(2026, 8, 1, 14, tzinfo=JST)
    now = tick - timedelta(seconds=31)
    clock = [now]
    sleeps: list[datetime] = []

    async def sleep_until(value: datetime) -> None:
        sleeps.append(value)
        clock[0] = value

    guild = FakeGuild([FakeTextChannel(222)])
    config = _config()
    monkeypatch.setattr(
        shift_notice, "get_guild_config", AsyncMock(return_value=config)
    )
    monkeypatch.setattr(
        shift_notice, "get_announcement_languages", AsyncMock(return_value=["en"])
    )
    cog = ShiftNotice(
        _scheduler_bot(guild),
        manager=_manager(),
        renderer=lambda _: b"png",
        now=lambda: clock[0],
        sleep_until=sleep_until,
    )
    spec = _tick_spec(tick=tick, target=target)
    cog._tick_specs[1001] = spec
    await cog._run_tick(spec)
    assert sleeps[:2] == [tick - timedelta(seconds=30), tick]
    assert len(guild.get_channel(222).sent) == 1


@pytest.mark.asyncio
async def test_prepared_payload_does_not_read_sheets_again_before_send(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tick = datetime(2026, 8, 1, 13, 45, tzinfo=JST)
    target = datetime(2026, 8, 1, 14, tzinfo=JST)
    guild = FakeGuild([FakeTextChannel(222)])
    config = _config()
    monkeypatch.setattr(
        shift_notice, "get_guild_config", AsyncMock(return_value=config)
    )
    monkeypatch.setattr(
        shift_notice, "get_announcement_languages", AsyncMock(return_value=["en"])
    )
    manager = _manager()
    cog = ShiftNotice(
        _scheduler_bot(guild),
        manager=manager,
        renderer=lambda _: b"png",
        now=lambda: tick,
    )
    spec = _tick_spec(tick=tick, target=target)
    cog._tick_specs[1001] = spec
    await cog._run_tick(spec)
    manager.load_source_catalog.assert_awaited_once_with(1001)
    manager.build_snapshot.assert_awaited_once()
    assert len(guild.get_channel(222).sent) == 1


@pytest.mark.asyncio
async def test_replaced_destination_task_stays_silent_without_catalog_read(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    tick = datetime(2026, 8, 1, 13, 45, tzinfo=JST)
    target = datetime(2026, 8, 1, 14, tzinfo=JST)
    guild = FakeGuild([FakeTextChannel(222), FakeTextChannel(333)])
    current_config = _config(channel_id=333)
    get_config = AsyncMock(return_value=current_config)
    monkeypatch.setattr(shift_notice, "get_guild_config", get_config)
    manager = _manager()
    cog = ShiftNotice(
        _scheduler_bot(guild),
        manager=manager,
        renderer=lambda value: b"png",
        now=lambda: tick,
    )
    spec = _tick_spec(tick=tick, target=target)
    cog._tick_specs[1001] = spec

    with caplog.at_level("WARNING"):
        assert await cog._prepare_automatic_payload(spec) is None

    get_config.assert_awaited_once_with(1001)
    manager.load_source_catalog.assert_not_awaited()
    assert "operation=automatic_delivery stage=destination" not in caplog.text


@pytest.mark.asyncio
async def test_final_revalidation_retries_transient_without_rereading_sheets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tick = datetime(2026, 8, 1, 13, 45, tzinfo=JST)
    target = datetime(2026, 8, 1, 14, tzinfo=JST)
    guild = FakeGuild([FakeTextChannel(222)])
    config = _config()
    get_config = AsyncMock(side_effect=[config, OperationalError("temporary"), config])
    monkeypatch.setattr(shift_notice, "get_guild_config", get_config)
    monkeypatch.setattr(
        shift_notice, "get_announcement_languages", AsyncMock(return_value=["en"])
    )
    delays: list[float] = []

    async def retry_sleep(delay: float) -> None:
        delays.append(delay)

    manager = _manager()
    cog = ShiftNotice(
        _scheduler_bot(guild),
        manager=manager,
        renderer=lambda _: b"png",
        now=lambda: tick,
        retry_sleep=retry_sleep,
    )
    spec = _tick_spec(tick=tick, target=target)
    cog._tick_specs[1001] = spec

    await cog._run_tick(spec)

    assert len(guild.get_channel(222).sent) == 1
    assert delays == [5]
    assert manager.load_source_catalog.await_count == 1
    assert manager.build_snapshot.await_count == 1


@pytest.mark.asyncio
async def test_missing_automatic_destination_logs_safe_ids(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(shift_notice, "get_guild_config", AsyncMock(return_value=None))
    guild = FakeGuild([FakeTextChannel(222)])
    cog = ShiftNotice(_scheduler_bot(guild), manager=_manager())
    spec = _tick_spec(
        tick=datetime(2026, 8, 1, 13, 45, tzinfo=JST),
        target=datetime(2026, 8, 1, 14, tzinfo=JST),
    )
    cog._tick_specs[1001] = spec

    with caplog.at_level("WARNING"):
        assert await cog._delivery_context(spec) is None

    assert "operation=automatic_delivery" in caplog.text
    assert "stage=destination" in caplog.text
    assert "guild_id=1001" in caplog.text
    assert "config_id=10" in caplog.text
    assert "destination_channel_id=222" in caplog.text


@pytest.mark.asyncio
async def test_deterministic_automatic_preparation_failure_logs_safe_context(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    config = _config()
    monkeypatch.setattr(
        shift_notice, "get_guild_config", AsyncMock(return_value=config)
    )
    monkeypatch.setattr(
        shift_notice, "get_announcement_languages", AsyncMock(return_value=["en"])
    )
    manager = _manager()
    manager.build_snapshot.side_effect = ValueError("private cell text")
    guild = FakeGuild([FakeTextChannel(222)])
    cog = ShiftNotice(
        _scheduler_bot(guild),
        manager=manager,
        renderer=lambda _: b"png",
        now=lambda: NOW,
    )
    spec = _tick_spec(
        tick=NOW,
        target=datetime(2026, 8, 1, 14, tzinfo=JST),
    )
    cog._tick_specs[1001] = spec

    with caplog.at_level("WARNING"):
        await cog._run_tick(spec)

    assert len(guild.get_channel(222).sent) == 1
    assert "operation=automatic_prepare" in caplog.text
    assert "stage=prepare" in caplog.text
    assert "guild_id=1001" in caplog.text
    assert "config_id=10" in caplog.text
    assert "destination_channel_id=222" in caplog.text
    assert "target_boundary=2026-08-01T14:00:00+09:00" in caplog.text
    assert "exception_class=ValueError" in caplog.text
    assert "private cell text" not in caplog.text


def test_failure_event_hour_uses_source_axis_at_envelope_end() -> None:
    target = datetime(2026, 8, 2, 6, tzinfo=JST)
    source = SimpleNamespace(
        id=7,
        first_hour=24,
        end_hour=30,
        civil_start=lambda hour: (
            datetime(2026, 8, 1, tzinfo=JST) + timedelta(hours=hour)
        ),
        event_hour=lambda value: int(
            (value - datetime(2026, 8, 1, tzinfo=JST)).total_seconds() // 3600
        ),
    )
    catalog = ShiftNoticeCatalog(
        complete_sources=(source,),
        incomplete_sources=(),
        slot_owners={},
        envelope_start=datetime(2026, 8, 2, 0, tzinfo=JST),
        envelope_end=target,
        overlap_losses=(),
    )
    cog = ShiftNotice(_bot(), manager=_manager())

    assert cog._failure_event_hour(target, catalog) == 30


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        OperationalError("locked"),
        DBConnectionError("offline"),
        GoogleSheetsError(GoogleSheetsErrorKind.QUOTA, "quota"),
    ],
)
async def test_transient_preparation_retries_with_bounded_backoff(
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
) -> None:
    tick = datetime(2026, 8, 1, 13, 45, tzinfo=JST)
    target = datetime(2026, 8, 1, 14, tzinfo=JST)
    guild = FakeGuild([FakeTextChannel(222)])
    config = _config()
    monkeypatch.setattr(
        shift_notice, "get_guild_config", AsyncMock(return_value=config)
    )
    monkeypatch.setattr(
        shift_notice, "get_announcement_languages", AsyncMock(return_value=["en"])
    )
    manager = _manager()
    manager.load_source_catalog.side_effect = [error, _catalog()]
    delays: list[float] = []

    async def retry_sleep(delay: float) -> None:
        delays.append(delay)

    cog = ShiftNotice(
        _scheduler_bot(guild),
        manager=manager,
        renderer=lambda _: b"png",
        now=lambda: tick,
        retry_sleep=retry_sleep,
    )
    spec = _tick_spec(tick=tick, target=target)
    cog._tick_specs[1001] = spec
    await cog._run_tick(spec)
    assert delays == [5]
    assert len(guild.get_channel(222).sent) == 1


@pytest.mark.asyncio
async def test_deterministic_failure_waits_for_tick_and_sends_one_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tick = datetime(2026, 8, 1, 13, 45, tzinfo=JST)
    target = datetime(2026, 8, 1, 14, tzinfo=JST)
    clock = [tick - timedelta(seconds=30)]
    guild = FakeGuild([FakeTextChannel(222)])
    config = _config()
    monkeypatch.setattr(
        shift_notice, "get_guild_config", AsyncMock(return_value=config)
    )
    monkeypatch.setattr(
        shift_notice, "get_announcement_languages", AsyncMock(return_value=["en"])
    )
    manager = _manager()
    manager.build_snapshot.side_effect = ValueError("invalid payload")
    sleeps: list[datetime] = []

    async def sleep_until(value: datetime) -> None:
        sleeps.append(value)
        clock[0] = value

    cog = ShiftNotice(
        _scheduler_bot(guild),
        manager=manager,
        renderer=lambda _: b"png",
        now=lambda: clock[0],
        sleep_until=sleep_until,
    )
    spec = _tick_spec(tick=tick, target=target)
    cog._tick_specs[1001] = spec
    await cog._run_tick(spec)
    assert sleeps[-1] == tick
    assert len(guild.get_channel(222).sent) == 1
    assert "embeds" in guild.get_channel(222).sent[0]


@pytest.mark.asyncio
async def test_stale_task_and_send_exception_do_not_follow_up_or_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tick = datetime(2026, 8, 1, 13, 45, tzinfo=JST)
    guild = FakeGuild([FakeTextChannel(222)])
    guild.get_channel(222).send_error = RuntimeError("ambiguous")
    config = _config()
    monkeypatch.setattr(
        shift_notice, "get_destination_config", AsyncMock(return_value=config)
    )
    monkeypatch.setattr(
        shift_notice, "get_announcement_languages", AsyncMock(return_value=["en"])
    )
    cog = ShiftNotice(
        _scheduler_bot(guild),
        manager=_manager(),
        renderer=lambda _: b"png",
        now=lambda: tick,
    )
    spec = _tick_spec(tick=tick, target=datetime(2026, 8, 1, 14, tzinfo=JST))
    cog._tick_specs[1001] = _tick_spec(
        tick=tick, target=spec.target_boundary, minute=44
    )
    await cog._run_tick(spec)
    assert len(guild.get_channel(222).sent) == 0


@pytest.mark.asyncio
async def test_missing_registry_spec_is_stale_and_does_not_read_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    get_config = AsyncMock(side_effect=AssertionError("stale task must stop first"))
    monkeypatch.setattr(shift_notice, "get_destination_config", get_config)
    guild = FakeGuild([FakeTextChannel(222)])
    cog = ShiftNotice(_scheduler_bot(guild), manager=_manager(), now=lambda: NOW)
    spec = _tick_spec(
        tick=datetime(2026, 8, 1, 13, 45, tzinfo=JST),
        target=datetime(2026, 8, 1, 14, tzinfo=JST),
    )
    await cog._run_tick(spec)
    get_config.assert_not_awaited()


@pytest.mark.asyncio
async def test_cog_unload_cancels_and_gathers_bootstrap_and_tick_tasks() -> None:
    guild = FakeGuild([FakeTextChannel(222)])
    cog = ShiftNotice(_scheduler_bot(guild), manager=_manager())

    async def wait_forever() -> None:
        await asyncio.Event().wait()

    bootstrap = asyncio.create_task(wait_forever())
    tick_task = asyncio.create_task(wait_forever())
    cog._bootstrap_task = bootstrap
    spec = _tick_spec(
        tick=datetime(2026, 8, 1, 13, 45, tzinfo=JST),
        target=datetime(2026, 8, 1, 14, tzinfo=JST),
    )
    cog._tick_specs[1001] = spec
    cog._tick_tasks[1001] = tick_task
    await cog.cog_unload()
    assert bootstrap.cancelled()
    assert tick_task.cancelled()
    assert cog._bootstrap_task is None
    assert cog._tick_tasks == {}
    assert cog._tick_specs == {}
