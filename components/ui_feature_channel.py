import discord
from discord import ButtonStyle, Interaction
from discord.ui import Button, View


class DisableAndClearConfirmView(View):
    """
    A Discord UI view for confirming disable and clear actions.

    Presents Confirm and Cancel buttons to the user. Sets self.value to True if
    confirmed, False if cancelled, or None if timed out.
    """

    def __init__(self, timeout: float = 20.0) -> None:
        super().__init__(timeout=timeout)
        self.value: bool | None = None

    @discord.ui.button(label="Confirm", style=ButtonStyle.danger)
    async def confirm(self, interaction: Interaction, _: Button) -> None:
        self.value = True
        await interaction.response.edit_message(
            content="Confirmed. Clearing settings...",
            view=None,
        )
        self.stop()

    @discord.ui.button(label="Cancel", style=ButtonStyle.secondary)
    async def cancel(self, interaction: Interaction, _: Button) -> None:
        self.value = False
        await interaction.response.edit_message(
            content="Operation cancelled.",
            view=None,
        )
        self.stop()
