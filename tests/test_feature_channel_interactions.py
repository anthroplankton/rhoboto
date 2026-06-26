from __future__ import annotations

from types import SimpleNamespace

import pytest

from cogs.base.feature_channel_base import FeatureChannelBase, FeatureChannelUserBase
from cogs.shift_register import ShiftRegister
from models.feature_channel import FeatureChannel
from tests.fakes import ConfiguredManager, FakeInteraction, MissingConfigManager
from utils.google_sheets_errors import GoogleSheetsError, GoogleSheetsErrorKind


async def fake_feature_channel_get(
    *, guild_id: int, channel_id: int, feature_name: str
) -> SimpleNamespace:
    return SimpleNamespace(
        guild_id=guild_id,
        channel_id=channel_id,
        feature_name=feature_name,
    )


class FakeMessage:
    id = 123

    def __init__(self) -> None:
        self.added_reactions: list[str] = []
        self.removed_reactions: list[tuple[str, object]] = []

    async def add_reaction(self, emoji: str) -> None:
        self.added_reactions.append(emoji)

    async def remove_reaction(self, emoji: str, user: object) -> None:
        self.removed_reactions.append((emoji, user))


class NullLogger:
    def warning(self, *_: object, **__: object) -> None:
        pass


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
async def test_context_menu_reports_google_sheets_error_safely() -> None:
    bot_user = object()
    message = FakeMessage()

    async def raise_google_sheets_error(message: FakeMessage) -> None:
        await message.add_reaction("<:haruka_math:1402204882492063825>")
        raise GoogleSheetsError(
            GoogleSheetsErrorKind.QUOTA,
            "Google Sheets is rate-limiting requests. Try again later.",
        )

    interaction = FakeInteraction()
    subject = SimpleNamespace(
        feature_name="team_register",
        process_upsert_from_message=raise_google_sheets_error,
        bot=SimpleNamespace(user=bot_user),
        logger=NullLogger(),
    )

    await FeatureChannelBase.upsert_from_content_menu(
        subject,
        interaction,
        message,
    )

    assert interaction.response.deferred == [False]
    assert interaction.followup.messages == [
        (
            "Google Sheets could not complete this action. "
            "Google Sheets is rate-limiting requests. Try again later.",
            {"ephemeral": False},
        )
    ]
    assert message.removed_reactions == [
        ("<:haruka_math:1402204882492063825>", bot_user)
    ]
    assert message.added_reactions == [
        "<:haruka_math:1402204882492063825>",
        "⚠️",
    ]


@pytest.mark.asyncio
async def test_message_listener_marks_google_sheets_error() -> None:
    bot_user = object()
    message = FakeMessage()

    async def raise_google_sheets_error(message: FakeMessage) -> None:
        await message.add_reaction("<:haruka_math:1402204882492063825>")
        raise GoogleSheetsError(
            GoogleSheetsErrorKind.TRANSIENT,
            "Google Sheets is temporarily unavailable. Try again later.",
        )

    subject = SimpleNamespace(
        process_upsert_from_message=raise_google_sheets_error,
        bot=SimpleNamespace(user=bot_user),
        logger=NullLogger(),
    )

    await FeatureChannelBase.on_message(subject, message)

    assert message.removed_reactions == [
        ("<:haruka_math:1402204882492063825>", bot_user)
    ]
    assert message.added_reactions == [
        "<:haruka_math:1402204882492063825>",
        "⚠️",
    ]


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
