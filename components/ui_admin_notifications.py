from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from discord import ButtonStyle, Embed, Interaction, Member, Role, TextStyle, User
from discord.ui import Button, MentionableSelect, Modal, TextInput

from bot import config
from components.ui_permissions import require_settings_permissions
from components.ui_settings_flow import (
    SettingsPanel,
    SettingsTimeoutView,
    settings_description,
    settings_title,
)
from utils.admin_notifications import (
    MentionResolution,
    parse_reminder_lead_minutes,
    saved_mention_defaults,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence
    from datetime import datetime

    from discord import TextChannel
    from discord.ui import View


ADMIN_NOTIFICATIONS_FEATURE_NAME = "admin_notifications"
ADMIN_NOTIFICATIONS_DISPLAY_NAME = "Admin Notifications"
SETUP_BUTTON_LABEL = "Set Up Admin Notifications"
SETUP_MODAL_TITLE = "Set Up Admin Notifications"
EDIT_LEAD_MODAL_TITLE = "Edit Reminder Lead Time"
LEAD_INPUT_LABEL = "Lead Time (minutes)"
LEAD_INPUT_PLACEHOLDER = "1–1440"  # noqa: RUF001
MENTION_SELECT_PLACEHOLDER = "Select roles or users to mention"
STALE_SETTINGS_MESSAGE = (
    "Admin Notifications settings changed while this panel was open. "
    "Reopen settings and try again."
)
NOT_CONFIGURED_MESSAGE = (
    "Admin Notifications settings are no longer configured for this channel."
)
UNAUTHORIZED_REQUESTER_MESSAGE = (
    "Only the administrator who opened this setup can use it."
)


def lead_time_error_message() -> str:
    return (
        f"⚠️ {config.CONFUSED_EMOJI} Lead Time must be a whole number from "
        "1 to 1440 minutes. No settings were changed."
    )


def mention_selection_error_message() -> str:
    return (
        f"⚠️ {config.CONFUSED_EMOJI} The selected roles cannot currently be "
        "mentioned. Make them mentionable and try again; no settings were changed."
    )


@dataclass(frozen=True)
class AdminNotificationsUIActions:
    setup_is_current: Callable[[int, datetime], Awaitable[bool]]
    save_setup: Callable[[Interaction, int, datetime, str], Awaitable[None]]
    replace_destination: Callable[[Interaction, int, int], Awaitable[None]]
    save_lead: Callable[
        [Interaction, int, datetime, int | None, str, View], Awaitable[None]
    ]
    save_mentions: Callable[
        [
            Interaction,
            int,
            datetime,
            list[int],
            list[int],
            Sequence[Role | Member | User],
            View,
        ],
        Awaitable[None],
    ]
    set_shift_reminders: Callable[
        [Interaction, int, datetime, bool, bool, View], Awaitable[None]
    ]


class AdminNotificationsSetupView(SettingsTimeoutView):
    def __init__(
        self,
        *,
        requesting_user_id: int,
        config_id: int,
        expected_updated_at: datetime,
        actions: AdminNotificationsUIActions,
        expected_channel_id: int | None = None,
    ) -> None:
        super().__init__()
        self.requesting_user_id = requesting_user_id
        self.config_id = config_id
        self.expected_updated_at = expected_updated_at
        self.expected_channel_id = expected_channel_id
        self.actions = actions
        self.add_item(AdminNotificationsSetupButton())


class AdminNotificationsSetupButton(Button):
    def __init__(self) -> None:
        super().__init__(label=SETUP_BUTTON_LABEL, style=ButtonStyle.primary)

    async def callback(self, interaction: Interaction) -> None:
        view = self.view
        if not isinstance(view, AdminNotificationsSetupView):
            return
        if interaction.user.id != view.requesting_user_id:
            await interaction.response.send_message(
                UNAUTHORIZED_REQUESTER_MESSAGE,
                ephemeral=True,
            )
            return
        if not await require_settings_permissions(interaction):
            return
        if (
            view.expected_channel_id is not None
            and getattr(interaction.channel, "id", None) != view.expected_channel_id
        ):
            await interaction.response.send_message(
                STALE_SETTINGS_MESSAGE,
                ephemeral=True,
            )
            return
        if not await view.actions.setup_is_current(
            view.config_id,
            view.expected_updated_at,
        ):
            await interaction.response.send_message(
                STALE_SETTINGS_MESSAGE,
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(
            AdminNotificationsLeadModal(
                config_id=view.config_id,
                expected_updated_at=view.expected_updated_at,
                actions=view.actions,
                is_setup=True,
            )
        )


class AdminNotificationsLeadModal(Modal):
    def __init__(  # noqa: PLR0913
        self,
        *,
        config_id: int,
        expected_updated_at: datetime,
        actions: AdminNotificationsUIActions,
        expected_lead: int | None = None,
        current_view: View | None = None,
        is_setup: bool = False,
    ) -> None:
        super().__init__(title=SETUP_MODAL_TITLE if is_setup else EDIT_LEAD_MODAL_TITLE)
        self.config_id = config_id
        self.expected_updated_at = expected_updated_at
        self.expected_lead = expected_lead
        self.actions = actions
        self.current_view = current_view
        self.is_setup = is_setup
        self.lead_input = TextInput(
            label=LEAD_INPUT_LABEL,
            placeholder=LEAD_INPUT_PLACEHOLDER,
            default="10" if is_setup else str(expected_lead),
            style=TextStyle.short,
            required=True,
        )
        self.add_item(self.lead_input)

    async def on_submit(self, interaction: Interaction) -> None:
        if not await require_settings_permissions(interaction):
            return
        raw_value = self.lead_input.value
        try:
            parse_reminder_lead_minutes(raw_value)
        except ValueError:
            await interaction.response.send_message(
                lead_time_error_message(),
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        if self.is_setup:
            await self.actions.save_setup(
                interaction,
                self.config_id,
                self.expected_updated_at,
                raw_value,
            )
            return
        if self.current_view is None:
            return
        await self.actions.save_lead(
            interaction,
            self.config_id,
            self.expected_updated_at,
            self.expected_lead,
            raw_value,
            self.current_view,
        )


class ReplaceAdminNotificationsDestinationView(SettingsTimeoutView):
    def __init__(
        self,
        *,
        requesting_user_id: int,
        config_id: int,
        expected_channel_id: int,
        actions: AdminNotificationsUIActions,
    ) -> None:
        super().__init__(timeout=20.0)
        self.requesting_user_id = requesting_user_id
        self.config_id = config_id
        self.expected_channel_id = expected_channel_id
        self.actions = actions
        self.add_item(ReplaceAdminNotificationsDestinationButton())
        self.add_item(CancelAdminNotificationsDestinationReplacementButton())


class ReplaceAdminNotificationsDestinationButton(Button):
    def __init__(self) -> None:
        super().__init__(label="Replace Channel", style=ButtonStyle.danger)

    async def callback(self, interaction: Interaction) -> None:
        view = self.view
        if not isinstance(view, ReplaceAdminNotificationsDestinationView):
            return
        if interaction.user.id != view.requesting_user_id:
            await interaction.response.send_message(
                UNAUTHORIZED_REQUESTER_MESSAGE,
                ephemeral=True,
            )
            return
        if not await require_settings_permissions(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        await view.actions.replace_destination(
            interaction,
            view.config_id,
            view.expected_channel_id,
        )
        view.stop()


class CancelAdminNotificationsDestinationReplacementButton(Button):
    def __init__(self) -> None:
        super().__init__(label="Cancel", style=ButtonStyle.secondary)

    async def callback(self, interaction: Interaction) -> None:
        view = self.view
        if not isinstance(view, ReplaceAdminNotificationsDestinationView):
            return
        if interaction.user.id != view.requesting_user_id:
            await interaction.response.send_message(
                UNAUTHORIZED_REQUESTER_MESSAGE,
                ephemeral=True,
            )
            return
        if not await require_settings_permissions(interaction):
            return
        await interaction.response.edit_message(
            content="Operation cancelled.",
            view=None,
        )
        view.stop()


def _mention_field_value(mentions: MentionResolution) -> str:
    return mentions.mention_line or "None"


def _missing_mentions_value(mentions: MentionResolution) -> str:
    values = [
        *(f"Role `{role_id}`" for role_id in mentions.missing_role_ids),
        *(f"User `{user_id}`" for user_id in mentions.missing_user_ids),
    ]
    return "\n".join(values) if values else "None"


def _unmentionable_roles_value(mentions: MentionResolution) -> str:
    return " ".join(role.mention for role in mentions.unmentionable_roles) or "None"


class AdminNotificationsSettingsView(SettingsTimeoutView):
    def __init__(
        self,
        *,
        config: object,
        actions: AdminNotificationsUIActions,
    ) -> None:
        super().__init__()
        self.config = config
        self.actions = actions
        self.add_item(
            AdminNotificationMentionableSelect(
                role_ids=config.mention_role_ids,
                user_ids=config.mention_user_ids,
            )
        )
        self.add_item(EditAdminNotificationLeadTimeButton())
        self.add_item(
            ToggleShiftTimelineRemindersButton(
                enabled=config.shift_timeline_reminders_enabled
            )
        )


class AdminNotificationMentionableSelect(MentionableSelect):
    def __init__(self, *, role_ids: Sequence[int], user_ids: Sequence[int]) -> None:
        super().__init__(
            placeholder=MENTION_SELECT_PLACEHOLDER,
            min_values=0,
            max_values=25,
            row=0,
            default_values=saved_mention_defaults(role_ids, user_ids),
        )

    async def callback(self, interaction: Interaction) -> None:
        view = self.view
        if not isinstance(view, AdminNotificationsSettingsView):
            return
        if not await require_settings_permissions(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        await view.actions.save_mentions(
            interaction,
            view.config.id,
            view.config.updated_at,
            list(view.config.mention_role_ids),
            list(view.config.mention_user_ids),
            self.values,
            view,
        )


class EditAdminNotificationLeadTimeButton(Button):
    def __init__(self) -> None:
        super().__init__(label="Edit Lead Time", style=ButtonStyle.secondary, row=1)

    async def callback(self, interaction: Interaction) -> None:
        view = self.view
        if not isinstance(view, AdminNotificationsSettingsView):
            return
        if not await require_settings_permissions(interaction):
            return
        await interaction.response.send_modal(
            AdminNotificationsLeadModal(
                config_id=view.config.id,
                expected_updated_at=view.config.updated_at,
                expected_lead=view.config.reminder_lead_minutes,
                actions=view.actions,
                current_view=view,
            )
        )


class ToggleShiftTimelineRemindersButton(Button):
    def __init__(self, *, enabled: bool) -> None:
        super().__init__(
            label=(
                "Disable Shift Timeline Reminders"
                if enabled
                else "Enable Shift Timeline Reminders"
            ),
            style=ButtonStyle.secondary if enabled else ButtonStyle.primary,
            row=1,
        )
        self.enabled = enabled

    async def callback(self, interaction: Interaction) -> None:
        view = self.view
        if not isinstance(view, AdminNotificationsSettingsView):
            return
        if not await require_settings_permissions(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        await view.actions.set_shift_reminders(
            interaction,
            view.config.id,
            view.config.updated_at,
            self.enabled,
            not self.enabled,
            view,
        )


def build_admin_notifications_settings_panel(  # noqa: PLR0913
    config: object,
    *,
    destination: TextChannel,
    mentions: MentionResolution,
    scheduled_reminder_count: int,
    actions: AdminNotificationsUIActions,
    is_save_action: bool = False,
) -> SettingsPanel:
    controls_description = (
        "Select mentions or use the buttons below to update reminders."
    )
    embed = Embed(
        title=settings_title(
            ADMIN_NOTIFICATIONS_DISPLAY_NAME,
            is_save_action=is_save_action,
        ),
        description=settings_description(
            ADMIN_NOTIFICATIONS_DISPLAY_NAME,
            controls_description,
            is_save_action=is_save_action,
        ),
    )
    embed.add_field(
        name="Notification Channel", value=destination.mention, inline=False
    )
    embed.add_field(
        name="Lead Time",
        value=f"{config.reminder_lead_minutes} minutes before each milestone",
        inline=False,
    )
    embed.add_field(name="Mentions", value=_mention_field_value(mentions), inline=False)
    embed.add_field(
        name="Missing Mentions",
        value=_missing_mentions_value(mentions),
        inline=False,
    )
    embed.add_field(
        name="Unmentionable Roles",
        value=_unmentionable_roles_value(mentions),
        inline=False,
    )
    embed.add_field(
        name="Shift Timeline Reminders",
        value=(
            "🟢 Enabled" if config.shift_timeline_reminders_enabled else "⚫ Disabled"
        ),
        inline=False,
    )
    embed.add_field(
        name="Scheduled Reminders",
        value=str(scheduled_reminder_count),
        inline=False,
    )
    return SettingsPanel(
        embed=embed,
        view=AdminNotificationsSettingsView(config=config, actions=actions),
    )
