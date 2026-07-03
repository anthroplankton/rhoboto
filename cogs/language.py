from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from discord import Permissions, app_commands
from discord.ext import commands

from components.ui_language_settings import (
    build_announcement_language_settings_panel,
)
from components.ui_permissions import require_settings_permissions
from components.ui_settings_flow import send_current_panel_followup
from utils.announcement_languages import get_announcement_languages

if TYPE_CHECKING:
    from discord import Interaction

    from bot import Rhoboto


class Language(commands.Cog):
    """Guild-level language settings."""

    language_group = app_commands.Group(
        name="language",
        description="Configure language settings.",
        guild_only=True,
        default_permissions=Permissions(administrator=True, manage_channels=True),
    )
    settings_group = app_commands.Group(
        name="settings",
        description="Configure language settings.",
        parent=language_group,
    )

    def __init__(self, bot: Rhoboto) -> None:
        self.bot = bot
        self.logger = logging.getLogger(self.__class__.__name__)

    @settings_group.command(
        name="announcement",
        description="Configure public announcement languages for this server.",
    )
    async def announcement(self, interaction: Interaction) -> None:
        if interaction.guild is None:
            msg = "Interaction guild is None. Cannot configure language settings."
            raise ValueError(msg)
        if not await require_settings_permissions(interaction):
            return

        await interaction.response.defer(ephemeral=True)
        language_codes = await get_announcement_languages(
            interaction.guild.id,
            self.logger,
        )
        panel = build_announcement_language_settings_panel(
            interaction.guild.id,
            language_codes,
        )
        await send_current_panel_followup(interaction, panel)


async def setup(bot: Rhoboto) -> None:
    await bot.add_cog(Language(bot))
