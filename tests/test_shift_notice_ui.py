from __future__ import annotations

# ruff: noqa: RUF001, SLF001
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock

import pytest
from discord.ui import Modal

from bot import config
from components.ui_shift_notice import (
    STALE_SETTINGS_MESSAGE,
    CancelShiftNoticeDestinationReplacementButton,
    EditShiftNoticeMinuteButton,
    ReplaceShiftNoticeDestinationButton,
    ReplaceShiftNoticeDestinationView,
    ShiftNoticeMinuteModal,
    ShiftNoticeSettingsBundle,
    ShiftNoticeSettingsView,
    ShiftNoticeUIActions,
    build_shift_notice_settings_bundle,
    minute_error_message,
)
from tests.fakes import FakeInteraction
from utils.shift_notice import ShiftNoticeSourceRecord, build_source_catalog

EXPECTED_UPDATED_AT = datetime(2026, 8, 13, tzinfo=UTC)
EVENT_DATE = date(2026, 8, 1)


def _actions() -> ShiftNoticeUIActions:
    return ShiftNoticeUIActions(
        setup_is_current=AsyncMock(return_value=True),
        save_setup=AsyncMock(),
        replace_destination=AsyncMock(),
        save_minute=AsyncMock(),
    )


def _config(minute: int | None = 45) -> SimpleNamespace:
    return SimpleNamespace(
        id=10,
        updated_at=EXPECTED_UPDATED_AT,
        minute_of_hour=minute,
        feature_channel=SimpleNamespace(channel_id=222),
    )


def _record(  # noqa: PLR0913
    source_id: int,
    channel_id: int,
    *,
    is_enabled: bool = True,
    created_offset: int = 0,
    event_date: date | None = EVENT_DATE,
    ranges: object = None,
    sheet_url: str | None = None,
    worksheet_id: int = 301,
    anchor: str = "B2",
) -> ShiftNoticeSourceRecord:
    return ShiftNoticeSourceRecord(
        id=source_id,
        feature_channel_id=1000 + source_id,
        channel_id=channel_id,
        is_enabled=is_enabled,
        created_at=EXPECTED_UPDATED_AT + timedelta(minutes=created_offset),
        sheet_url=(
            sheet_url
            if sheet_url is not None
            else f"https://docs.google.com/spreadsheets/d/source-{source_id}/edit"
        ),
        final_schedule_worksheet_id=worksheet_id,
        final_schedule_anchor_cell=anchor,
        event_date=event_date,
        recruitment_time_ranges=(
            [{"start": 4, "end": 6}] if ranges is None else ranges
        ),
    )


def _bundle(
    *,
    minute: int | None = 45,
    catalog: object | None = None,
    actions: ShiftNoticeUIActions | None = None,
) -> ShiftNoticeSettingsBundle:
    return build_shift_notice_settings_bundle(
        _config(minute),
        destination=SimpleNamespace(id=222, mention="<#222>"),
        catalog=(
            build_source_catalog((_record(1, 401),)) if catalog is None else catalog
        ),
        requesting_user_id=333,
        actions=actions or _actions(),
    )


def _all_fields(bundle: object) -> list[object]:
    return [
        field
        for page in bundle.message_pages
        for embed in page
        for field in embed.fields
    ]


def _warning_text(bundle: object) -> str:
    return "\n".join(
        field.value for field in _all_fields(bundle) if field.name == "Source Warnings"
    )


def _utf16_length(value: str) -> int:
    return len(value.encode("utf-16-le")) // 2


@pytest.mark.asyncio
async def test_setup_copy_snapshot_guards_and_raw_parser_routing() -> None:
    actions = _actions()
    bundle = _bundle(minute=None, actions=actions)
    view = bundle.view
    assert isinstance(view, ShiftNoticeSettingsView)
    assert [item.label for item in view.children] == ["Set Up Shift Notice"]

    interaction = FakeInteraction(user_id=333)
    await view.children[0].callback(interaction)

    modal = interaction.response.modals[0]
    assert isinstance(modal, ShiftNoticeMinuteModal)
    assert isinstance(modal, Modal)
    assert modal.title == "Set Up Shift Notice"
    assert modal.minute_input._underlying.label == "Minute of Each Hour (JST)"
    assert modal.minute_input.placeholder == "0–59"
    assert modal.minute_input.default == "45"

    modal.minute_input._value = " ４５ "
    await modal.on_submit(FakeInteraction(user_id=333))

    actions.save_setup.assert_awaited_once_with(
        ANY,
        10,
        EXPECTED_UPDATED_AT,
        " ４５ ",
    )


@pytest.mark.asyncio
async def test_edit_copy_invalid_input_and_valid_edit_routing() -> None:
    actions = _actions()
    bundle = _bundle(actions=actions)
    view = bundle.view
    button = view.children[0]
    assert isinstance(button, EditShiftNoticeMinuteButton)
    assert button.label == "Edit Notice Minute"

    interaction = FakeInteraction(user_id=333)
    await button.callback(interaction)
    modal = interaction.response.modals[0]
    assert isinstance(modal, ShiftNoticeMinuteModal)
    assert modal.title == "Edit Notice Minute"
    assert modal.minute_input.default == "45"

    modal.minute_input._value = "60"
    invalid = FakeInteraction(user_id=333)
    await modal.on_submit(invalid)
    assert invalid.response.messages[0][0] == minute_error_message()
    actions.save_minute.assert_not_awaited()

    modal.minute_input._value = "30"
    await modal.on_submit(FakeInteraction(user_id=333))
    actions.save_minute.assert_awaited_once_with(
        ANY,
        10,
        EXPECTED_UPDATED_AT,
        45,
        "30",
        view,
    )


@pytest.mark.parametrize(
    ("interaction", "stale"),
    [
        (FakeInteraction(user_id=444), False),
        (FakeInteraction(user_id=333, administrator=False), False),
        (FakeInteraction(user_id=333, manage_channels=False), False),
        (FakeInteraction(user_id=333), False),
        (FakeInteraction(user_id=333), True),
    ],
)
@pytest.mark.asyncio
async def test_settings_button_rejects_requester_permission_channel_and_snapshot(
    interaction: FakeInteraction,
    stale: bool,  # noqa: FBT001
) -> None:
    actions = _actions()
    if (
        interaction.user.id == 333
        and interaction.user.guild_permissions.administrator
        and interaction.user.guild_permissions.manage_channels
    ):
        if stale:
            actions.setup_is_current.return_value = False
        else:
            interaction.channel.id = 999
    view = _bundle(actions=actions).view

    await view.children[0].callback(interaction)

    assert not interaction.response.modals
    actions.save_setup.assert_not_awaited()
    actions.save_minute.assert_not_awaited()
    if stale:
        assert interaction.response.messages[0][0] == STALE_SETTINGS_MESSAGE


@pytest.mark.asyncio
async def test_replacement_is_destructive_requester_bound_and_rechecked() -> None:
    actions = _actions()
    view = ReplaceShiftNoticeDestinationView(
        requesting_user_id=333,
        config_id=10,
        expected_updated_at=EXPECTED_UPDATED_AT,
        expected_channel_id=777,
        replacement_channel_id=222,
        actions=actions,
    )
    assert [item.label for item in view.children] == [
        "‼️ Replace Channel",
        "Cancel",
    ]
    assert isinstance(view.children[0], ReplaceShiftNoticeDestinationButton)
    assert isinstance(view.children[1], CancelShiftNoticeDestinationReplacementButton)

    wrong_user = FakeInteraction(user_id=444)
    await view.children[0].callback(wrong_user)
    denied = FakeInteraction(user_id=333, manage_channels=False)
    await view.children[0].callback(denied)
    wrong_channel = FakeInteraction(user_id=333)
    wrong_channel.channel.id = 999
    await view.children[0].callback(wrong_channel)
    actions.replace_destination.assert_not_awaited()

    allowed = FakeInteraction(user_id=333)
    await view.children[0].callback(allowed)
    actions.setup_is_current.assert_awaited_once_with(10, EXPECTED_UPDATED_AT)
    actions.replace_destination.assert_awaited_once_with(allowed, 10, 777)


@pytest.mark.asyncio
async def test_replacement_cancel_and_timeout_apply_no_change() -> None:
    actions = _actions()
    view = ReplaceShiftNoticeDestinationView(
        requesting_user_id=333,
        config_id=10,
        expected_updated_at=EXPECTED_UPDATED_AT,
        expected_channel_id=777,
        replacement_channel_id=222,
        actions=actions,
    )

    interaction = FakeInteraction(user_id=333)
    await view.children[1].callback(interaction)
    assert interaction.response.edits[0][0] == "Operation cancelled."
    actions.replace_destination.assert_not_awaited()

    timeout_view = _bundle().view
    await timeout_view.on_timeout()
    assert all(item.disabled for item in timeout_view.children)


def test_settings_fields_copy_and_null_minute_are_exact() -> None:
    configured = _bundle()
    first_embed = configured.message_pages[0][0]
    assert first_embed.title == "Shift Notice Settings"
    assert first_embed.color.value == config.DEFAULT_EMBED_COLOR
    assert [field.name for field in _all_fields(configured)[:3]] == [
        "Notice Channel",
        "Notice Time",
        "Source Warnings",
    ]
    assert _all_fields(configured)[1].value == "Every hour at :45 JST"
    assert _warning_text(configured) == "✅ No source warnings."

    unset = _bundle(minute=None)
    assert _all_fields(unset)[1].value == "Not set"
    assert _all_fields(unset)[1].value != "Every hour at :45 JST"

    no_sources = _bundle(catalog=build_source_catalog(()))
    assert _warning_text(no_sources) == ("⚠️ No Shift Register sources are configured.")


def test_warning_copy_identifies_status_config_missing_parts_and_merged_overlap() -> (
    None
):
    catalog = build_source_catalog(
        (
            _record(1, 401, ranges=[{"start": 4, "end": 7}]),
            _record(
                2,
                402,
                is_enabled=False,
                created_offset=1,
                ranges=[{"start": 4, "end": 7}],
            ),
            _record(
                3,
                403,
                created_offset=2,
                event_date=None,
                ranges=[],
                sheet_url="",
                worksheet_id=0,
                anchor="",
            ),
        )
    )

    warning = _warning_text(_bundle(catalog=catalog))
    assert "🟢 <#401>" in warning
    assert "config ID `1`" in warning
    assert "⚫ <#402>" in warning
    assert "config ID `2`" in warning
    assert "config ID `3`" in warning
    assert "Event Date" in warning
    assert "Recruitment Time Ranges" in warning
    assert "Google Sheet" in warning
    assert "Final Schedule Worksheet ID" in warning
    assert "Final Schedule Anchor Cell" in warning
    assert "2026-08-01" in warning
    assert "04:00–07:00 JST" in warning
    assert warning.count("ignored") == 1
    assert "Tier 2" not in warning
    assert "runtime" not in warning.lower()


def test_warning_pagination_preserves_order_and_discord_limits() -> None:
    records = tuple(
        _record(
            source_id,
            400 + source_id,
            is_enabled=source_id % 2 == 0,
            created_offset=source_id,
            ranges=[{"start": 4, "end": 7}],
        )
        for source_id in range(1, 91)
    )
    bundle = _bundle(catalog=build_source_catalog(records))

    assert len(bundle.message_pages) > 1
    warning = _warning_text(bundle)
    ignored_lines = [line for line in warning.splitlines() if "ignored" in line]
    assert len(ignored_lines) == 89
    assert "config ID `2`" in ignored_lines[0]
    assert "config ID `90`" in ignored_lines[-1]

    for page in bundle.message_pages:
        assert 1 <= len(page) <= 10
        aggregate = 0
        for embed in page:
            assert len(embed.fields) <= 25
            aggregate += _utf16_length(embed.title or "")
            aggregate += _utf16_length(embed.description or "")
            for field in embed.fields:
                assert _utf16_length(field.name) <= 256
                assert _utf16_length(field.value) <= 1024
                aggregate += _utf16_length(field.name) + _utf16_length(field.value)
        assert aggregate <= 6000
