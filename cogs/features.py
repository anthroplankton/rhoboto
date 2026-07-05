from discord import Embed, Interaction, app_commands
from discord.ext import commands

from bot import Rhoboto, config
from cogs.base.discord_context import require_guild_channel_source
from models.feature_channel import FeatureChannel


class Features(commands.Cog):
    """Query all enabled features in this channel."""

    @app_commands.command(
        name="features",
        description="View all features and whether they are enabled in this channel",
    )
    async def features(self, interaction: Interaction) -> None:
        source = require_guild_channel_source(
            interaction,
            action="proceed with features command",
        )
        feature_channel = await FeatureChannel.filter(
            guild_id=source.guild.id,
            channel_id=source.channel.id,
        ).all()
        embed = Embed(
            title="Features in This Channel", color=config.DEFAULT_EMBED_COLOR
        )
        if feature_channel:
            lines = []
            for f in feature_channel:
                status = r"\🟢 enabled" if f.is_enabled else r"\⚪ disabled"
                lines.append(f"- `{f.feature_name}`: {status}")
            embed.description = "\n".join(lines)
        else:
            embed.description = "No features are registered in this channel."
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: Rhoboto) -> None:
    await bot.add_cog(Features(bot))
