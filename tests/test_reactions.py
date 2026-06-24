from __future__ import annotations

from types import SimpleNamespace

import pytest
from discord import HTTPException

from utils.reactions import remove_reaction_if_present


class FakeMessage:
    def __init__(self, exc: Exception | None = None) -> None:
        self.id = 123
        self.exc = exc
        self.calls: list[tuple[str, object]] = []

    async def remove_reaction(self, emoji: str, user: object) -> None:
        self.calls.append((emoji, user))
        if self.exc is not None:
            raise self.exc


@pytest.mark.asyncio
async def test_remove_reaction_if_present_removes_reaction() -> None:
    message = FakeMessage()
    user = object()

    await remove_reaction_if_present(message, "⌛", user)

    assert message.calls == [("⌛", user)]


@pytest.mark.asyncio
async def test_remove_reaction_if_present_tolerates_http_errors() -> None:
    response = SimpleNamespace(status=404, reason="Not Found")
    message = FakeMessage(HTTPException(response, "missing"))

    await remove_reaction_if_present(message, "⌛", object())

    assert len(message.calls) == 1
