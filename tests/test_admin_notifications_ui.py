from __future__ import annotations

# ruff: noqa: E501
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from discord import Embed
from discord.ui import MentionableSelect, Modal

from components.ui_admin_notifications import (
    AdminNotificationMentionableSelect,
    AdminNotificationsLeadModal,
    AdminNotificationsSettingsView,
    AdminNotificationsSetupView,
    AdminNotificationsUIActions,
    CancelAdminNotificationsDestinationReplacementButton,
    EditAdminNotificationLeadTimeButton,
    ReplaceAdminNotificationsDestinationButton,
    ReplaceAdminNotificationsDestinationView,
    ToggleShiftTimelineRemindersButton,
    build_admin_notifications_settings_panel,
    lead_time_error_message,
)
from tests.fakes import FakeInteraction
from utils.admin_notifications import MentionResolution

EXPECTED_UPDATED_AT = datetime(2026, 8, 13, tzinfo=UTC)


def _actions() -> AdminNotificationsUIActions:
    return AdminNotificationsUIActions(
        setup_is_current=AsyncMock(return_value=True),
        save_setup=AsyncMock(),
        replace_destination=AsyncMock(),
        save_lead=AsyncMock(),
        save_mentions=AsyncMock(),
        set_shift_reminders=AsyncMock(),
    )


def _config() -> SimpleNamespace:
    return SimpleNamespace(
        id=10,
        updated_at=EXPECTED_UPDATED_AT,
        reminder_lead_minutes=10,
        mention_role_ids=[101],
        mention_user_ids=[201],
        shift_timeline_reminders_enabled=False,
        feature_channel_id=99,
    )


def _resolution() -> MentionResolution:
    return MentionResolution(
        active_roles=(SimpleNamespace(id=101, mention="<@&101>"),),
        active_users=(SimpleNamespace(id=201, mention="<@201>"),),
        missing_role_ids=(102,),
        missing_user_ids=(202,),
        unmentionable_roles=(SimpleNamespace(id=103, mention="<@&103>"),),
    )


@pytest.mark.asyncio
async def test_setup_view_button_modal_copy_and_default_ten() -> None:
    actions = _actions()
    view = AdminNotificationsSetupView(
        requesting_user_id=333,
        config_id=10,
        expected_updated_at=EXPECTED_UPDATED_AT,
        actions=actions,
    )
    interaction = FakeInteraction(user_id=333)

    await view.children[0].callback(interaction)

    assert len(interaction.response.modals) == 1
    modal = interaction.response.modals[0]
    assert isinstance(modal, AdminNotificationsLeadModal)
    assert modal.title == "Set Up Admin Notifications"
    assert modal.lead_input.label == "Lead Time (minutes)"
    assert modal.lead_input.default == "10"


@pytest.mark.asyncio
async def test_setup_button_rejects_wrong_requester_permission_loss_and_stale_config() -> (
    None
):
    actions = _actions()
    view = AdminNotificationsSetupView(
        requesting_user_id=333,
        config_id=10,
        expected_updated_at=EXPECTED_UPDATED_AT,
        actions=actions,
    )

    wrong_user = FakeInteraction(user_id=444)
    await view.children[0].callback(wrong_user)
    assert wrong_user.response.messages[0][0] == (
        "Only the administrator who opened this setup can use it."
    )
    actions.setup_is_current.assert_not_awaited()

    denied = FakeInteraction(user_id=333, administrator=False)
    await view.children[0].callback(denied)
    assert denied.response.messages
    actions.setup_is_current.assert_not_awaited()

    actions.setup_is_current.return_value = False
    stale = FakeInteraction(user_id=333)
    await view.children[0].callback(stale)
    assert "settings changed" in stale.response.messages[0][0]
    actions.setup_is_current.assert_awaited_once_with(10, EXPECTED_UPDATED_AT)


@pytest.mark.asyncio
async def test_replacement_view_copy_labels_cancel_and_requester_guard() -> None:
    actions = _actions()
    view = ReplaceAdminNotificationsDestinationView(
        requesting_user_id=333,
        config_id=10,
        expected_channel_id=222,
        actions=actions,
    )
    assert [item.label for item in view.children] == [
        "Replace Channel",
        "Cancel",
    ]
    assert isinstance(view.children[0], ReplaceAdminNotificationsDestinationButton)
    assert isinstance(
        view.children[1], CancelAdminNotificationsDestinationReplacementButton
    )

    wrong_user = FakeInteraction(user_id=444)
    await view.children[0].callback(wrong_user)
    assert actions.replace_destination.await_count == 0

    cancel = FakeInteraction(user_id=333)
    await view.children[1].callback(cancel)
    assert cancel.response.edits[0][0] == "Operation cancelled."
    assert actions.replace_destination.await_count == 0


def test_settings_embed_has_exact_fields_status_and_saved_title() -> None:
    destination = SimpleNamespace(mention="<#222>")
    panel = build_admin_notifications_settings_panel(
        _config(),
        destination=destination,
        mentions=_resolution(),
        scheduled_reminder_count=3,
        actions=_actions(),
    )
    assert isinstance(panel.embed, Embed)
    assert panel.embed.title == "Admin Notifications Settings"
    assert panel.embed.description == (
        "Admin Notifications is configured for this channel. "
        "Select mentions or use the buttons below to update reminders."
    )
    assert [field.name for field in panel.embed.fields] == [
        "Notification Channel",
        "Lead Time",
        "Mentions",
        "Missing Mentions",
        "Unmentionable Roles",
        "Shift Timeline Reminders",
        "Scheduled Reminders",
    ]
    assert panel.embed.fields[-1].value == "3"

    saved = build_admin_notifications_settings_panel(
        _config(),
        destination=destination,
        mentions=_resolution(),
        scheduled_reminder_count=3,
        actions=_actions(),
        is_save_action=True,
    )
    assert saved.embed.title == "Admin Notifications Settings Saved"


def test_settings_view_has_two_rows_and_typed_twenty_five_value_defaults() -> None:
    panel = build_admin_notifications_settings_panel(
        _config(),
        destination=SimpleNamespace(mention="<#222>"),
        mentions=_resolution(),
        scheduled_reminder_count=0,
        actions=_actions(),
    )
    assert isinstance(panel.view, AdminNotificationsSettingsView)
    assert isinstance(panel.view.children[0], MentionableSelect)
    assert isinstance(panel.view.children[0], AdminNotificationMentionableSelect)
    assert panel.view.children[0].row == 0
    assert panel.view.children[0].min_values == 0
    assert panel.view.children[0].max_values == 25
    assert len(panel.view.children[0].default_values) == 2
    assert [item.id for item in panel.view.children[0].default_values] == [101, 201]
    assert [item.row for item in panel.view.children[1:]] == [1, 1]


@pytest.mark.asyncio
async def test_lead_modal_prefills_saved_value_and_routes_valid_input() -> None:
    actions = _actions()
    view = AdminNotificationsSettingsView(
        config=_config(),
        actions=actions,
    )
    button = view.children[1]
    assert isinstance(button, EditAdminNotificationLeadTimeButton)
    interaction = FakeInteraction()

    await button.callback(interaction)
    modal = interaction.response.modals[0]
    assert isinstance(modal, AdminNotificationsLeadModal)
    assert isinstance(modal, Modal)
    assert modal.lead_input.default == "10"

    modal.lead_input._value = "20"  # noqa: SLF001
    await modal.on_submit(FakeInteraction())
    actions.save_lead.assert_awaited_once()


@pytest.mark.asyncio
async def test_lead_modal_invalid_input_uses_configured_confused_emoji_without_callback() -> (
    None
):
    actions = _actions()
    modal = AdminNotificationsLeadModal(
        config_id=10,
        expected_updated_at=EXPECTED_UPDATED_AT,
        expected_lead=10,
        actions=actions,
        current_view=AdminNotificationsSettingsView(
            config=_config(),
            actions=actions,
        ),
    )
    modal.lead_input._value = "0"  # noqa: SLF001
    interaction = FakeInteraction()

    await modal.on_submit(interaction)

    assert interaction.response.messages[0][0] == lead_time_error_message()
    actions.save_lead.assert_not_awaited()


@pytest.mark.asyncio
async def test_mentions_and_toggle_recheck_permissions_and_route_expected_snapshot() -> (
    None
):
    actions = _actions()
    view = AdminNotificationsSettingsView(config=_config(), actions=actions)
    select = view.children[0]
    assert isinstance(select, AdminNotificationMentionableSelect)
    select._values = [SimpleNamespace(id=101)]  # noqa: SLF001
    denied = FakeInteraction(administrator=False)
    await select.callback(denied)
    assert actions.save_mentions.await_count == 0

    allowed = FakeInteraction()
    await select.callback(allowed)
    actions.save_mentions.assert_awaited_once()

    toggle = view.children[2]
    assert isinstance(toggle, ToggleShiftTimelineRemindersButton)
    await toggle.callback(FakeInteraction())
    actions.set_shift_reminders.assert_awaited_once()


@pytest.mark.asyncio
async def test_settings_timeout_disables_every_component() -> None:
    view = AdminNotificationsSettingsView(config=_config(), actions=_actions())
    await view.on_timeout()
    assert all(item.disabled for item in view.children)
