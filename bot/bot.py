from __future__ import annotations

import asyncio
import logging
from typing import override

from discord import Intents
from discord.ext import commands
from tortoise import Tortoise

from bot.translator import Translator
from utils.db import init_db

logger = logging.getLogger(__name__)


class Rhoboto(commands.Bot):
    """
    Rhoboto Discord bot.

    Handles extension (cog) loading, initializes and closes Tortoise ORM database,
    and provides startup/shutdown logging.
    """

    def __init__(
        self, *, command_prefix: str, db_url: str, initial_cogs: list[str]
    ) -> None:
        """
        Initializes the Rhoboto bot.

        Args:
            command_prefix (str): The bot's command prefix.
            db_url (str): Database connection URL for Tortoise ORM.
            initial_cogs (list[str]): List of initial cogs module names to load.
        """
        self.initial_cogs = initial_cogs
        self.db_url = db_url
        intents = Intents.default()
        intents.members = True
        intents.message_content = True
        super().__init__(command_prefix=command_prefix, intents=intents)

    @override
    async def load_extension(self, name: str, *, package: str | None = None) -> None:
        """
        Loads a Discord.py extension (cog) by module name.

        Args:
            name (str): The module name of the extension to load (e.g., 'cogs.hello').
        """
        try:
            await super().load_extension(name)
            logger.info("Loaded cog: `%s`", name)
        except Exception:
            logger.exception("Failed to load cog `%s`", name)

    @override
    async def setup_hook(self) -> None:
        """
        Called by Discord.py when the bot is starting up.
        Loads all initial cogs and initializes the database, then syncs slash commands.
        """
        await asyncio.gather(
            init_db(self.db_url),
            *(self.load_extension(name) for name in self.initial_cogs),
        )
        await self.tree.set_translator(Translator())
        await self.tree.sync()
        logger.info("Slash commands synced.")

    async def on_ready(self) -> None:
        """
        Called when the bot has successfully connected to Discord.
        Logs the bot's user information.
        """
        if self.user is not None:
            logger.info(
                "Logged in as %s (ID: %s) %r", self.user, self.user.id, self.user
            )
        else:
            logger.warning("Bot user is None on on_ready.")

    async def close(self) -> None:
        """
        Closes Tortoise ORM connections and shuts down the Discord bot.
        """
        logger.info("Closing Tortoise ORM connections...")
        await Tortoise.close_connections()
        logger.info("Tortoise ORM connections closed. Shutting down Discord bot.")
        await super().close()
