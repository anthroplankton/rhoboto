# ruff: noqa: SLF001

from __future__ import annotations

from types import SimpleNamespace

import pytest

from cogs.base.discord_context import require_guild_channel_source
from cogs.base.feature_channel_context import (
    FeatureChannelContextMixin,
    MessageParseResult,
    MessageParseStatus,
)
from models.feature_channel import FeatureChannel
from tests.fakes import ConfiguredManager, FakeInteraction, MissingConfigManager
from utils.structs_base import UserInfo


class ContextSubject(FeatureChannelContextMixin[ConfiguredManager]):
    feature_name = "team_register"
    feature_display_name = "Team Register"
    ManagerType = ConfiguredManager


async def fake_feature_channel_get(
    *, guild_id: int, channel_id: int, feature_name: str
) -> SimpleNamespace:
    return SimpleNamespace(
        guild_id=guild_id,
        channel_id=channel_id,
        feature_name=feature_name,
        is_enabled=True,
    )


@pytest.mark.asyncio
async def test_feature_channel_context_uses_manager_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(FeatureChannel, "get", fake_feature_channel_get)
    interaction = FakeInteraction()
    subject = ContextSubject()
    source = require_guild_channel_source(
        interaction,
        action="inspect feature channel context",
    )

    context = await subject._get_feature_channel_context(source)

    assert context.guild_id == 111
    assert context.channel_id == 222
    assert not hasattr(context, "guild")
    assert context.feature_channel.feature_name == "team_register"
    assert isinstance(context.manager, ConfiguredManager)
    assert context.manager.feature_channel is context.feature_channel


@pytest.mark.asyncio
async def test_configured_context_returns_feature_config_without_followup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(FeatureChannel, "get", fake_feature_channel_get)
    interaction = FakeInteraction()
    subject = ContextSubject()
    source = require_guild_channel_source(
        interaction,
        action="inspect feature channel context",
    )
    feature_channel_context = await subject._get_feature_channel_context(
        source,
    )

    context = await subject._get_configured_feature_channel_context(
        feature_channel_context
    )

    assert context is not None
    assert context.guild_id == 111
    assert context.channel_id == 222
    assert not hasattr(context, "guild")
    assert context.feature_config.sheet_url == "https://sheet.example"
    assert not hasattr(context, "sheet_config")
    assert interaction.followup.messages == []


@pytest.mark.asyncio
async def test_configured_context_missing_config_returns_none_without_followup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class MissingConfigSubject(FeatureChannelContextMixin[MissingConfigManager]):
        feature_name = "team_register"
        feature_display_name = "Team Register"
        ManagerType = MissingConfigManager

    monkeypatch.setattr(FeatureChannel, "get", fake_feature_channel_get)
    interaction = FakeInteraction()
    subject = MissingConfigSubject()
    source = require_guild_channel_source(
        interaction,
        action="inspect feature channel context",
    )
    feature_channel_context = await subject._get_feature_channel_context(
        source,
    )

    context = await subject._get_configured_feature_channel_context(
        feature_channel_context
    )

    assert context is None
    assert interaction.followup.messages == []


@pytest.mark.asyncio
async def test_missing_config_followup_uses_feature_display_name() -> None:
    interaction = FakeInteraction()
    subject = ContextSubject()

    await subject._send_missing_config_followup(interaction)

    assert interaction.followup.messages == [
        (
            "⚠️ Team Register is not configured for this channel.",
            {"ephemeral": True},
        )
    ]


@pytest.mark.asyncio
async def test_enabled_feature_channel_lookup_filters_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = {
        "enabled": SimpleNamespace(is_enabled=True),
        "disabled": SimpleNamespace(is_enabled=False),
    }

    async def fake_get_or_none(
        *, guild_id: int, channel_id: int, feature_name: str
    ) -> object | None:
        assert guild_id == 111
        assert channel_id == 222
        return rows.get(feature_name)

    monkeypatch.setattr(FeatureChannel, "get_or_none", fake_get_or_none)

    assert (
        await ContextSubject._get_enabled_feature_channel_or_none(
            111,
            222,
            "enabled",
        )
        is rows["enabled"]
    )
    assert (
        await ContextSubject._get_enabled_feature_channel_or_none(
            111,
            222,
            "disabled",
        )
        is None
    )
    assert (
        await ContextSubject._get_enabled_feature_channel_or_none(
            111,
            222,
            "missing",
        )
        is None
    )


@pytest.mark.asyncio
async def test_feature_channel_context_or_none_respects_enabled_requirement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    disabled_row = SimpleNamespace(
        guild_id=111,
        channel_id=222,
        feature_name="team_register",
        is_enabled=False,
    )
    rows = {"current": disabled_row}

    async def fake_get_or_none(
        *, guild_id: int, channel_id: int, feature_name: str
    ) -> object | None:
        assert guild_id == 111
        assert channel_id == 222
        assert feature_name == "team_register"
        return rows["current"]

    monkeypatch.setattr(FeatureChannel, "get_or_none", fake_get_or_none)
    interaction = FakeInteraction()
    subject = ContextSubject()

    context = await subject._get_feature_channel_context_or_none(
        guild_id=interaction.guild.id,
        channel_id=222,
    )

    assert context is not None
    assert context.guild_id == 111
    assert context.channel_id == 222
    assert not hasattr(context, "guild")
    assert context.feature_channel is disabled_row
    assert isinstance(context.manager, ConfiguredManager)
    assert context.manager.feature_channel is disabled_row

    assert (
        await subject._get_feature_channel_context_or_none(
            guild_id=interaction.guild.id,
            channel_id=222,
            require_enabled=True,
        )
        is None
    )

    rows["current"] = None

    assert (
        await subject._get_feature_channel_context_or_none(
            guild_id=interaction.guild.id,
            channel_id=222,
        )
        is None
    )


def test_message_parse_result_factories() -> None:
    user_info = UserInfo(username="alice", display_name="Alice")
    ignored = MessageParseResult.ignored()
    invalid = MessageParseResult.invalid(user_info=user_info)
    parsed = MessageParseResult.parsed(["submission"], user_info=user_info)

    assert ignored.status is MessageParseStatus.IGNORED
    assert ignored.submission is None
    assert ignored.user_info is None
    assert invalid.status is MessageParseStatus.INVALID
    assert invalid.submission is None
    assert invalid.user_info is user_info
    assert parsed.status is MessageParseStatus.PARSED
    assert parsed.submission == ["submission"]
    assert parsed.user_info is user_info
