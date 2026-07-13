from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest
from discord import HTTPException

from utils import reactions
from utils.reactions import (
    add_reaction_if_possible,
    add_reactions_if_possible,
    remove_reaction_if_present,
)


class FakeMessage:
    def __init__(self, exc: Exception | None = None) -> None:
        self.id = 123
        self.exc = exc
        self.add_calls: list[str] = []
        self.remove_calls: list[tuple[str, object]] = []
        self.calls: list[tuple[object, ...]] = []

    async def add_reaction(self, emoji: str) -> None:
        self.add_calls.append(emoji)
        self.calls.append(("add", emoji))
        if self.exc is not None:
            raise self.exc

    async def remove_reaction(self, emoji: str, user: object) -> None:
        self.remove_calls.append((emoji, user))
        self.calls.append(("remove", emoji, user))
        if self.exc is not None:
            raise self.exc


@pytest.mark.asyncio
async def test_remove_reaction_if_present_removes_reaction() -> None:
    message = FakeMessage()
    user = object()

    await remove_reaction_if_present(message, "⌛", user)

    assert message.remove_calls == [("⌛", user)]


@pytest.mark.asyncio
async def test_remove_reaction_if_present_tolerates_http_errors() -> None:
    response = SimpleNamespace(status=404, reason="Not Found")
    message = FakeMessage(HTTPException(response, "missing"))

    await remove_reaction_if_present(message, "⌛", object())

    assert len(message.remove_calls) == 1


@pytest.mark.asyncio
async def test_add_reaction_if_possible_adds_reaction() -> None:
    message = FakeMessage()

    await add_reaction_if_possible(message, "✅")

    assert message.add_calls == ["✅"]


@pytest.mark.asyncio
async def test_add_reaction_if_possible_tolerates_http_errors() -> None:
    response = SimpleNamespace(status=403, reason="Forbidden")
    message = FakeMessage(HTTPException(response, "forbidden"))

    await add_reaction_if_possible(message, "✅")

    assert message.add_calls == ["✅"]


@pytest.mark.asyncio
async def test_add_reactions_if_possible_adds_reactions_in_order() -> None:
    message = FakeMessage()

    await add_reactions_if_possible(message, ["⚠️", "🛠️"])

    assert message.add_calls == ["⚠️", "🛠️"]


@pytest.mark.asyncio
async def test_terminal_reactions_are_attempted_before_processing_removal() -> None:
    message = FakeMessage()
    user = object()

    await reactions.transition_processing_reaction(
        message,
        ["✅"],
        processing_emoji="⌛",
        user=user,
    )

    assert message.calls == [("add", "✅"), ("remove", "⌛", user)]


@pytest.mark.asyncio
async def test_terminal_reactions_still_apply_without_bot_user() -> None:
    message = FakeMessage()

    await reactions.transition_processing_reaction(
        message,
        ["⚠️", "🚧"],
        processing_emoji="⌛",
        user=None,
    )

    assert message.calls == [("add", "⚠️"), ("add", "🚧")]


@pytest.mark.asyncio
async def test_terminal_reactions_isolate_each_marker_delivery_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class FirstMarkerFailureMessage(FakeMessage):
        async def add_reaction(self, emoji: str) -> None:
            await super().add_reaction(emoji)
            if emoji == "⚠️":
                msg = "terminal marker delivery failed"
                raise RuntimeError(msg)

    message = FirstMarkerFailureMessage()
    user = object()
    log = logging.getLogger("tests.reactions.terminal_isolation")
    caplog.set_level(logging.ERROR, logger=log.name)

    await reactions.transition_processing_reaction(
        message,
        ["⚠️", "📏"],
        processing_emoji="⌛",
        user=user,
        log=log,
    )

    assert message.calls == [
        ("add", "⚠️"),
        ("add", "📏"),
        ("remove", "⌛", user),
    ]
    assert "Failed to deliver terminal reaction" in caplog.text
