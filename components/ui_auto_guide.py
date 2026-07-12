from __future__ import annotations

from typing import TYPE_CHECKING, Final, Protocol

from discord import ButtonStyle, Interaction
from discord.ui import Button, View

from components.ui_permissions import require_settings_permissions

if TYPE_CHECKING:
    from collections.abc import Sequence


LATEST_GUIDE_FIELD_NAME = "Latest Guide Message"
LATEST_GUIDE_ENABLED_STATUS_VALUE = (
    r"\🟢 `Enabled` : A short guide is automatically kept near the newest messages. "
    "When a full guide announcement exists, the short guide replies to it."
)
LATEST_GUIDE_DISABLED_STATUS_VALUE = (
    r"\⚫ `Disabled` : No short guide is maintained near new messages. Enable this to "
    "keep registration rules visible as the channel moves."
)
LATEST_GUIDE_ENABLE_REFRESH_FAILED_WARNING = (
    "Latest Guide Message is enabled, but the latest guide could not be sent. "
    "Check bot permissions and try again."
)
LATEST_GUIDE_SETTINGS_REFRESH_FAILED_WARNING = (
    "Settings were saved, but Latest Guide Message could not be refreshed. "
    "Check bot permissions and try again."
)
AUTO_GUIDE_DELETE_CUSTOM_ID_PREFIX: Final = "rhoboto:auto_guide:delete:"
AUTO_GUIDE_BUTTON_VIEW_TIMEOUT_SECONDS: Final[float] = 180.0
SUPPORTED_AUTO_GUIDE_BUTTON_LANGUAGES: Final = frozenset({"en", "ja", "zh_tw"})

AUTO_GUIDE_DELETE_LABELS: Final[dict[str, dict[str, str]]] = {
    "team_register": {
        "en": "Delete Your Teams",
        "zh_tw": "刪除我的編成",
        "ja": "自分の編成を削除",
    },
    "shift_register": {
        "en": "Delete Your Shift",
        "zh_tw": "刪除我的班表",
        "ja": "自分のシフトを削除",
    },
}
AUTO_GUIDE_FULL_GUIDE_LABELS: Final[dict[str, str]] = {
    "en": "Full Guide",
    "zh_tw": "完整說明",
    "ja": "詳しい使い方",
}
AUTO_GUIDE_GOOGLE_SHEETS_LABEL: Final = "Google Sheets"


class LatestGuideToggleCallback(Protocol):
    async def __call__(
        self,
        interaction: Interaction,
        *,
        enabled: bool,
        current_view: View | None,
    ) -> None: ...


class LatestGuideStateResolver(Protocol):
    async def __call__(self) -> bool: ...


class LatestGuideRefreshCallback(Protocol):
    async def __call__(
        self,
        interaction: Interaction,
        feature_config: object,
    ) -> bool: ...


class AutoGuideDeleteCallback(Protocol):
    async def __call__(self, interaction: Interaction) -> None: ...


def auto_guide_delete_custom_id(feature_name: str) -> str:
    return f"{AUTO_GUIDE_DELETE_CUSTOM_ID_PREFIX}{feature_name}"


def discord_message_url(
    *,
    guild_id: int,
    channel_id: int,
    message_id: int,
) -> str:
    return f"https://discord.com/channels/{guild_id}/{channel_id}/{message_id}"


def auto_guide_button_language(languages: Sequence[str]) -> str:
    return next(
        (
            language
            for language in languages
            if language in SUPPORTED_AUTO_GUIDE_BUTTON_LANGUAGES
        ),
        "en",
    )


def _auto_guide_delete_label(feature_name: str, language: str) -> str:
    labels = AUTO_GUIDE_DELETE_LABELS.get(
        feature_name,
        AUTO_GUIDE_DELETE_LABELS["team_register"],
    )
    return labels.get(language, labels["en"])


def latest_guide_status_value(*, enabled: bool) -> str:
    return (
        LATEST_GUIDE_ENABLED_STATUS_VALUE
        if enabled
        else LATEST_GUIDE_DISABLED_STATUS_VALUE
    )


async def resolve_latest_guide_enabled(
    *,
    enabled: bool,
    state_resolver: LatestGuideStateResolver | None,
) -> bool:
    if state_resolver is None:
        return enabled
    return await state_resolver()


async def refresh_latest_guide_after_settings_save(
    interaction: Interaction,
    feature_config: object,
    refresh_callback: LatestGuideRefreshCallback | None,
) -> None:
    if refresh_callback is None:
        return
    if await refresh_callback(interaction, feature_config):
        return
    await interaction.followup.send(
        LATEST_GUIDE_SETTINGS_REFRESH_FAILED_WARNING,
        ephemeral=True,
    )


class AutoGuideDeleteButton(Button):
    def __init__(
        self,
        *,
        feature_name: str,
        language: str,
        delete_callback: AutoGuideDeleteCallback,
    ) -> None:
        super().__init__(
            label=_auto_guide_delete_label(feature_name, language),
            emoji="🗑️",
            style=ButtonStyle.danger,
            custom_id=auto_guide_delete_custom_id(feature_name),
        )
        self.delete_callback = delete_callback

    async def callback(self, interaction: Interaction) -> None:
        await self.delete_callback(interaction)


class AutoGuideButtonsView(View):
    def __init__(  # noqa: PLR0913
        self,
        *,
        feature_name: str,
        language: str,
        delete_callback: AutoGuideDeleteCallback,
        sheet_url: str | None,
        full_guide_url: str | None = None,
        timeout: float | None = AUTO_GUIDE_BUTTON_VIEW_TIMEOUT_SECONDS,
        delete_only: bool = False,
    ) -> None:
        super().__init__(timeout=timeout)
        self.add_item(
            AutoGuideDeleteButton(
                feature_name=feature_name,
                language=language,
                delete_callback=delete_callback,
            )
        )
        if delete_only:
            return
        if full_guide_url is not None:
            self.add_item(
                Button(
                    label=AUTO_GUIDE_FULL_GUIDE_LABELS.get(
                        language,
                        AUTO_GUIDE_FULL_GUIDE_LABELS["en"],
                    ),
                    emoji="⤴️",
                    style=ButtonStyle.link,
                    url=full_guide_url,
                )
            )
        if sheet_url is not None:
            self.add_item(
                Button(
                    label=AUTO_GUIDE_GOOGLE_SHEETS_LABEL,
                    emoji="👀",
                    style=ButtonStyle.link,
                    url=sheet_url,
                )
            )


class LatestGuideButton(Button):
    def __init__(
        self,
        *,
        enabled: bool,
        toggle_callback: LatestGuideToggleCallback,
    ) -> None:
        super().__init__(
            label="Disable Latest Guide" if enabled else "Enable Latest Guide",
            style=ButtonStyle.secondary if enabled else ButtonStyle.primary,
        )
        self.enabled = enabled
        self.toggle_callback = toggle_callback

    async def callback(self, interaction: Interaction) -> None:
        if not await require_settings_permissions(interaction):
            return

        await interaction.response.defer()
        await self.toggle_callback(
            interaction,
            enabled=not self.enabled,
            current_view=self.view,
        )
