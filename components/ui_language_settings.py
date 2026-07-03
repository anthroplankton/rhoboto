from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from discord import ButtonStyle, Embed, SelectOption
from discord.ui import Button, Select
from tortoise.exceptions import DBConnectionError, IntegrityError, OperationalError

from bot import config
from components.ui_permissions import require_settings_permissions
from components.ui_settings_flow import (
    SettingsPanel,
    SettingsTimeoutView,
    disable_view_items,
    prepare_replacement_settings_view,
)
from utils.announcement_languages import (
    DEFAULT_ANNOUNCEMENT_LANGUAGES,
    SUPPORTED_ANNOUNCEMENT_LANGUAGE_LABELS,
    normalize_announcement_languages,
    save_announcement_languages,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from discord import Interaction


LANGUAGE_REQUIRED_MESSAGE = "At least one announcement language is required."
LANGUAGE_SAVE_ERROR_MESSAGE = (
    "Could not save announcement language settings. Try again later."
)

logger = logging.getLogger(__name__)


@dataclass
class AnnouncementLanguageDraft:
    language_codes: list[str]

    @classmethod
    def from_saved(cls, language_codes: Sequence[str]) -> AnnouncementLanguageDraft:
        return cls(normalize_announcement_languages(language_codes))

    @property
    def remaining_language_codes(self) -> list[str]:
        return [
            code
            for code in SUPPORTED_ANNOUNCEMENT_LANGUAGE_LABELS
            if code not in self.language_codes
        ]

    def add_language(self, language_code: str) -> None:
        if (
            language_code in SUPPORTED_ANNOUNCEMENT_LANGUAGE_LABELS
            and language_code not in self.language_codes
        ):
            self.language_codes.append(language_code)

    def remove_language(self, language_code: str) -> bool:
        if language_code not in self.language_codes or len(self.language_codes) <= 1:
            return False
        self.language_codes.remove(language_code)
        return True

    def reset(self) -> None:
        self.language_codes = list(DEFAULT_ANNOUNCEMENT_LANGUAGES)


def _language_order_lines(language_codes: Sequence[str]) -> list[str]:
    return [
        f"{index}. {SUPPORTED_ANNOUNCEMENT_LANGUAGE_LABELS[language_code]}"
        for index, language_code in enumerate(
            normalize_announcement_languages(language_codes),
            start=1,
        )
    ]


def build_announcement_languages_embed(
    language_codes: Sequence[str],
    *,
    is_save_action: bool = False,
) -> Embed:
    title = (
        "Announcement Language Settings Saved"
        if is_save_action
        else "Announcement Language Settings"
    )
    embed = Embed(title=title, color=config.DEFAULT_EMBED_COLOR)
    embed.description = "\n".join(
        [
            *_language_order_lines(language_codes),
            "",
            (
                "Each selected language sends one public announcement message, "
                "in this order."
            ),
        ]
    )
    return embed


class AnnouncementLanguageSettingsView(SettingsTimeoutView):
    def __init__(
        self,
        guild_id: int,
        language_codes: Sequence[str],
        *,
        saved_language_codes: Sequence[str] | None = None,
    ) -> None:
        super().__init__()
        self.guild_id = guild_id
        self.saved_language_codes = normalize_announcement_languages(
            language_codes if saved_language_codes is None else saved_language_codes
        )
        self.draft = AnnouncementLanguageDraft.from_saved(language_codes)

        if self.draft.remaining_language_codes:
            self.add_item(AddAnnouncementLanguageSelect(self))
        self.add_item(RemoveAnnouncementLanguageSelect(self))
        self.add_item(ResetAnnouncementLanguagesButton(self))
        self.add_item(SaveAnnouncementLanguagesButton(self))
        self.add_item(CancelAnnouncementLanguagesButton(self))

    async def edit_interaction(
        self,
        interaction: Interaction,
        *,
        is_save_action: bool = False,
    ) -> None:
        saved_language_codes = (
            self.draft.language_codes if is_save_action else self.saved_language_codes
        )
        panel = build_announcement_language_settings_panel(
            self.guild_id,
            self.draft.language_codes,
            is_save_action=is_save_action,
            saved_language_codes=saved_language_codes,
        )
        prepare_replacement_settings_view(self, panel.view)
        await interaction.response.edit_message(embed=panel.embed, view=panel.view)

    def build_disabled_saved_panel(self) -> SettingsPanel:
        panel = build_announcement_language_settings_panel(
            self.guild_id,
            self.saved_language_codes,
            saved_language_codes=self.saved_language_codes,
        )
        panel.view.message = self.message
        disable_view_items(panel.view)
        panel.view.stop()
        return panel

    def build_timeout_edit_kwargs(self) -> dict[str, object]:
        panel = self.build_disabled_saved_panel()
        return {"embed": panel.embed, "view": panel.view}


class AddAnnouncementLanguageSelect(Select):
    def __init__(self, parent_view: AnnouncementLanguageSettingsView) -> None:
        self.parent_view = parent_view
        options = [
            SelectOption(
                label=SUPPORTED_ANNOUNCEMENT_LANGUAGE_LABELS[code],
                value=code,
            )
            for code in parent_view.draft.remaining_language_codes
        ]
        super().__init__(
            placeholder="Add Language",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: Interaction) -> None:
        if not await require_settings_permissions(interaction):
            return
        self.parent_view.draft.add_language(self.values[0])
        await self.parent_view.edit_interaction(interaction)


class RemoveAnnouncementLanguageSelect(Select):
    def __init__(self, parent_view: AnnouncementLanguageSettingsView) -> None:
        self.parent_view = parent_view
        options = [
            SelectOption(
                label=SUPPORTED_ANNOUNCEMENT_LANGUAGE_LABELS[code],
                value=code,
            )
            for code in parent_view.draft.language_codes
        ]
        super().__init__(
            placeholder="Remove Language",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: Interaction) -> None:
        if not await require_settings_permissions(interaction):
            return
        if not self.parent_view.draft.remove_language(self.values[0]):
            await interaction.response.send_message(
                LANGUAGE_REQUIRED_MESSAGE,
                ephemeral=True,
            )
            return
        await self.parent_view.edit_interaction(interaction)


class ResetAnnouncementLanguagesButton(Button):
    def __init__(self, parent_view: AnnouncementLanguageSettingsView) -> None:
        self.parent_view = parent_view
        super().__init__(label="Reset to Default", style=ButtonStyle.secondary)

    async def callback(self, interaction: Interaction) -> None:
        if not await require_settings_permissions(interaction):
            return
        self.parent_view.draft.reset()
        await self.parent_view.edit_interaction(interaction)


class SaveAnnouncementLanguagesButton(Button):
    def __init__(self, parent_view: AnnouncementLanguageSettingsView) -> None:
        self.parent_view = parent_view
        super().__init__(label="Save", style=ButtonStyle.success)

    async def callback(self, interaction: Interaction) -> None:
        if not await require_settings_permissions(interaction):
            return
        try:
            await save_announcement_languages(
                self.parent_view.guild_id,
                self.parent_view.draft.language_codes,
                logger,
            )
        except (DBConnectionError, IntegrityError, OperationalError):
            logger.exception(
                "Failed to save announcement languages for guild `%s`.",
                self.parent_view.guild_id,
            )
            await interaction.response.send_message(
                LANGUAGE_SAVE_ERROR_MESSAGE,
                ephemeral=True,
            )
            return
        await self.parent_view.edit_interaction(interaction, is_save_action=True)


class CancelAnnouncementLanguagesButton(Button):
    def __init__(self, parent_view: AnnouncementLanguageSettingsView) -> None:
        self.parent_view = parent_view
        super().__init__(label="Cancel", style=ButtonStyle.secondary)

    async def callback(self, interaction: Interaction) -> None:
        if not await require_settings_permissions(interaction):
            return
        panel = self.parent_view.build_disabled_saved_panel()
        prepare_replacement_settings_view(self.parent_view, panel.view)
        await interaction.response.edit_message(embed=panel.embed, view=panel.view)


def build_announcement_language_settings_panel(
    guild_id: int,
    language_codes: Sequence[str],
    *,
    is_save_action: bool = False,
    saved_language_codes: Sequence[str] | None = None,
) -> SettingsPanel:
    embed = build_announcement_languages_embed(
        language_codes,
        is_save_action=is_save_action,
    )
    view = AnnouncementLanguageSettingsView(
        guild_id,
        language_codes,
        saved_language_codes=saved_language_codes,
    )
    return SettingsPanel(embed=embed, view=view)
