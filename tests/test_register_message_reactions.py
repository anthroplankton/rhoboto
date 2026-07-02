from __future__ import annotations

from types import MethodType, SimpleNamespace

import pytest

from bot import config
from cogs.base.feature_channel_base import FeatureChannelBase
from cogs.shift_register import ShiftRegister
from cogs.team_register import TeamRegister
from models.feature_channel import FeatureChannel


class FakeLogger:
    def debug(self, *_: object, **__: object) -> None:
        pass

    def info(self, *_: object, **__: object) -> None:
        pass


class FakeAuthor:
    def __init__(self) -> None:
        self.bot = False
        self.name = "alice"
        self.display_name = "Alice"
        self.roles: list[object] = []


class FakeMessage:
    id = 123

    def __init__(self, content: str) -> None:
        self.content = content
        self.author = FakeAuthor()
        self.guild = SimpleNamespace(id=111)
        self.channel = SimpleNamespace(id=222)
        self.added_reactions: list[str] = []
        self.removed_reactions: list[tuple[str, object]] = []

    async def add_reaction(self, emoji: str) -> None:
        self.added_reactions.append(emoji)

    async def remove_reaction(self, emoji: str, user: object) -> None:
        self.removed_reactions.append((emoji, user))


async def enabled(*_: object) -> bool:
    return True


def make_subject(feature_name: str) -> SimpleNamespace:
    should_process_message = FeatureChannelBase._should_process_message  # noqa: SLF001
    message_user_info = FeatureChannelBase._message_user_info  # noqa: SLF001
    log_received_message = FeatureChannelBase._log_received_message  # noqa: SLF001
    subject = SimpleNamespace(
        feature_name=feature_name,
        logger=FakeLogger(),
        bot=SimpleNamespace(user=object()),
        is_enabled=enabled,
    )
    subject._should_process_message = MethodType(  # noqa: SLF001
        should_process_message,
        subject,
    )
    subject._message_user_info = MethodType(message_user_info, subject)  # noqa: SLF001
    subject._log_received_message = MethodType(  # noqa: SLF001
        log_received_message,
        subject,
    )
    return subject


@pytest.mark.asyncio
async def test_team_message_invalid_attempt_adds_confused_without_sheets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail_if_called(**_: object) -> None:
        raise AssertionError

    monkeypatch.setattr(FeatureChannel, "get_or_none", fail_if_called)
    subject = make_subject("team_register")
    message = FakeMessage("160//600/33")

    result = await TeamRegister.process_upsert_from_message(subject, message)

    assert result is None
    assert message.added_reactions == [config.CONFUSED_EMOJI]


@pytest.mark.asyncio
async def test_team_message_ordinary_text_adds_no_reaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail_if_called(**_: object) -> None:
        raise AssertionError

    monkeypatch.setattr(FeatureChannel, "get_or_none", fail_if_called)
    subject = make_subject("team_register")
    message = FakeMessage("公告")

    result = await TeamRegister.process_upsert_from_message(subject, message)

    assert result is None
    assert message.added_reactions == []


@pytest.mark.asyncio
async def test_shift_message_invalid_attempt_adds_confused_without_sheets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail_if_called(**_: object) -> None:
        raise AssertionError

    monkeypatch.setattr(FeatureChannel, "get_or_none", fail_if_called)
    subject = make_subject("shift_register")
    message = FakeMessage("18:00-20:00")

    result = await ShiftRegister.process_upsert_from_message(subject, message)

    assert result is None
    assert message.added_reactions == [config.CONFUSED_EMOJI]


@pytest.mark.asyncio
async def test_shift_message_ordinary_text_adds_no_reaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail_if_called(**_: object) -> None:
        raise AssertionError

    monkeypatch.setattr(FeatureChannel, "get_or_none", fail_if_called)
    subject = make_subject("shift_register")
    message = FakeMessage("20:00")

    result = await ShiftRegister.process_upsert_from_message(subject, message)

    assert result is None
    assert message.added_reactions == []
