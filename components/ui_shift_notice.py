from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from discord import ButtonStyle, Embed, Interaction, TextStyle
from discord.ui import Button, Modal, TextInput, View

from bot import config as bot_config
from components.ui_permissions import require_settings_permissions
from components.ui_settings_flow import SettingsTimeoutView
from utils.google_sheets_urls import extract_google_sheet_id
from utils.shift_final import parse_a1_cell
from utils.shift_notice import ShiftNoticeCatalog, parse_minute_of_hour
from utils.shift_register_structs import RecruitmentTimeRanges

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from datetime import datetime

    from discord import Message, TextChannel

    from utils.shift_notice import (
        ShiftNoticeOverlapLoss,
        ShiftNoticeSource,
        ShiftNoticeSourceRecord,
    )


SHIFT_NOTICE_FEATURE_NAME = "shift_notice"
SHIFT_NOTICE_DISPLAY_NAME = "Shift Notice"
SETUP_BUTTON_LABEL = "Set Up Shift Notice"
SETUP_MODAL_TITLE = "Set Up Shift Notice"
EDIT_MINUTE_LABEL = "Edit Notice Minute"
MINUTE_INPUT_LABEL = "Minute of Each Hour (JST)"
MINUTE_INPUT_PLACEHOLDER = "0–59"  # noqa: RUF001
STALE_SETTINGS_MESSAGE = (
    "Shift Notice settings changed while this panel was open. "
    "Reopen settings and try again."
)
NOT_CONFIGURED_MESSAGE = (
    "Shift Notice settings are no longer configured for this channel."
)
UNAUTHORIZED_REQUESTER_MESSAGE = (
    "Only the administrator who opened these settings can use them."
)

_EMBED_TITLE_LIMIT: Final = 256
_EMBED_FIELD_NAME_LIMIT: Final = 256
_EMBED_FIELD_VALUE_LIMIT: Final = 1024
_EMBED_FIELD_COUNT_LIMIT: Final = 25
_EMBED_TOTAL_LIMIT: Final = 6000
_MESSAGE_EMBED_COUNT_LIMIT: Final = 10
_MESSAGE_EMBED_TOTAL_LIMIT: Final = 6000


def minute_error_message() -> str:
    return (
        f"⚠️ {bot_config.CONFUSED_EMOJI} Minute of Each Hour must be a whole "
        "number from 0 to 59. No settings were changed."
    )


@dataclass(frozen=True)
class ShiftNoticeUIActions:
    setup_is_current: Callable[[int, datetime], Awaitable[bool]]
    save_setup: Callable[[Interaction, int, datetime, str], Awaitable[None]]
    replace_destination: Callable[[Interaction, int, int], Awaitable[None]]
    save_minute: Callable[
        [Interaction, int, datetime, int | None, str, View],
        Awaitable[None],
    ]


@dataclass(frozen=True)
class ShiftNoticeSettingsBundle:
    message_pages: tuple[tuple[Embed, ...], ...]
    view: SettingsTimeoutView


async def _is_current_callback(  # noqa: PLR0913
    interaction: Interaction,
    *,
    requesting_user_id: int,
    expected_channel_id: int,
    config_id: int,
    expected_updated_at: datetime,
    actions: ShiftNoticeUIActions,
) -> bool:
    if interaction.user.id != requesting_user_id:
        await interaction.response.send_message(
            UNAUTHORIZED_REQUESTER_MESSAGE,
            ephemeral=True,
        )
        return False
    if not await require_settings_permissions(interaction):
        return False
    if getattr(interaction.channel, "id", None) != expected_channel_id:
        await interaction.response.send_message(
            STALE_SETTINGS_MESSAGE,
            ephemeral=True,
        )
        return False
    if not await actions.setup_is_current(config_id, expected_updated_at):
        await interaction.response.send_message(
            STALE_SETTINGS_MESSAGE,
            ephemeral=True,
        )
        return False
    return True


class ShiftNoticeSettingsView(SettingsTimeoutView):
    def __init__(
        self,
        *,
        requesting_user_id: int,
        config: object,
        expected_channel_id: int,
        actions: ShiftNoticeUIActions,
    ) -> None:
        super().__init__()
        self.requesting_user_id = requesting_user_id
        self.config = config
        self.expected_channel_id = expected_channel_id
        self.actions = actions
        self.continuation_messages: list[Message] = []
        self.add_item(
            SetUpShiftNoticeButton()
            if config.minute_of_hour is None
            else EditShiftNoticeMinuteButton()
        )


class SetUpShiftNoticeButton(Button):
    def __init__(self) -> None:
        super().__init__(label=SETUP_BUTTON_LABEL, style=ButtonStyle.primary)

    async def callback(self, interaction: Interaction) -> None:
        view = self.view
        if not isinstance(view, ShiftNoticeSettingsView):
            return
        if not await _is_current_callback(
            interaction,
            requesting_user_id=view.requesting_user_id,
            expected_channel_id=view.expected_channel_id,
            config_id=view.config.id,
            expected_updated_at=view.config.updated_at,
            actions=view.actions,
        ):
            return
        await interaction.response.send_modal(
            ShiftNoticeMinuteModal(
                requesting_user_id=view.requesting_user_id,
                config_id=view.config.id,
                expected_updated_at=view.config.updated_at,
                expected_channel_id=view.expected_channel_id,
                expected_minute=None,
                actions=view.actions,
                current_view=view,
                is_setup=True,
            )
        )


class EditShiftNoticeMinuteButton(Button):
    def __init__(self) -> None:
        super().__init__(label=EDIT_MINUTE_LABEL, style=ButtonStyle.secondary)

    async def callback(self, interaction: Interaction) -> None:
        view = self.view
        if not isinstance(view, ShiftNoticeSettingsView):
            return
        if not await _is_current_callback(
            interaction,
            requesting_user_id=view.requesting_user_id,
            expected_channel_id=view.expected_channel_id,
            config_id=view.config.id,
            expected_updated_at=view.config.updated_at,
            actions=view.actions,
        ):
            return
        await interaction.response.send_modal(
            ShiftNoticeMinuteModal(
                requesting_user_id=view.requesting_user_id,
                config_id=view.config.id,
                expected_updated_at=view.config.updated_at,
                expected_channel_id=view.expected_channel_id,
                expected_minute=view.config.minute_of_hour,
                actions=view.actions,
                current_view=view,
            )
        )


class ShiftNoticeMinuteModal(Modal):
    def __init__(  # noqa: PLR0913
        self,
        *,
        requesting_user_id: int,
        config_id: int,
        expected_updated_at: datetime,
        expected_channel_id: int,
        expected_minute: int | None,
        actions: ShiftNoticeUIActions,
        current_view: View,
        is_setup: bool = False,
    ) -> None:
        super().__init__(title=SETUP_MODAL_TITLE if is_setup else EDIT_MINUTE_LABEL)
        self.requesting_user_id = requesting_user_id
        self.config_id = config_id
        self.expected_updated_at = expected_updated_at
        self.expected_channel_id = expected_channel_id
        self.expected_minute = expected_minute
        self.actions = actions
        self.current_view = current_view
        self.is_setup = is_setup
        self.minute_input = TextInput(
            label=MINUTE_INPUT_LABEL,
            placeholder=MINUTE_INPUT_PLACEHOLDER,
            default="45" if is_setup else str(expected_minute),
            style=TextStyle.short,
            required=True,
        )
        self.add_item(self.minute_input)

    async def on_submit(self, interaction: Interaction) -> None:
        if not await _is_current_callback(
            interaction,
            requesting_user_id=self.requesting_user_id,
            expected_channel_id=self.expected_channel_id,
            config_id=self.config_id,
            expected_updated_at=self.expected_updated_at,
            actions=self.actions,
        ):
            return
        raw_value = self.minute_input.value
        try:
            parse_minute_of_hour(raw_value)
        except (TypeError, ValueError):
            await interaction.response.send_message(
                minute_error_message(),
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
        await self.actions.save_minute(
            interaction,
            self.config_id,
            self.expected_updated_at,
            self.expected_minute,
            raw_value,
            self.current_view,
        )


class ReplaceShiftNoticeDestinationView(SettingsTimeoutView):
    def __init__(  # noqa: PLR0913
        self,
        *,
        requesting_user_id: int,
        config_id: int,
        expected_updated_at: datetime,
        expected_channel_id: int,
        replacement_channel_id: int,
        actions: ShiftNoticeUIActions,
    ) -> None:
        super().__init__(timeout=20.0)
        self.requesting_user_id = requesting_user_id
        self.config_id = config_id
        self.expected_updated_at = expected_updated_at
        self.expected_channel_id = expected_channel_id
        self.replacement_channel_id = replacement_channel_id
        self.actions = actions
        self.add_item(ReplaceShiftNoticeDestinationButton())
        self.add_item(CancelShiftNoticeDestinationReplacementButton())


async def _replacement_is_current(
    interaction: Interaction,
    view: ReplaceShiftNoticeDestinationView,
) -> bool:
    return await _is_current_callback(
        interaction,
        requesting_user_id=view.requesting_user_id,
        expected_channel_id=view.replacement_channel_id,
        config_id=view.config_id,
        expected_updated_at=view.expected_updated_at,
        actions=view.actions,
    )


class ReplaceShiftNoticeDestinationButton(Button):
    def __init__(self) -> None:
        super().__init__(label="‼️ Replace Channel", style=ButtonStyle.danger)

    async def callback(self, interaction: Interaction) -> None:
        view = self.view
        if not isinstance(view, ReplaceShiftNoticeDestinationView):
            return
        if not await _replacement_is_current(interaction, view):
            return
        await interaction.response.defer(ephemeral=True)
        await view.actions.replace_destination(
            interaction,
            view.config_id,
            view.expected_channel_id,
        )
        view.stop()


class CancelShiftNoticeDestinationReplacementButton(Button):
    def __init__(self) -> None:
        super().__init__(label="Cancel", style=ButtonStyle.secondary)

    async def callback(self, interaction: Interaction) -> None:
        view = self.view
        if not isinstance(view, ReplaceShiftNoticeDestinationView):
            return
        if not await _replacement_is_current(interaction, view):
            return
        await interaction.response.edit_message(
            content="Operation cancelled.",
            view=None,
        )
        view.stop()


def _utf16_length(value: str) -> int:
    return len(value.encode("utf-16-le")) // 2


def _source_label(source: ShiftNoticeSource | ShiftNoticeSourceRecord) -> str:
    marker = "🟢" if source.is_enabled else "⚫"
    return f"{marker} <#{source.channel_id}> Shift Register (config ID `{source.id}`)"


def _missing_source_parts(source: ShiftNoticeSourceRecord) -> tuple[str, ...]:
    missing: list[str] = []
    if source.event_date is None:
        missing.append("Event Date")
    raw_ranges = source.recruitment_time_ranges
    if raw_ranges is None or raw_ranges == []:
        missing.append("Recruitment Time Ranges")
    else:
        try:
            ranges = RecruitmentTimeRanges.from_json(raw_ranges).ranges.ranges
        except (AttributeError, TypeError, ValueError):
            missing.append("Recruitment Time Ranges")
        else:
            if not ranges:
                missing.append("Recruitment Time Ranges")
    try:
        extract_google_sheet_id(source.sheet_url)
    except (TypeError, ValueError):
        missing.append("Google Sheet")
    worksheet_id = source.final_schedule_worksheet_id
    if worksheet_id.__class__ is not int or worksheet_id <= 0:
        missing.append("Final Schedule Worksheet ID")
    try:
        parse_a1_cell(source.final_schedule_anchor_cell)
    except (TypeError, ValueError):
        missing.append("Final Schedule Anchor Cell")
    return tuple(missing)


def _overlap_warning(
    loss: ShiftNoticeOverlapLoss,
    sources_by_id: dict[int, ShiftNoticeSource],
) -> str:
    loser = sources_by_id[loss.losing_source_id]
    winner = sources_by_id[loss.winning_source_id]
    return (
        f"⚠️ {loss.civil_start:%Y-%m-%d} "
        f"{loss.civil_start:%H:%M}–{loss.civil_end:%H:%M} JST — "  # noqa: RUF001
        f"ignored {_source_label(loser)}; used {_source_label(winner)}."
    )


def _warning_entries(catalog: ShiftNoticeCatalog) -> tuple[str, ...]:
    if not catalog.complete_sources and not catalog.incomplete_sources:
        return ("⚠️ No Shift Register sources are configured.",)

    warnings = [
        f"⚠️ {_source_label(source)} — missing: "
        f"{', '.join(_missing_source_parts(source))}."
        for source in catalog.incomplete_sources
    ]
    sources_by_id = {source.id: source for source in catalog.complete_sources}
    warnings.extend(
        _overlap_warning(loss, sources_by_id) for loss in catalog.overlap_losses
    )
    return tuple(warnings) or ("✅ No source warnings.",)


def _warning_field_values(entries: tuple[str, ...]) -> tuple[str, ...]:
    values: list[str] = []
    current: list[str] = []
    current_units = 0
    for entry in entries:
        entry_units = _utf16_length(entry)
        if entry_units > _EMBED_FIELD_VALUE_LIMIT:
            msg = "One Shift Notice source warning exceeds Discord's field limit."
            raise ValueError(msg)
        separator_units = 1 if current else 0
        if (
            current
            and current_units + separator_units + entry_units > _EMBED_FIELD_VALUE_LIMIT
        ):
            values.append("\n".join(current))
            current = []
            current_units = 0
            separator_units = 0
        current.append(entry)
        current_units += separator_units + entry_units
    if current:
        values.append("\n".join(current))
    return tuple(values)


def _pack_settings_fields(
    fields: tuple[tuple[str, str], ...],
) -> tuple[tuple[Embed, ...], ...]:
    title = "Shift Notice Settings"
    title_units = _utf16_length(title)
    if title_units > _EMBED_TITLE_LIMIT:
        raise ValueError

    pages: list[list[Embed]] = []
    page: list[Embed] = []
    page_units = 0
    embed: Embed | None = None
    embed_units = 0

    for name, value in fields:
        name_units = _utf16_length(name)
        value_units = _utf16_length(value)
        if (
            name_units > _EMBED_FIELD_NAME_LIMIT
            or value_units > _EMBED_FIELD_VALUE_LIMIT
        ):
            raise ValueError
        field_units = name_units + value_units

        needs_embed = (
            embed is None
            or len(embed.fields) >= _EMBED_FIELD_COUNT_LIMIT
            or embed_units + field_units > _EMBED_TOTAL_LIMIT
        )
        if needs_embed:
            if page and (
                len(page) >= _MESSAGE_EMBED_COUNT_LIMIT
                or page_units + title_units + field_units > _MESSAGE_EMBED_TOTAL_LIMIT
            ):
                pages.append(page)
                page = []
                page_units = 0
            embed = Embed(title=title, color=bot_config.DEFAULT_EMBED_COLOR)
            page.append(embed)
            page_units += title_units
            embed_units = title_units
        elif page_units + field_units > _MESSAGE_EMBED_TOTAL_LIMIT:
            pages.append(page)
            page = [Embed(title=title, color=bot_config.DEFAULT_EMBED_COLOR)]
            page_units = title_units
            embed = page[0]
            embed_units = title_units

        embed.add_field(name=name, value=value, inline=False)
        embed_units += field_units
        page_units += field_units

    if page:
        pages.append(page)
    return tuple(tuple(current_page) for current_page in pages)


def build_shift_notice_settings_bundle(
    config: object,
    *,
    destination: TextChannel,
    catalog: ShiftNoticeCatalog,
    requesting_user_id: int,
    actions: ShiftNoticeUIActions,
) -> ShiftNoticeSettingsBundle:
    minute = config.minute_of_hour
    fields = [
        ("Notice Channel", destination.mention),
        (
            "Notice Time",
            "Not set" if minute is None else f"Every hour at :{minute:02d} JST",
        ),
    ]
    fields.extend(
        ("Source Warnings", value)
        for value in _warning_field_values(_warning_entries(catalog))
    )
    return ShiftNoticeSettingsBundle(
        message_pages=_pack_settings_fields(tuple(fields)),
        view=ShiftNoticeSettingsView(
            requesting_user_id=requesting_user_id,
            config=config,
            expected_channel_id=destination.id,
            actions=actions,
        ),
    )
