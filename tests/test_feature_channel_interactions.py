from __future__ import annotations

from types import SimpleNamespace

import pytest

from cogs.base.feature_channel_base import FeatureChannelUserBase
from cogs.shift_register import ShiftRegister
from models.feature_channel import FeatureChannel
from tests.fakes import ConfiguredManager, FakeInteraction, MissingConfigManager


async def fake_feature_channel_get(
    *, guild_id: int, channel_id: int, feature_name: str
) -> SimpleNamespace:
    return SimpleNamespace(
        guild_id=guild_id,
        channel_id=channel_id,
        feature_name=feature_name,
    )


@pytest.mark.asyncio
async def test_user_help_defers_before_followup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(FeatureChannel, "get", fake_feature_channel_get)
    interaction = FakeInteraction(locale="zh-TW")
    subject = SimpleNamespace(
        feature_name="team_register",
        ManagerType=ConfiguredManager,
        bot=SimpleNamespace(user=SimpleNamespace(mention="@Rhoboto")),
    )

    await FeatureChannelUserBase.send_help_message(subject, interaction, "team.help")

    assert interaction.response.deferred == [True]
    message, kwargs = interaction.followup.messages[0]
    assert kwargs["ephemeral"] is True
    assert "@Rhoboto" in str(message)
    assert "https://sheet.example" in str(message)


@pytest.mark.asyncio
async def test_user_help_uses_followup_for_missing_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(FeatureChannel, "get", fake_feature_channel_get)
    interaction = FakeInteraction()
    subject = SimpleNamespace(
        feature_name="team_register",
        ManagerType=MissingConfigManager,
        bot=SimpleNamespace(user=None),
    )

    await FeatureChannelUserBase.send_help_message(subject, interaction, "team.help")

    assert interaction.response.deferred == [True]
    message, kwargs = interaction.followup.messages[0]
    assert kwargs["ephemeral"] is True
    assert message == "`team_register` is not configured for this channel."


@pytest.mark.asyncio
async def test_shift_info_defers_before_public_followup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(FeatureChannel, "get", fake_feature_channel_get)
    interaction = FakeInteraction(locale="ja")
    subject = SimpleNamespace(
        feature_name="shift_register",
        ManagerType=ConfiguredManager,
        bot=SimpleNamespace(user=SimpleNamespace(mention="@Rhoboto")),
        info_template_key="shift.info",
    )

    await ShiftRegister.info.callback(
        subject,
        interaction,
        2,
        8,
        15,
        12,
        21,
        13,
        20,
        14,
        18,
    )

    assert interaction.response.deferred == [False]
    message, kwargs = interaction.followup.messages[0]
    assert kwargs["ephemeral"] is False
    assert "2日目" in str(message)
    assert "@Rhoboto" in str(message)
    assert "https://sheet.example" in str(message)
