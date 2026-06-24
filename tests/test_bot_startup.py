from __future__ import annotations

import pytest
from discord.ext import commands

from bot.bot import Rhoboto
from bot.config import Config


def test_config_runtime_validation_is_explicit() -> None:
    config = Config()
    config.DISCORD_TOKEN = ""

    with pytest.raises(ValueError, match="DISCORD_TOKEN is required"):
        config.validate_runtime()


@pytest.mark.asyncio
async def test_load_extension_re_raises_startup_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def raise_extension_error(
        self: commands.Bot, name: str, *, package: str | None = None
    ) -> None:
        assert self is bot
        assert name == "cogs.missing"
        assert package == "pkg"
        msg = "boom"
        raise RuntimeError(msg)

    bot = Rhoboto(command_prefix="$", db_url="sqlite://:memory:", initial_cogs=[])
    monkeypatch.setattr(commands.Bot, "load_extension", raise_extension_error)

    try:
        with pytest.raises(RuntimeError, match="boom"):
            await bot.load_extension("cogs.missing", package="pkg")
    finally:
        await bot.close()
