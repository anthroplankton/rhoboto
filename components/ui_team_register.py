from __future__ import annotations

import itertools as it
from typing import TYPE_CHECKING

import pandas as pd
from discord import ButtonStyle, Embed, Interaction, Role, SelectOption, TextStyle
from discord.ui import Button, Modal, Select, TextInput, View

from bot import config
from utils.team_register_structs import (
    SummaryWorksheetMetadata,
    TeamRegisterGoogleSheetsMetadata,
    TeamWorksheetMetadata,
)

if TYPE_CHECKING:
    from utils.team_register_manager import TeamRegisterManager


class TeamRegisterSheetModal(Modal):
    """Modal for team register setup. Only collects user input and calls callback."""

    def __init__(
        self,
        team_register_manager: TeamRegisterManager,
        sheet_url: str = "",
        team_worksheet_titles: list[str | None] | None = None,
        summary_worksheet_title: str | None = None,
    ) -> None:
        super().__init__(title="Team Register Setup")
        team_worksheet_titles = team_worksheet_titles or [
            *it.islice(TeamWorksheetMetadata.default_title_generator(), 3)
        ]
        summary_worksheet_title = summary_worksheet_title or next(
            SummaryWorksheetMetadata.default_title_generator()
        )

        self.sheet_url: TextInput = TextInput(
            label="Google Sheet Link",
            placeholder="Paste your Google Sheet URL here",
            default=sheet_url,
            required=True,
            style=TextStyle.short,
        )
        self.worksheet_titles: TextInput = TextInput(
            label="Worksheet Titles",
            placeholder=(
                "Optional. One title per line. "
                "Leave blank to use default titles. "
                "Missing worksheets will be created."
            ),
            default="\n".join(w or "" for w in team_worksheet_titles),
            required=False,
            style=TextStyle.paragraph,
        )
        self.summary_worksheet_title: TextInput = TextInput(
            label="Summary Worksheet Title",
            placeholder=(
                "Optional. Title for the summary worksheet. "
                "Leave blank to use the default title."
            ),
            default=summary_worksheet_title,
            required=False,
            style=TextStyle.short,
        )
        self.add_item(self.sheet_url)
        self.add_item(self.worksheet_titles)
        self.add_item(self.summary_worksheet_title)
        self.team_register_manager = team_register_manager

    async def on_submit(self, interaction: Interaction) -> None:
        """
        Handle modal submission for Team Register setup.

        Args:
            interaction (Interaction): Discord interaction object.
        """
        await interaction.response.defer(ephemeral=True)

        sheet_url = self.sheet_url.value
        team_worksheet_titles = [
            w for w in self.worksheet_titles.value.splitlines() if w
        ]
        summary_worksheet_title = self.summary_worksheet_title.value

        metadata = await self.team_register_manager.upsert_sheet_config_and_worksheets(
            sheet_url=sheet_url,
            team_worksheet_titles=team_worksheet_titles,
            summary_worksheet_title=summary_worksheet_title,
        )

        team_register = await self.team_register_manager.get_sheet_config()

        roles = list(interaction.guild.roles) if interaction.guild else []
        encore_role_ids = team_register.encore_role_ids

        embed = build_current_settings_embed(
            sheet_url=sheet_url,
            metadata=metadata,
            color=config.DEFAULT_EMBED_COLOR,
            encore_role_ids=encore_role_ids,
            is_save_action=True,
        )

        view = TeamRegisterView(
            team_register_manager=self.team_register_manager,
            has_existing_settings=True,
            sheet_url=sheet_url,
            team_worksheet_titles=[ws.title for ws in metadata.team_worksheets],
            summary_worksheet_title=metadata.summary_worksheet.title,
            roles=roles,
            encore_role_ids=encore_role_ids,
        )

        await interaction.followup.send(embed=embed, view=view, ephemeral=True)


class TeamRegisterButton(Button):
    """Dynamic button for team register setup/edit."""

    def __init__(
        self,
        label: str,
        team_register_manager: TeamRegisterManager,
        sheet_url: str = "",
        worksheet_titles: list[str | None] | None = None,
        summary_worksheet_title: str | None = None,
    ) -> None:
        super().__init__(label=label, style=ButtonStyle.primary)
        self.team_register_manager = team_register_manager
        self.sheet_url = sheet_url
        self.worksheet_titles = worksheet_titles
        self.summary_worksheet_title = summary_worksheet_title

    async def callback(self, interaction: Interaction) -> None:
        await interaction.response.send_modal(
            TeamRegisterSheetModal(
                team_register_manager=self.team_register_manager,
                sheet_url=self.sheet_url,
                team_worksheet_titles=self.worksheet_titles,
                summary_worksheet_title=self.summary_worksheet_title,
            )
        )


class EncoreRoleMultiSelect(Select):
    def __init__(
        self,
        team_register_manager: TeamRegisterManager,
        roles: list[Role] | None = None,
        encore_role_ids: list[int] | None = None,
    ) -> None:
        roles = roles or []
        encore_role_ids = encore_role_ids or []
        roles = [r for r in roles if not r.is_default() and not r.managed]
        options = [
            SelectOption(
                label=role.name,
                value=str(role.id),
                default=role.id in encore_role_ids,
            )
            for role in roles
        ]
        super().__init__(
            placeholder="🔧 Select Encore Roles",
            options=options,
            min_values=0,
            max_values=min(25, len(options)),
            disabled=not bool(options),
        )
        self.team_register_manager = team_register_manager

    async def callback(self, interaction: Interaction) -> None:
        role_ids = [int(role_id) for role_id in self.values]
        if interaction.guild is not None:
            roles = [interaction.guild.get_role(rid) for rid in role_ids]
        else:
            roles = []

        valid_roles = [role for role in roles if role is not None]
        await self.team_register_manager.update_encore_roles_record(valid_roles)

        if valid_roles:
            selected_roles = ", ".join(
                [f"<@&{role.id}>" for role in valid_roles if role]
            )
            await interaction.response.send_message(
                f"Encore roles updated: {selected_roles}", ephemeral=True
            )
        else:
            await interaction.response.send_message(
                "Encore roles cleared. No encore roles are set.", ephemeral=True
            )


class TeamRegisterView(View):
    """View for team register setup/edit button."""

    def __init__(
        self,
        team_register_manager: TeamRegisterManager,
        *,
        has_existing_settings: bool = False,
        sheet_url: str = "",
        team_worksheet_titles: list[str | None] | None = None,
        summary_worksheet_title: str | None = None,
        roles: list[Role] | None = None,
        encore_role_ids: list[int] | None = None,
    ) -> None:
        super().__init__(timeout=None)
        label = (
            "Edit Team Register Settings"
            if has_existing_settings
            else "Setup Team Register"
        )
        button = TeamRegisterButton(
            label=label,
            team_register_manager=team_register_manager,
            sheet_url=sheet_url,
            worksheet_titles=team_worksheet_titles,
            summary_worksheet_title=summary_worksheet_title,
        )
        self.add_item(button)
        if not has_existing_settings:
            return
        select = EncoreRoleMultiSelect(
            roles=roles,
            team_register_manager=team_register_manager,
            encore_role_ids=encore_role_ids,
        )
        self.add_item(select)


def build_current_settings_embed(
    sheet_url: str,
    metadata: TeamRegisterGoogleSheetsMetadata,
    encore_role_ids: list[int],
    color: int,
    *,
    is_save_action: bool = False,
) -> Embed:
    """
    Build an embed showing the current team register settings.

    Args:
        sheet_url (str): The Google Sheet link.
        team_register_data (TeamRegisterData): The team register data object.
        color (int): Embed color.
        is_settings_query (bool):
            If True, this is a settings query (view), otherwise a settings save.

    Returns:
        Embed: The constructed embed.
    """
    if is_save_action:
        title = "✅ Team Register Settings Saved!"
    else:
        title = "📃 Team Register Settings"
    embed = Embed(title=title, color=color)

    sheet_url_row = f"**Link** -> {sheet_url}"
    embed.add_field(name="Google Sheet", value=sheet_url_row, inline=False)

    worksheet_rows = [
        f"- `{ws.title or '**Not Found**'}` : `{ws.id}`"
        for ws in metadata.team_worksheets
    ]
    embed.add_field(
        name="Worksheets & IDs",
        value=(
            "\n".join(worksheet_rows) if worksheet_rows else "No worksheets configured."
        ),
        inline=False,
    )

    summary_worksheet_row = (
        f"- `{metadata.summary_worksheet.title or '**Not Found**'}` : "
        f"`{metadata.summary_worksheet.id}`"
    )
    embed.add_field(
        name="Summary Worksheet & ID", value=summary_worksheet_row, inline=False
    )

    # Build Encore Roles field with explicit instructions
    # (only select menu, not settings button)
    if encore_role_ids:
        encore_roles_value = (
            f"{', '.join([f'<@&{rid}>' for rid in encore_role_ids])}\n\n"
            "You can update encore roles using the select menu below."
        )
    else:
        encore_roles_value = (
            "No encore roles configured.\n\n"
            "To add encore roles, use the select menu below."
        )
    embed.add_field(
        name="Encore Roles",
        value=encore_roles_value,
        inline=False,
    )

    embed.set_footer(
        text=(
            "You can configure encore roles using the select menu. "
            "To edit sheet settings, use the settings button. "
            "To add more worksheet titles, run this command again and "
            "enter all previous worksheet titles plus any new ones."
        )
    )
    return embed


def build_summary_embed(summary_dataframe: pd.DataFrame) -> Embed:
    embed = Embed(
        title="📊 Team Register Summary",
        color=config.DEFAULT_EMBED_COLOR,
    )
    if summary_dataframe.empty:
        embed.description = "No summary data available."
        return embed

    display_name_lines = []
    team_lines = []
    encore_roles_lines = []
    for _, row in summary_dataframe.iterrows():
        display_name = str(row["display_name"])
        if not display_name:
            continue
        pairs = [
            f"`{value:.0f}/{power:.1f}`"
            for value, power in it.batched(
                row.drop(["display_name", "encore_roles"]), n=2
            )
            if pd.notna(value) and pd.notna(power)
        ]
        encore_roles = str(row["encore_roles"])
        display_name_lines.append(f"`{display_name}`")
        team_lines.append(" ".join(pairs))
        encore_roles_lines.append(f"`{encore_roles}`" if encore_roles else "")

    if display_name_lines and team_lines and encore_roles_lines:
        embed.add_field(
            name="Display Name",
            value="\n".join(display_name_lines),
            inline=True,
        )
        embed.add_field(
            name="Teams",
            value="\n".join(team_lines),
            inline=True,
        )
        embed.add_field(
            name="Encore Roles",
            value="\n".join(encore_roles_lines),
            inline=True,
        )

    return embed
