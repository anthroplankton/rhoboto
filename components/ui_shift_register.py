from __future__ import annotations

from typing import TYPE_CHECKING

from discord import ButtonStyle, Embed, Interaction, TextStyle
from discord.ui import Button, Modal, TextInput, View

from bot import config
from utils.shift_register_structs import (
    DraftWorksheetMetadata,
    EntryWorksheetMetadata,
    FinalScheduleWorksheetMetadata,
    ShiftRegisterGoogleSheetsMetadata,
)

if TYPE_CHECKING:
    from utils.shift_register_manager import ShiftRegisterManager


class ShiftRegisterSheetModal(Modal):
    """Modal for shift register setup. Only collects user input and calls callback."""

    def __init__(
        self,
        shift_register_manager: ShiftRegisterManager,
        sheet_url: str = "",
        entry_worksheet_title: str | None = None,
        draft_worksheet_title: str | None = None,
        final_schedule_worksheet_title: str | None = None,
        final_schedule_anchor_cell: str = "A1",
    ) -> None:
        super().__init__(title="Shift Register Setup")
        entry_worksheet_title = entry_worksheet_title or next(
            EntryWorksheetMetadata.default_title_generator()
        )
        draft_worksheet_title = draft_worksheet_title or next(
            DraftWorksheetMetadata.default_title_generator()
        )
        final_schedule_worksheet_title = final_schedule_worksheet_title or next(
            FinalScheduleWorksheetMetadata.default_title_generator()
        )

        self.sheet_url: TextInput = TextInput(
            label="Google Sheet Link",
            placeholder="Paste your Google Sheet URL here",
            default=sheet_url,
            required=True,
            style=TextStyle.short,
        )
        self.entry_worksheet_title: TextInput = TextInput(
            label="Entry Worksheet Title",
            placeholder="Title for the entry worksheet",
            default=entry_worksheet_title,
            required=True,
            style=TextStyle.short,
        )
        self.draft_worksheet_title: TextInput = TextInput(
            label="Draft Worksheet Title",
            placeholder="Title for the draft worksheet",
            default=draft_worksheet_title,
            required=True,
            style=TextStyle.short,
        )
        self.final_schedule_worksheet_title: TextInput = TextInput(
            label="Final Schedule Worksheet Title",
            placeholder="Title for the final schedule worksheet",
            default=final_schedule_worksheet_title,
            required=True,
            style=TextStyle.short,
        )
        self.final_schedule_anchor_cell: TextInput = TextInput(
            label="Final Schedule Anchor Cell",
            placeholder=(
                "e.g. A1. "
                "Will be used to anchor the final schedule in the worksheet."
            ),
            default=final_schedule_anchor_cell,
            required=True,
            style=TextStyle.short,
        )
        self.add_item(self.sheet_url)
        self.add_item(self.entry_worksheet_title)
        self.add_item(self.draft_worksheet_title)
        self.add_item(self.final_schedule_worksheet_title)
        self.add_item(self.final_schedule_anchor_cell)
        self.shift_register_manager = shift_register_manager

    async def on_submit(self, interaction: Interaction) -> None:
        """
        Handle modal submission for Shift Register setup.

        Args:
            interaction (Interaction): Discord interaction object.
        """
        await interaction.response.defer(ephemeral=True)

        sheet_url = self.sheet_url.value
        entry_worksheet_title = self.entry_worksheet_title.value
        draft_worksheet_title = self.draft_worksheet_title.value
        final_schedule_worksheet_title = self.final_schedule_worksheet_title.value
        final_schedule_anchor_cell = self.final_schedule_anchor_cell.value

        metadata = await self.shift_register_manager.upsert_sheet_config_and_worksheets(
            sheet_url=sheet_url,
            entry_worksheet_title=entry_worksheet_title,
            draft_worksheet_title=draft_worksheet_title,
            final_schedule_worksheet_title=final_schedule_worksheet_title,
        )
        await self.shift_register_manager.update_final_schedule_anchor_cell(
            final_schedule_anchor_cell
        )

        embed = build_current_settings_embed(
            sheet_url=sheet_url,
            metadata=metadata,
            final_schedule_anchor_cell=final_schedule_anchor_cell,
            color=config.DEFAULT_EMBED_COLOR,
            is_save_action=True,
        )

        view = ShiftRegisterView(
            shift_register_manager=self.shift_register_manager,
            has_existing_settings=True,
            sheet_url=sheet_url,
            entry_worksheet_title=entry_worksheet_title,
            draft_worksheet_title=draft_worksheet_title,
            final_schedule_worksheet_title=final_schedule_worksheet_title,
            final_schedule_anchor_cell=final_schedule_anchor_cell,
        )

        await interaction.followup.send(embed=embed, view=view, ephemeral=True)


class ShiftRegisterButton(Button):
    """Dynamic button for shift register setup/edit."""

    def __init__(
        self,
        label: str,
        shift_register_manager: ShiftRegisterManager,
        sheet_url: str = "",
        entry_worksheet_title: str | None = None,
        draft_worksheet_title: str | None = None,
        final_schedule_worksheet_title: str | None = None,
        final_schedule_anchor_cell: str = "A1",
    ) -> None:
        super().__init__(label=label, style=ButtonStyle.primary)
        self.shift_register_manager = shift_register_manager
        self.sheet_url = sheet_url
        self.entry_worksheet_title = entry_worksheet_title
        self.draft_worksheet_title = draft_worksheet_title
        self.final_schedule_worksheet_title = final_schedule_worksheet_title
        self.final_schedule_anchor_cell = final_schedule_anchor_cell

    async def callback(self, interaction: Interaction) -> None:
        await interaction.response.send_modal(
            ShiftRegisterSheetModal(
                shift_register_manager=self.shift_register_manager,
                sheet_url=self.sheet_url,
                entry_worksheet_title=self.entry_worksheet_title,
                draft_worksheet_title=self.draft_worksheet_title,
                final_schedule_worksheet_title=self.final_schedule_worksheet_title,
                final_schedule_anchor_cell=self.final_schedule_anchor_cell,
            )
        )


class ShiftRegisterView(View):
    """View for shift register setup/edit button."""

    def __init__(
        self,
        shift_register_manager: ShiftRegisterManager,
        *,
        has_existing_settings: bool = False,
        sheet_url: str = "",
        entry_worksheet_title: str | None = None,
        draft_worksheet_title: str | None = None,
        final_schedule_worksheet_title: str | None = None,
        final_schedule_anchor_cell: str = "A1",
    ) -> None:
        super().__init__(timeout=None)
        label = (
            "Edit Shift Register Settings"
            if has_existing_settings
            else "Setup Shift Register"
        )
        button = ShiftRegisterButton(
            label=label,
            shift_register_manager=shift_register_manager,
            sheet_url=sheet_url,
            entry_worksheet_title=entry_worksheet_title,
            draft_worksheet_title=draft_worksheet_title,
            final_schedule_worksheet_title=final_schedule_worksheet_title,
            final_schedule_anchor_cell=final_schedule_anchor_cell,
        )
        self.add_item(button)


def build_current_settings_embed(
    sheet_url: str,
    metadata: ShiftRegisterGoogleSheetsMetadata,
    final_schedule_anchor_cell: str,
    color: int,
    *,
    is_save_action: bool = False,
) -> Embed:
    """
    Build an embed showing the current shift register settings.

    Args:
        sheet_url (str): The Google Sheet link.
        metadata (ShiftRegisterGoogleSheetsMetadata):
            The shift register metadata object.
        final_schedule_anchor_cell (str):
            The anchor cell for the final schedule worksheet.
        color (int): Embed color.
        is_save_action (bool):
            If True, this is a settings save, otherwise a settings query (view).

    Returns:
        Embed: The constructed embed.
    """
    if is_save_action:
        title = "✅ Shift Register Settings Saved!"
    else:
        title = "📃 Shift Register Settings"
    embed = Embed(title=title, color=color)

    sheet_url_row = f"**Link** -> {sheet_url}"
    embed.add_field(name="Google Sheet", value=sheet_url_row, inline=False)

    worksheet_rows = [
        f"- **Entry** -> `{metadata.entry_worksheets.title or '**Not Found**'}` : "
        f"`{metadata.entry_worksheets.id}`",
        f"- **Draft** -> `{metadata.draft_worksheet.title or '**Not Found**'}` : "
        f"`{metadata.draft_worksheet.id}`",
        f"- **Final Schedule** -> "
        f"`{metadata.final_schedule_worksheet.title or '**Not Found**'}` : "
        f"`{metadata.final_schedule_worksheet.id}`",
    ]
    embed.add_field(
        name="Worksheets & IDs",
        value="\n".join(worksheet_rows),
        inline=False,
    )

    embed.add_field(
        name="Final Schedule Anchor Cell",
        value=f"`{final_schedule_anchor_cell}`",
        inline=False,
    )

    embed.set_footer(text="To edit sheet settings, use the settings button.")
    return embed
