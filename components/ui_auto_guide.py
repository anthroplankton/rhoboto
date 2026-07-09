from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from discord import ButtonStyle, Interaction
from discord.ui import Button

from components.ui_permissions import require_settings_permissions

if TYPE_CHECKING:
    from discord.ui import View


LATEST_GUIDE_FIELD_NAME = "Latest Guide Message"
LATEST_GUIDE_ENABLED_STATUS_VALUE = (
    "`Enabled` : A short guide is automatically kept near the newest messages. "
    "When a full guide announcement exists, the short guide replies to it."
)
LATEST_GUIDE_DISABLED_STATUS_VALUE = (
    "`disabled` : No short guide is maintained near new messages. Enable this to "
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

        await self.toggle_callback(
            interaction,
            enabled=not self.enabled,
            current_view=self.view,
        )
