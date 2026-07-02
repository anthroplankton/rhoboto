from __future__ import annotations

from types import SimpleNamespace

import pytest
from discord import HTTPException

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

    async def add_reaction(self, emoji: str) -> None:
        self.add_calls.append(emoji)
        if self.exc is not None:
            raise self.exc

    async def remove_reaction(self, emoji: str, user: object) -> None:
        self.remove_calls.append((emoji, user))
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
