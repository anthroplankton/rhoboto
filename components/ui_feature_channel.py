import discord
from discord import ButtonStyle, Interaction
from discord.ui import Button, View

from components.ui_permissions import require_settings_permissions


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
        if not await require_settings_permissions(interaction):
            self.value = False
            self.stop()
            return

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


class ConfirmDeleteUserDataView(View):
    """A confirmation view for deleting the requesting user's register data."""

    def __init__(  # noqa: PLR0913
        self,
        *,
        requesting_user_id: int,
        confirm_label: str,
        cancel_label: str,
        in_progress_message: str,
        cancelled_message: str,
        unauthorized_message: str,
        timeout: float = 20.0,
    ) -> None:
        super().__init__(timeout=timeout)
        self.requesting_user_id = requesting_user_id
        self.value: bool | None = None
        self.in_progress_message = in_progress_message
        self.cancelled_message = cancelled_message
        self.unauthorized_message = unauthorized_message
        self.add_item(ConfirmDeleteUserDataButton(confirm_label))
        self.add_item(CancelDeleteUserDataButton(cancel_label))


class ConfirmDeleteUserDataButton(Button):
    def __init__(self, label: str) -> None:
        super().__init__(label=label, style=ButtonStyle.danger)

    async def callback(self, interaction: Interaction) -> None:
        view = self.view
        if not isinstance(view, ConfirmDeleteUserDataView):
            return
        if interaction.user.id != view.requesting_user_id:
            await interaction.response.send_message(
                view.unauthorized_message,
                ephemeral=True,
            )
            return

        view.value = True
        await interaction.response.edit_message(
            content=view.in_progress_message,
            view=None,
        )
        view.stop()


class CancelDeleteUserDataButton(Button):
    def __init__(self, label: str) -> None:
        super().__init__(label=label, style=ButtonStyle.secondary)

    async def callback(self, interaction: Interaction) -> None:
        view = self.view
        if not isinstance(view, ConfirmDeleteUserDataView):
            return

        view.value = False
        await interaction.response.edit_message(
            content=view.cancelled_message,
            view=None,
        )
        view.stop()
