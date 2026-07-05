from __future__ import annotations

from types import SimpleNamespace

import pytest

from cogs.base.discord_context import (
    require_guild_channel_source,
    require_guild_source,
)
from tests.fakes import FakeContext, FakeInteraction


def test_require_guild_source_returns_same_interaction() -> None:
    interaction = FakeInteraction()

    source = require_guild_source(interaction, action="configure language settings")

    assert source is interaction
    assert source.guild is interaction.guild


def test_require_guild_source_raises_action_specific_interaction_error() -> None:
    interaction = FakeInteraction()
    interaction.guild = None

    with pytest.raises(
        ValueError,
        match=("Interaction guild is None. Cannot configure language settings."),
    ):
        require_guild_source(interaction, action="configure language settings")


def test_require_guild_channel_source_returns_same_interaction() -> None:
    interaction = FakeInteraction()

    source = require_guild_channel_source(
        interaction,
        action="proceed with enable command",
    )

    assert source is interaction
    assert source.guild.id == 111
    assert source.channel.id == 222


def test_require_guild_channel_source_raises_interaction_error() -> None:
    interaction = FakeInteraction()
    interaction.channel = None

    with pytest.raises(
        ValueError,
        match=(
            "Interaction guild or channel is None. Cannot proceed with enable command."
        ),
    ):
        require_guild_channel_source(
            interaction,
            action="proceed with enable command",
        )


def test_require_guild_channel_source_labels_context() -> None:
    ctx = FakeContext()

    source = require_guild_channel_source(
        ctx,
        action="check feature status for feature: team_register",
    )

    assert source is ctx
    assert source.guild.id == 111
    assert source.channel.id == 222


def test_require_guild_channel_source_raises_context_error() -> None:
    ctx = FakeContext(channel=None)

    with pytest.raises(
        ValueError,
        match=(
            "Context guild or channel is None. "
            "Cannot check feature status for feature: team_register."
        ),
    ):
        require_guild_channel_source(
            ctx,
            action="check feature status for feature: team_register",
        )


def test_require_guild_channel_source_falls_back_to_class_name() -> None:
    class UnknownSource:
        guild = None
        channel = SimpleNamespace(id=222)

    with pytest.raises(
        ValueError,
        match=(
            "UnknownSource guild or channel is None. Cannot inspect unknown source."
        ),
    ):
        require_guild_channel_source(
            UnknownSource(),
            action="inspect unknown source",
        )
