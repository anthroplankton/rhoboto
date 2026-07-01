from __future__ import annotations

from typing import TYPE_CHECKING

from discord import ButtonStyle, Embed, Interaction, TextStyle
from discord.ui import Button, Modal, TextInput, View

from bot import config
from components.ui_google_sheets_errors import send_google_sheets_error
from components.ui_permissions import require_settings_permissions
from components.ui_settings_flow import (
    SettingsPanel,
    send_stale_setup_panel_if_configured,
    settings_description,
    settings_title,
)
from utils.google_sheets_errors import GoogleSheetsError
from utils.shift_register_structs import (
    DraftWorksheetMetadata,
    EntryWorksheetMetadata,
    FinalScheduleWorksheetMetadata,
    ShiftRegisterGoogleSheetsMetadata,
)

if TYPE_CHECKING:
    from utils.shift_register_manager import ShiftRegisterManager


SHIFT_REGISTER_DISPLAY_NAME = "Shift Register"
SHIFT_REGISTER_CURRENT_CONTROLS = "Use the button below to update sheet settings."
SHIFT_REGISTER_SAVED_CONTROLS = "Use the button below to edit sheet settings."
SHIFT_REGISTER_SETTINGS_MISSING_MESSAGE = (
    "Shift Register settings are no longer configured for this channel."
)


async def send_shift_settings_missing(interaction: Interaction) -> None:
    await interaction.response.send_message(
        SHIFT_REGISTER_SETTINGS_MISSING_MESSAGE,
        ephemeral=True,
    )


async def get_fresh_shift_register_config_or_respond(
    shift_register_manager: ShiftRegisterManager,
    interaction: Interaction,
) -> object | None:
    shift_register = await shift_register_manager.get_fresh_sheet_config()
    if shift_register is None:
        await send_shift_settings_missing(interaction)
        return None
    return shift_register


async def build_shift_register_settings_panel(
    shift_register_manager: ShiftRegisterManager,
    shift_register: object,
    *,
    is_save_action: bool = False,
    metadata: ShiftRegisterGoogleSheetsMetadata | None = None,
) -> SettingsPanel:
    active_metadata = (
        metadata or await shift_register_manager.fetch_google_sheets_metadata()
    )
    final_schedule_anchor_cell = getattr(
        shift_register,
        "final_schedule_anchor_cell",
        "A1",
    )
    embed = build_current_settings_embed(
        sheet_url=shift_register.sheet_url,
        metadata=active_metadata,
        final_schedule_anchor_cell=final_schedule_anchor_cell,
        color=config.DEFAULT_EMBED_COLOR,
        is_save_action=is_save_action,
    )
    view = ShiftRegisterView(
        shift_register_manager=shift_register_manager,
        has_existing_settings=True,
        sheet_url=shift_register.sheet_url,
        entry_worksheet_title=active_metadata.entry_worksheets.title,
        draft_worksheet_title=active_metadata.draft_worksheet.title,
        final_schedule_worksheet_title=active_metadata.final_schedule_worksheet.title,
        final_schedule_anchor_cell=final_schedule_anchor_cell,
    )
    return SettingsPanel(embed=embed, view=view)


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
        *,
        requires_existing_settings: bool = False,
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
                "e.g. A1. Will be used to anchor the final schedule in the worksheet."
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
        self.requires_existing_settings = requires_existing_settings

    async def on_submit(self, interaction: Interaction) -> None:
        """
        Handle modal submission for Shift Register setup.

        Args:
            interaction (Interaction): Discord interaction object.
        """
        if not await require_settings_permissions(interaction):
            return

        if self.requires_existing_settings:
            shift_register = await get_fresh_shift_register_config_or_respond(
                self.shift_register_manager,
                interaction,
            )
            if shift_register is None:
                return

        await interaction.response.defer(ephemeral=True)

        sheet_url = self.sheet_url.value
        entry_worksheet_title = self.entry_worksheet_title.value
        draft_worksheet_title = self.draft_worksheet_title.value
        final_schedule_worksheet_title = self.final_schedule_worksheet_title.value
        final_schedule_anchor_cell = self.final_schedule_anchor_cell.value

        try:
            metadata = (
                await self.shift_register_manager.upsert_sheet_config_and_worksheets(
                    sheet_url=sheet_url,
                    entry_worksheet_title=entry_worksheet_title,
                    draft_worksheet_title=draft_worksheet_title,
                    final_schedule_worksheet_title=final_schedule_worksheet_title,
                )
            )
        except GoogleSheetsError as exc:
            await send_google_sheets_error(interaction, exc)
            return
        await self.shift_register_manager.update_final_schedule_anchor_cell(
            final_schedule_anchor_cell
        )

        shift_register = await self.shift_register_manager.get_sheet_config()
        panel = await build_shift_register_settings_panel(
            self.shift_register_manager,
            shift_register,
            is_save_action=True,
            metadata=metadata,
        )

        await interaction.followup.send(
            embed=panel.embed,
            view=panel.view,
            ephemeral=True,
        )


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
        *,
        requires_existing_settings: bool = False,
    ) -> None:
        super().__init__(label=label, style=ButtonStyle.primary)
        self.shift_register_manager = shift_register_manager
        self.sheet_url = sheet_url
        self.entry_worksheet_title = entry_worksheet_title
        self.draft_worksheet_title = draft_worksheet_title
        self.final_schedule_worksheet_title = final_schedule_worksheet_title
        self.final_schedule_anchor_cell = final_schedule_anchor_cell
        self.requires_existing_settings = requires_existing_settings

    async def callback(self, interaction: Interaction) -> None:
        if not await require_settings_permissions(interaction):
            return

        async def build_current_panel(shift_register: object) -> SettingsPanel:
            return await build_shift_register_settings_panel(
                self.shift_register_manager,
                shift_register,
            )

        if not self.requires_existing_settings:
            handled = await send_stale_setup_panel_if_configured(
                interaction,
                self.shift_register_manager,
                feature_display_name=SHIFT_REGISTER_DISPLAY_NAME,
                build_current_panel=build_current_panel,
            )
            if handled:
                return

        if self.requires_existing_settings:
            shift_register = await get_fresh_shift_register_config_or_respond(
                self.shift_register_manager,
                interaction,
            )
            if shift_register is None:
                return
            sheet_url = shift_register.sheet_url
            entry_worksheet_title = self.entry_worksheet_title
            draft_worksheet_title = self.draft_worksheet_title
            final_schedule_worksheet_title = self.final_schedule_worksheet_title
            final_schedule_anchor_cell = getattr(
                shift_register,
                "final_schedule_anchor_cell",
                self.final_schedule_anchor_cell,
            )
        else:
            sheet_url = self.sheet_url
            entry_worksheet_title = self.entry_worksheet_title
            draft_worksheet_title = self.draft_worksheet_title
            final_schedule_worksheet_title = self.final_schedule_worksheet_title
            final_schedule_anchor_cell = self.final_schedule_anchor_cell

        await interaction.response.send_modal(
            ShiftRegisterSheetModal(
                shift_register_manager=self.shift_register_manager,
                sheet_url=sheet_url,
                entry_worksheet_title=entry_worksheet_title,
                draft_worksheet_title=draft_worksheet_title,
                final_schedule_worksheet_title=final_schedule_worksheet_title,
                final_schedule_anchor_cell=final_schedule_anchor_cell,
                requires_existing_settings=self.requires_existing_settings,
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
            requires_existing_settings=has_existing_settings,
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
    embed = Embed(
        title=settings_title(
            SHIFT_REGISTER_DISPLAY_NAME,
            is_save_action=is_save_action,
        ),
        color=color,
    )
    embed.description = settings_description(
        SHIFT_REGISTER_DISPLAY_NAME,
        (
            SHIFT_REGISTER_SAVED_CONTROLS
            if is_save_action
            else SHIFT_REGISTER_CURRENT_CONTROLS
        ),
        is_save_action=is_save_action,
    )

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

    return embed
