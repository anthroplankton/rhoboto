from __future__ import annotations

import itertools as it
from dataclasses import dataclass
from typing import TYPE_CHECKING

import pandas as pd
from discord import ButtonStyle, Embed, Interaction, Object, Role, TextStyle
from discord.ui import Button, Modal, RoleSelect, TextInput, View

from bot import config
from components.ui_google_sheets_errors import send_google_sheets_error
from components.ui_permissions import require_settings_permissions
from utils.google_sheets_errors import GoogleSheetsError
from utils.team_register_structs import (
    SummaryWorksheetMetadata,
    TeamRegisterGoogleSheetsMetadata,
    TeamWorksheetMetadata,
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from utils.team_register_manager import TeamRegisterManager


ENCORE_ROLE_SELECT_MAX_VALUES = 25
TEAM_REGISTER_SETTINGS_MISSING_MESSAGE = (
    "Team Register settings are no longer configured for this channel."
)


@dataclass(frozen=True)
class EncoreRoleResolution:
    active_roles: tuple[Role, ...]
    missing_role_ids: tuple[int, ...]


def unique_role_ids(role_ids: Iterable[int]) -> list[int]:
    seen: set[int] = set()
    unique_ids: list[int] = []
    for role_id in role_ids:
        if role_id in seen:
            continue
        unique_ids.append(role_id)
        seen.add(role_id)
    return unique_ids


def resolve_encore_roles(
    encore_role_ids: Sequence[int],
    roles: Sequence[Role],
) -> EncoreRoleResolution:
    roles_by_id = {role.id: role for role in roles}
    active_roles: list[Role] = []
    missing_role_ids: list[int] = []

    for role_id in unique_role_ids(encore_role_ids):
        role = roles_by_id.get(role_id)
        if role is None:
            missing_role_ids.append(role_id)
        else:
            active_roles.append(role)

    return EncoreRoleResolution(tuple(active_roles), tuple(missing_role_ids))


def format_role_mentions(roles: Sequence[Role]) -> str:
    return ", ".join(f"<@&{role.id}>" for role in roles)


def format_role_ids(role_ids: Sequence[int]) -> str:
    return ", ".join(f"`{role_id}`" for role_id in role_ids)


def is_everyone_role(role: Role, guild_id: int | None) -> bool:
    if guild_id is not None and role.id == guild_id:
        return True
    is_default = getattr(role, "is_default", None)
    return bool(is_default and is_default())


def disable_view_items(view: View) -> None:
    for item in view.children:
        item.disabled = True


def build_encore_role_edit_embed(
    retained_missing_role_ids: Sequence[int],
) -> Embed:
    embed = Embed(title="Edit Encore Roles", color=config.DEFAULT_EMBED_COLOR)
    embed.description = "Choose roles to mark in the Team Register summary."
    if retained_missing_role_ids:
        embed.add_field(
            name="Missing Encore Role IDs",
            value=(
                f"{format_role_ids(retained_missing_role_ids)}\n\n"
                "These IDs are retained until removed from the preview draft."
            ),
            inline=False,
        )
    return embed


def build_encore_role_preview_embed(
    selected_roles: Sequence[Role],
    retained_missing_role_ids: Sequence[int],
    guild_id: int | None,
) -> Embed:
    embed = Embed(title="Preview Encore Role Changes", color=config.DEFAULT_EMBED_COLOR)
    embed.add_field(
        name="Selected Encore Roles",
        value=(
            format_role_mentions(selected_roles)
            if selected_roles
            else "No active encore roles selected."
        ),
        inline=False,
    )
    if retained_missing_role_ids:
        embed.add_field(
            name="Retained Missing Role IDs",
            value=format_role_ids(retained_missing_role_ids),
            inline=False,
        )
    if any(is_everyone_role(role, guild_id) for role in selected_roles):
        embed.add_field(
            name="⚠ Warnings",
            value=(
                "@everyone is selected. Every member will be marked in Google Sheets."
            ),
            inline=False,
        )
    return embed


def build_too_many_encore_roles_embed(active_role_count: int) -> Embed:
    embed = Embed(title="Cannot Edit Encore Roles", color=config.DEFAULT_EMBED_COLOR)
    embed.description = (
        "There are too many active Encore roles to preselect safely. "
        f"Discord Role Select supports at most {ENCORE_ROLE_SELECT_MAX_VALUES} "
        f"selected roles, but {active_role_count} active roles are stored."
    )
    return embed


async def send_settings_missing(interaction: Interaction) -> None:
    await interaction.response.send_message(
        TEAM_REGISTER_SETTINGS_MISSING_MESSAGE,
        ephemeral=True,
    )


async def get_fresh_team_register_config_or_respond(
    team_register_manager: TeamRegisterManager,
    interaction: Interaction,
) -> object | None:
    team_register = await team_register_manager.get_fresh_sheet_config()
    if team_register is None:
        await send_settings_missing(interaction)
        return None
    return team_register


class TeamRegisterSheetModal(Modal):
    """Modal for team register setup. Only collects user input and calls callback."""

    def __init__(
        self,
        team_register_manager: TeamRegisterManager,
        sheet_url: str = "",
        team_worksheet_titles: list[str | None] | None = None,
        summary_worksheet_title: str | None = None,
        *,
        requires_existing_settings: bool = False,
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
        self.requires_existing_settings = requires_existing_settings

    async def on_submit(self, interaction: Interaction) -> None:
        """
        Handle modal submission for Team Register setup.

        Args:
            interaction (Interaction): Discord interaction object.
        """
        if not await require_settings_permissions(interaction):
            return

        if self.requires_existing_settings:
            team_register = await get_fresh_team_register_config_or_respond(
                self.team_register_manager,
                interaction,
            )
            if team_register is None:
                return

        await interaction.response.defer(ephemeral=True)

        sheet_url = self.sheet_url.value
        team_worksheet_titles = [
            w for w in self.worksheet_titles.value.splitlines() if w
        ]
        summary_worksheet_title = self.summary_worksheet_title.value

        try:
            metadata = (
                await self.team_register_manager.upsert_sheet_config_and_worksheets(
                    sheet_url=sheet_url,
                    team_worksheet_titles=team_worksheet_titles,
                    summary_worksheet_title=summary_worksheet_title,
                )
            )
            team_register = await self.team_register_manager.get_sheet_config()
        except GoogleSheetsError as exc:
            await send_google_sheets_error(interaction, exc)
            return

        roles = list(interaction.guild.roles) if interaction.guild else []
        encore_role_ids = team_register.encore_role_ids

        embed = build_current_settings_embed(
            sheet_url=sheet_url,
            metadata=metadata,
            color=config.DEFAULT_EMBED_COLOR,
            encore_role_ids=encore_role_ids,
            roles=roles,
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
            metadata=metadata,
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
        *,
        requires_existing_settings: bool = False,
    ) -> None:
        super().__init__(label=label, style=ButtonStyle.primary)
        self.team_register_manager = team_register_manager
        self.sheet_url = sheet_url
        self.worksheet_titles = worksheet_titles
        self.summary_worksheet_title = summary_worksheet_title
        self.requires_existing_settings = requires_existing_settings

    async def callback(self, interaction: Interaction) -> None:
        if not await require_settings_permissions(interaction):
            return

        sheet_url = self.sheet_url
        if self.requires_existing_settings:
            team_register = await get_fresh_team_register_config_or_respond(
                self.team_register_manager,
                interaction,
            )
            if team_register is None:
                return
            sheet_url = team_register.sheet_url

        await interaction.response.send_modal(
            TeamRegisterSheetModal(
                team_register_manager=self.team_register_manager,
                sheet_url=sheet_url,
                team_worksheet_titles=self.worksheet_titles,
                summary_worksheet_title=self.summary_worksheet_title,
                requires_existing_settings=self.requires_existing_settings,
            )
        )


class EditEncoreRolesButton(Button):
    def __init__(
        self,
        team_register_manager: TeamRegisterManager,
        *,
        metadata: TeamRegisterGoogleSheetsMetadata,
    ) -> None:
        super().__init__(label="Edit Encore Roles", style=ButtonStyle.secondary)
        self.team_register_manager = team_register_manager
        self.metadata = metadata

    async def callback(self, interaction: Interaction) -> None:
        if not await require_settings_permissions(interaction):
            return

        team_register = await get_fresh_team_register_config_or_respond(
            self.team_register_manager,
            interaction,
        )
        if team_register is None:
            return

        roles = list(interaction.guild.roles) if interaction.guild else []
        resolution = resolve_encore_roles(team_register.encore_role_ids, roles)
        if len(resolution.active_roles) > ENCORE_ROLE_SELECT_MAX_VALUES:
            await interaction.response.edit_message(
                embed=build_too_many_encore_roles_embed(len(resolution.active_roles)),
                view=TeamRegisterView(
                    team_register_manager=self.team_register_manager,
                    has_existing_settings=True,
                    roles=roles,
                    encore_role_ids=team_register.encore_role_ids,
                    metadata=self.metadata,
                ),
            )
            return

        await interaction.response.edit_message(
            embed=build_encore_role_edit_embed(resolution.missing_role_ids),
            view=EncoreRoleEditView(
                team_register_manager=self.team_register_manager,
                metadata=self.metadata,
                roles=roles,
                encore_role_ids=team_register.encore_role_ids,
                retained_missing_role_ids=resolution.missing_role_ids,
            ),
        )


class BackToTeamSettingsButton(Button):
    def __init__(
        self,
        team_register_manager: TeamRegisterManager,
        *,
        metadata: TeamRegisterGoogleSheetsMetadata,
    ) -> None:
        super().__init__(label="Back to Settings", style=ButtonStyle.secondary)
        self.team_register_manager = team_register_manager
        self.metadata = metadata

    async def callback(self, interaction: Interaction) -> None:
        if not await require_settings_permissions(interaction):
            return

        team_register = await get_fresh_team_register_config_or_respond(
            self.team_register_manager,
            interaction,
        )
        if team_register is None:
            return

        roles = list(interaction.guild.roles) if interaction.guild else []
        await interaction.response.edit_message(
            embed=build_current_settings_embed(
                sheet_url=team_register.sheet_url,
                metadata=self.metadata,
                encore_role_ids=team_register.encore_role_ids,
                color=config.DEFAULT_EMBED_COLOR,
                roles=roles,
            ),
            view=TeamRegisterView(
                team_register_manager=self.team_register_manager,
                has_existing_settings=True,
                roles=roles,
                encore_role_ids=team_register.encore_role_ids,
                metadata=self.metadata,
            ),
        )


class EncoreRoleSelect(RoleSelect):
    def __init__(
        self,
        team_register_manager: TeamRegisterManager,
        roles: Sequence[Role] | None = None,
        encore_role_ids: Sequence[int] | None = None,
        retained_missing_role_ids: Sequence[int] | None = None,
        metadata: TeamRegisterGoogleSheetsMetadata | None = None,
    ) -> None:
        roles = roles or []
        encore_role_ids = encore_role_ids or []
        resolution = resolve_encore_roles(encore_role_ids, roles)
        super().__init__(
            placeholder="Select Encore Roles",
            min_values=0,
            max_values=ENCORE_ROLE_SELECT_MAX_VALUES,
            default_values=[Object(id=role.id) for role in resolution.active_roles],
            disabled=False,
        )
        self.team_register_manager = team_register_manager
        self.retained_missing_role_ids = tuple(
            retained_missing_role_ids
            if retained_missing_role_ids is not None
            else resolution.missing_role_ids
        )
        self.metadata = metadata

    async def callback(self, interaction: Interaction) -> None:
        if not await require_settings_permissions(interaction):
            return

        team_register = await get_fresh_team_register_config_or_respond(
            self.team_register_manager,
            interaction,
        )
        if team_register is None:
            return

        selected_roles = list(self.values)
        await interaction.response.edit_message(
            embed=build_encore_role_preview_embed(
                selected_roles=selected_roles,
                retained_missing_role_ids=self.retained_missing_role_ids,
                guild_id=interaction.guild.id if interaction.guild else None,
            ),
            view=EncoreRolePreviewView(
                team_register_manager=self.team_register_manager,
                selected_roles=selected_roles,
                retained_missing_role_ids=self.retained_missing_role_ids,
                metadata=self.metadata,
            ),
        )


class EncoreRoleEditView(View):
    def __init__(
        self,
        team_register_manager: TeamRegisterManager,
        *,
        metadata: TeamRegisterGoogleSheetsMetadata,
        roles: Sequence[Role],
        encore_role_ids: Sequence[int],
        retained_missing_role_ids: Sequence[int],
    ) -> None:
        super().__init__(timeout=None)
        self.team_register_manager = team_register_manager
        self.metadata = metadata
        self.active_roles = resolve_encore_roles(encore_role_ids, roles).active_roles
        self.retained_missing_role_ids = tuple(retained_missing_role_ids)
        self.add_item(
            EncoreRoleSelect(
                team_register_manager,
                roles=roles,
                encore_role_ids=encore_role_ids,
                retained_missing_role_ids=retained_missing_role_ids,
                metadata=metadata,
            )
        )
        if self.retained_missing_role_ids:
            self.add_item(RemoveMissingFromDraftButton())
        self.add_item(
            BackToTeamSettingsButton(
                team_register_manager,
                metadata=metadata,
            )
        )


class ConfirmEncoreRolesButton(Button):
    def __init__(self) -> None:
        super().__init__(label="Confirm Save", style=ButtonStyle.success)

    async def callback(self, interaction: Interaction) -> None:
        view = self.view
        if not isinstance(view, EncoreRolePreviewView):
            return
        if not await require_settings_permissions(interaction):
            return

        role_ids = unique_role_ids(
            [role.id for role in view.selected_roles]
            + list(view.retained_missing_role_ids)
        )
        team_register = await get_fresh_team_register_config_or_respond(
            view.team_register_manager,
            interaction,
        )
        if team_register is None:
            return
        await view.team_register_manager.update_encore_role_ids_record(role_ids)

        roles = list(interaction.guild.roles) if interaction.guild else []
        try:
            metadata = await view.team_register_manager.fetch_google_sheets_metadata()
        except GoogleSheetsError as exc:
            await interaction.response.edit_message(
                content=(
                    "Encore roles saved, but the settings view could not be "
                    f"refreshed. Google Sheets could not complete this action. "
                    f"{exc.user_message}"
                ),
                embed=None,
                view=None,
            )
            return

        await interaction.response.edit_message(
            content=None,
            embed=build_current_settings_embed(
                sheet_url=team_register.sheet_url,
                metadata=metadata,
                encore_role_ids=role_ids,
                color=config.DEFAULT_EMBED_COLOR,
                roles=roles,
                is_save_action=True,
            ),
            view=TeamRegisterView(
                team_register_manager=view.team_register_manager,
                has_existing_settings=True,
                roles=roles,
                encore_role_ids=role_ids,
                metadata=metadata,
            ),
        )


class CancelEncoreRolesButton(Button):
    def __init__(self) -> None:
        super().__init__(label="Cancel", style=ButtonStyle.secondary)

    async def callback(self, interaction: Interaction) -> None:
        view = self.view
        if not isinstance(view, EncoreRolePreviewView):
            return
        if not await require_settings_permissions(interaction):
            return

        disable_view_items(view)
        await interaction.response.edit_message(
            content="Cancelled. No changes saved.",
            embed=build_encore_role_preview_embed(
                selected_roles=view.selected_roles,
                retained_missing_role_ids=view.retained_missing_role_ids,
                guild_id=interaction.guild.id if interaction.guild else None,
            ),
            view=view,
        )
        view.stop()


class RemoveMissingFromDraftButton(Button):
    def __init__(self) -> None:
        super().__init__(label="Remove Missing From Draft", style=ButtonStyle.danger)

    async def callback(self, interaction: Interaction) -> None:
        view = self.view
        if not isinstance(view, (EncoreRoleEditView, EncoreRolePreviewView)):
            return
        if not await require_settings_permissions(interaction):
            return

        team_register = await get_fresh_team_register_config_or_respond(
            view.team_register_manager,
            interaction,
        )
        if team_register is None:
            return

        selected_roles = (
            view.active_roles
            if isinstance(view, EncoreRoleEditView)
            else view.selected_roles
        )
        updated_view = EncoreRolePreviewView(
            team_register_manager=view.team_register_manager,
            selected_roles=selected_roles,
            retained_missing_role_ids=(),
            metadata=view.metadata,
        )
        await interaction.response.edit_message(
            content=None,
            embed=build_encore_role_preview_embed(
                selected_roles=selected_roles,
                retained_missing_role_ids=(),
                guild_id=interaction.guild.id if interaction.guild else None,
            ),
            view=updated_view,
        )


class EncoreRolePreviewView(View):
    def __init__(
        self,
        team_register_manager: TeamRegisterManager,
        *,
        selected_roles: Sequence[Role],
        retained_missing_role_ids: Sequence[int],
        metadata: TeamRegisterGoogleSheetsMetadata,
    ) -> None:
        super().__init__(timeout=None)
        self.team_register_manager = team_register_manager
        self.selected_roles = tuple(selected_roles)
        self.retained_missing_role_ids = tuple(retained_missing_role_ids)
        self.metadata = metadata
        self.add_item(ConfirmEncoreRolesButton())
        self.add_item(CancelEncoreRolesButton())
        if self.retained_missing_role_ids:
            self.add_item(RemoveMissingFromDraftButton())


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
        metadata: TeamRegisterGoogleSheetsMetadata | None = None,
    ) -> None:
        super().__init__(timeout=None)
        del roles, encore_role_ids
        if metadata is not None:
            sheet_url = sheet_url or metadata.sheet_url
            team_worksheet_titles = team_worksheet_titles or [
                ws.title for ws in metadata.team_worksheets
            ]
            summary_worksheet_title = (
                summary_worksheet_title or metadata.summary_worksheet.title
            )
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
            requires_existing_settings=has_existing_settings,
        )
        self.add_item(button)
        if not has_existing_settings:
            return
        if metadata is not None:
            self.add_item(
                EditEncoreRolesButton(
                    team_register_manager,
                    metadata=metadata,
                )
            )


def build_current_settings_embed(
    sheet_url: str,
    metadata: TeamRegisterGoogleSheetsMetadata,
    encore_role_ids: list[int],
    color: int,
    *,
    roles: Sequence[Role] | None = None,
    is_save_action: bool = False,
) -> Embed:
    """
    Build an embed showing the current team register settings.

    Args:
        sheet_url (str): The Google Sheet link.
        metadata (TeamRegisterGoogleSheetsMetadata): Google Sheets worksheet metadata.
        encore_role_ids (list[int]): Stored Encore role IDs.
        color (int): Embed color.
        roles (Sequence[Role] | None): Current guild roles for resolving role IDs.
        is_save_action (bool):
            If True, this embed follows a settings save action.

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

    role_resolution = resolve_encore_roles(encore_role_ids, roles or [])
    if role_resolution.active_roles:
        encore_roles_value = format_role_mentions(role_resolution.active_roles)
    else:
        encore_roles_value = "No active encore roles configured."
    embed.add_field(
        name="Encore Roles",
        value=encore_roles_value,
        inline=False,
    )
    if role_resolution.missing_role_ids:
        embed.add_field(
            name="Missing Encore Role IDs",
            value=(
                f"{format_role_ids(role_resolution.missing_role_ids)}\n\n"
                "These IDs are retained until removed in Edit Encore Roles."
            ),
            inline=False,
        )

    embed.set_footer(
        text=(
            "Use Edit Encore Roles to configure encore role markers. "
            "Use Edit Team Register Settings to edit sheet settings. "
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
                row.drop(["display_name", "encore_roles"]), n=2, strict=False
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
