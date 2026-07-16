from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
from io import BytesIO
from typing import TYPE_CHECKING

import discord
from discord import Interaction, TextChannel, app_commands
from discord.ext import tasks
from tortoise.exceptions import DBConnectionError, OperationalError

from bot import config as bot_config
from cogs.base.discord_context import require_guild_channel_source
from cogs.base.feature_channel_base import FeatureChannelBase, FeatureNotEnabled
from components.ui_settings_flow import (
    SETTINGS_STORAGE_EXCEPTIONS,
    attach_settings_view_message,
    prepare_replacement_settings_view,
    send_settings_refresh_failure,
    send_settings_storage_error,
)
from components.ui_shift_notice import (
    NOT_CONFIGURED_MESSAGE,
    SHIFT_NOTICE_DISPLAY_NAME,
    SHIFT_NOTICE_FEATURE_NAME,
    STALE_SETTINGS_MESSAGE,
    ReplaceShiftNoticeDestinationView,
    ShiftNoticeSettingsBundle,
    ShiftNoticeUIActions,
    build_shift_notice_settings_bundle,
    minute_error_message,
)
from models.shift_notice import ShiftNoticeConfig
from utils.announcement_languages import get_announcement_languages
from utils.google_sheets_errors import GoogleSheetsError, GoogleSheetsErrorKind
from utils.shift_notice import (
    JST,
    ShiftNoticeCaseKind,
    ShiftNoticeCatalog,
    ShiftNoticeFrame,
    ShiftNoticePerson,
    ShiftNoticeSnapshot,
    boundary_for_scheduled_tick,
    latest_reached_boundary,
    parse_minute_of_hour,
)
from utils.shift_notice_manager import (
    ShiftNoticeManager,
    ShiftNoticeStaleStateError,
    claim_destination,
    get_destination_config,
    get_guild_config,
    replace_unavailable_destination,
    save_minute,
)
from utils.shift_notice_messages import build_failure_message, build_normal_message
from utils.shift_notice_renderer import (
    ShiftNoticeRenderFrame,
    ShiftNoticeRenderInput,
    render_shift_notice,
)
from utils.shift_schedule_role import resolve_schedule_role_label_matches
from utils.storage_errors import StorageError, StorageErrorKind

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping

    from discord.ui import View

    from bot import Rhoboto
    from models.feature_channel import FeatureChannel


INVALID_DESTINATION_MESSAGE = (
    "⚠️ Shift Notice requires a normal text channel where the bot can view the "
    "channel, send messages, embed links, and attach files."
)
REPLACEMENT_PROMPT = (
    "‼️ The configured Shift Notice channel is unavailable. "
    "Replace it with this channel?"
)
MANUAL_SEND_FAILURE_MESSAGE = (
    "Shift Notice could not be sent. No public failure message was posted."
)
MANUAL_STALE_MESSAGE = (
    "Shift Notice settings changed before sending. No notice was posted."
)

_PREPARE_LEAD = timedelta(seconds=30)
_RETRY_DEADLINE = timedelta(minutes=5)
_RETRY_BASE_SECONDS = 5
_RETRY_MAX_SECONDS = 300


@dataclass(frozen=True)
class ShiftNoticeTickSpec:
    config_id: int
    feature_channel_id: int
    guild_id: int
    channel_id: int
    minute_of_hour: int
    scheduled_tick: datetime
    target_boundary: datetime


class _AutomaticPreparationError(Exception):
    def __init__(
        self,
        cause: Exception,
        *,
        catalog: object | None,
        languages: tuple[str, ...],
    ) -> None:
        super().__init__(type(cause).__name__)
        self.cause = cause
        self.catalog = catalog
        self.languages = languages


def _following_exact_minute(now: datetime) -> datetime:
    """Return the exact minute immediately after ``now``."""
    return now.replace(second=0, microsecond=0) + timedelta(minutes=1)


def _strict_future_tick(now: datetime, minute: int) -> datetime | None:
    """Return a configured tick strictly inside the next-minute window."""
    candidate = now.replace(minute=minute, second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(hours=1)
    if now < candidate < now + timedelta(minutes=1):
        return candidate
    return None


def is_usable_shift_notice_destination(channel: object, guild: object) -> bool:
    if not isinstance(channel, TextChannel):
        return False
    bot_member = getattr(guild, "me", None)
    if bot_member is None:
        return False
    permissions = channel.permissions_for(bot_member)
    return bool(
        permissions.view_channel
        and permissions.send_messages
        and permissions.embed_links
        and permissions.attach_files
    )


def configured_elsewhere_message(channel_id: int) -> str:
    return (
        f"Shift Notice is already configured in <#{channel_id}>. "
        "Use `/shift_notice settings` there."
    )


def _render_hours(
    person: ShiftNoticePerson | None,
    hours: Mapping[object, int],
) -> str | None:
    if person is None:
        return None
    return f"{hours[person.key]}h"


def _render_frame(
    frame: ShiftNoticeFrame,
    hours: Mapping[object, int],
) -> ShiftNoticeRenderFrame:
    return ShiftNoticeRenderFrame(
        range_label=f"{frame.event_hour}–{frame.event_hour + 1}",  # noqa: RUF001
        names=tuple(
            None if person is None else person.schedule_label for person in frame.lanes
        ),
        hours=tuple(_render_hours(person, hours) for person in frame.lanes),
    )


def _snapshot_render_input(snapshot: ShiftNoticeSnapshot) -> ShiftNoticeRenderInput:
    previous = (
        _render_frame(snapshot.previous, snapshot.cumulative_hours)
        if snapshot.case in {ShiftNoticeCaseKind.TRANSITION, ShiftNoticeCaseKind.END}
        else None
    )
    next_frame = (
        _render_frame(snapshot.next, snapshot.remaining_hours)
        if snapshot.case in {ShiftNoticeCaseKind.START, ShiftNoticeCaseKind.TRANSITION}
        else None
    )
    return ShiftNoticeRenderInput(
        case=snapshot.case,
        previous=previous,
        next=next_frame,
        cut_window=snapshot.cut_window,
    )


class ShiftNotice(FeatureChannelBase, group_name=SHIFT_NOTICE_FEATURE_NAME):
    feature_name = SHIFT_NOTICE_FEATURE_NAME
    feature_display_name = SHIFT_NOTICE_DISPLAY_NAME

    def __init__(  # noqa: PLR0913
        self,
        bot: Rhoboto,
        *,
        manager: ShiftNoticeManager | None = None,
        renderer: Callable[[ShiftNoticeRenderInput], bytes] = render_shift_notice,
        now: Callable[[], datetime] | None = None,
        sleep_until: Callable[[datetime], Awaitable[None]] | None = None,
        retry_sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        super().__init__(bot)
        self._manager = manager or ShiftNoticeManager(
            bot_config.GOOGLE_SERVICE_ACCOUNT_PATH
        )
        self._renderer = renderer
        self._now = now or (lambda: datetime.now(JST))
        self._sleep_until = sleep_until or self._default_sleep_until
        self._retry_sleep = retry_sleep or asyncio.sleep
        self._tick_tasks: dict[int, asyncio.Task[None]] = {}
        self._tick_specs: dict[int, ShiftNoticeTickSpec] = {}
        self._tick_registry_lock = asyncio.Lock()
        self._bootstrap_task: asyncio.Task[None] | None = None
        self._reschedule_tasks: set[asyncio.Task[None]] = set()
        self._is_unloading = False
        self._dispatcher = self._dispatch_shift_notice_ticks

    def _ui_actions(self) -> ShiftNoticeUIActions:
        return ShiftNoticeUIActions(
            setup_is_current=self._setup_is_current,
            save_setup=self._save_setup,
            replace_destination=self._replace_destination,
            save_minute=self._save_minute,
        )

    async def _validate_lifecycle_owner(self, source: object) -> None:
        config = await get_guild_config(source.guild.id)
        if (
            config is not None
            and config.feature_channel.channel_id != source.channel.id
        ):
            raise FeatureNotEnabled(self.feature_name, self.feature_display_name)

    @app_commands.command(
        name="enable",
        description="Enable this feature in the current channel.",
    )
    async def enable(self, interaction: Interaction) -> None:
        source = require_guild_channel_source(
            interaction,
            action="proceed with enable command",
        )
        if not is_usable_shift_notice_destination(source.channel, source.guild):
            await interaction.response.send_message(
                INVALID_DESTINATION_MESSAGE,
                ephemeral=True,
            )
            return

        try:
            claim = await claim_destination(source.guild.id, source.channel.id)
        except Exception as exc:  # noqa: BLE001
            await self._send_interaction_storage_error_or_raise(
                interaction,
                exc,
                source=source,
                operation="enable",
            )
            return

        if claim.owns_requested_destination:
            self._reschedule_future_tick(source.guild.id)
            await interaction.response.send_message(
                f"Feature {self.feature_display_name} enabled in this channel.",
                ephemeral=True,
            )
            await self.setup_after_enable(interaction)
            return

        stored_channel = source.guild.get_channel(claim.channel_id)
        if is_usable_shift_notice_destination(stored_channel, source.guild):
            await interaction.response.send_message(
                configured_elsewhere_message(claim.channel_id),
                ephemeral=True,
            )
            return

        try:
            config = await get_guild_config(source.guild.id)
        except Exception as exc:  # noqa: BLE001
            await self._send_interaction_storage_error_or_raise(
                interaction,
                exc,
                source=source,
                operation="enable_replacement",
            )
            return
        if config is None or config.id != claim.config_id:
            await interaction.response.send_message(
                STALE_SETTINGS_MESSAGE,
                ephemeral=True,
            )
            return
        view = ReplaceShiftNoticeDestinationView(
            requesting_user_id=interaction.user.id,
            config_id=config.id,
            expected_updated_at=config.updated_at,
            expected_channel_id=claim.channel_id,
            replacement_channel_id=source.channel.id,
            actions=self._ui_actions(),
        )
        await interaction.response.send_message(
            REPLACEMENT_PROMPT,
            view=view,
            ephemeral=True,
        )

    @app_commands.command(
        name="settings",
        description="Show and edit current feature settings for this channel.",
    )
    @app_commands.check(
        FeatureChannelBase.feature_enabled_app_command_predicate(
            SHIFT_NOTICE_FEATURE_NAME,
            SHIFT_NOTICE_DISPLAY_NAME,
        )
    )
    async def settings(self, interaction: Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        await self.setup_after_enable(interaction)

    async def setup_after_enable(self, interaction: Interaction) -> None:
        source = require_guild_channel_source(
            interaction,
            action="show Shift Notice settings",
        )
        config = await get_destination_config(source.guild.id, source.channel.id)
        if config is None:
            await interaction.followup.send(NOT_CONFIGURED_MESSAGE, ephemeral=True)
            return
        try:
            catalog = await self._manager.load_source_catalog(source.guild.id)
        except SETTINGS_STORAGE_EXCEPTIONS as exc:
            await send_settings_storage_error(
                interaction,
                exc,
                operation="shift_notice_settings",
                feature_name=self.feature_name,
                log=self.logger,
            )
            return
        bundle = build_shift_notice_settings_bundle(
            config,
            destination=source.channel,
            catalog=catalog,
            requesting_user_id=interaction.user.id,
            actions=self._ui_actions(),
        )
        await self._send_settings_bundle_followup(interaction, bundle)

    async def _send_settings_bundle_followup(
        self,
        interaction: Interaction,
        bundle: ShiftNoticeSettingsBundle,
    ) -> None:
        first_message = await interaction.followup.send(
            embeds=list(bundle.message_pages[0]),
            view=bundle.view,
            ephemeral=True,
            wait=True,
        )
        attach_settings_view_message(bundle.view, first_message)
        continuation_messages = getattr(bundle.view, "continuation_messages", None)
        for page in bundle.message_pages[1:]:
            message = await interaction.followup.send(
                embeds=list(page),
                ephemeral=True,
                wait=True,
            )
            if continuation_messages is not None:
                continuation_messages.append(message)

    async def _setup_is_current(
        self,
        config_id: int,
        expected_updated_at: datetime,
    ) -> bool:
        config = await ShiftNoticeConfig.get_or_none(id=config_id)
        return config is not None and config.updated_at == expected_updated_at

    async def _owned_config(
        self,
        interaction: Interaction,
    ) -> tuple[object, TextChannel, ShiftNoticeConfig] | None:
        source = require_guild_channel_source(
            interaction,
            action="update Shift Notice settings",
        )
        config = await get_destination_config(
            source.guild.id,
            source.channel.id,
            require_enabled=True,
        )
        if config is None:
            await interaction.followup.send(NOT_CONFIGURED_MESSAGE, ephemeral=True)
            return None
        return source.guild, source.channel, config

    async def _save_setup(
        self,
        interaction: Interaction,
        config_id: int,
        expected_updated_at: datetime,
        raw_minute: str,
    ) -> None:
        await self._save_minute_value(
            interaction,
            config_id,
            expected_updated_at,
            None,
            raw_minute,
            current_view=None,
            setup_only=True,
        )

    async def _save_minute(  # noqa: PLR0913
        self,
        interaction: Interaction,
        config_id: int,
        expected_updated_at: datetime,
        expected_minute: int | None,
        raw_minute: str,
        current_view: View,
    ) -> None:
        await self._save_minute_value(
            interaction,
            config_id,
            expected_updated_at,
            expected_minute,
            raw_minute,
            current_view=current_view,
            setup_only=False,
        )

    async def _save_minute_value(  # noqa: PLR0913
        self,
        interaction: Interaction,
        config_id: int,
        expected_updated_at: datetime,
        expected_minute: int | None,
        raw_minute: str,
        *,
        current_view: View | None,
        setup_only: bool,
    ) -> None:
        owned = await self._owned_config(interaction)
        if owned is None:
            return
        guild, destination, _ = owned
        try:
            new_minute = parse_minute_of_hour(raw_minute)
        except (TypeError, ValueError):
            await interaction.followup.send(minute_error_message(), ephemeral=True)
            return
        try:
            await save_minute(
                config_id,
                expected_updated_at=expected_updated_at,
                expected_minute=expected_minute,
                new_minute=new_minute,
                setup_only=setup_only,
            )
        except ShiftNoticeStaleStateError:
            await interaction.followup.send(STALE_SETTINGS_MESSAGE, ephemeral=True)
            return
        except SETTINGS_STORAGE_EXCEPTIONS as exc:
            await send_settings_storage_error(
                interaction,
                exc,
                operation=(
                    "shift_notice_setup" if setup_only else "shift_notice_minute"
                ),
                feature_name=self.feature_name,
                log=self.logger,
            )
            return
        self._reschedule_future_tick(guild.id)
        try:
            await self._refresh_settings_response(
                interaction,
                guild,
                destination,
                current_view=current_view,
            )
        except SETTINGS_STORAGE_EXCEPTIONS as exc:
            await send_settings_refresh_failure(
                interaction,
                exc,
                operation="shift_notice_settings_refresh",
                feature_name=self.feature_name,
                log=self.logger,
            )

    async def _refresh_settings_response(
        self,
        interaction: Interaction,
        guild: object,
        destination: TextChannel,
        *,
        current_view: View | None,
    ) -> None:
        config = await get_destination_config(guild.id, destination.id)
        if config is None:
            await interaction.followup.send(NOT_CONFIGURED_MESSAGE, ephemeral=True)
            return
        catalog = await self._manager.load_source_catalog(guild.id)
        bundle = build_shift_notice_settings_bundle(
            config,
            destination=destination,
            catalog=catalog,
            requesting_user_id=interaction.user.id,
            actions=self._ui_actions(),
        )
        if current_view is None:
            await self._send_settings_bundle_followup(interaction, bundle)
            return

        for message in tuple(getattr(current_view, "continuation_messages", ())):
            try:
                await message.delete()
            except discord.HTTPException:
                self.logger.warning(
                    "Shift Notice settings continuation cleanup failed. "
                    "operation=settings_refresh guild_id=%s destination_channel_id=%s",
                    guild.id,
                    destination.id,
                )
        replacement = prepare_replacement_settings_view(current_view, bundle.view)
        await interaction.edit_original_response(
            content=None,
            embeds=list(bundle.message_pages[0]),
            view=replacement,
        )
        continuation_messages = getattr(bundle.view, "continuation_messages", None)
        for page in bundle.message_pages[1:]:
            message = await interaction.followup.send(
                embeds=list(page),
                ephemeral=True,
                wait=True,
            )
            if continuation_messages is not None:
                continuation_messages.append(message)

    async def _replace_destination(
        self,
        interaction: Interaction,
        config_id: int,
        expected_channel_id: int,
    ) -> None:
        source = require_guild_channel_source(
            interaction,
            action="replace Shift Notice destination",
        )
        if not is_usable_shift_notice_destination(source.channel, source.guild):
            await interaction.edit_original_response(
                content=INVALID_DESTINATION_MESSAGE,
                view=None,
            )
            return
        config = await get_guild_config(source.guild.id)
        old_channel = source.guild.get_channel(
            config.feature_channel.channel_id if config is not None else 0
        )
        if (
            config is None
            or config.id != config_id
            or config.feature_channel.channel_id != expected_channel_id
            or is_usable_shift_notice_destination(old_channel, source.guild)
        ):
            await interaction.edit_original_response(
                content=STALE_SETTINGS_MESSAGE,
                view=None,
            )
            return
        try:
            await replace_unavailable_destination(
                config_id,
                expected_channel_id,
                source.channel.id,
            )
        except ShiftNoticeStaleStateError:
            await interaction.edit_original_response(
                content=STALE_SETTINGS_MESSAGE,
                view=None,
            )
            return
        except SETTINGS_STORAGE_EXCEPTIONS as exc:
            await send_settings_storage_error(
                interaction,
                exc,
                operation="shift_notice_destination_replacement",
                feature_name=self.feature_name,
                log=self.logger,
            )
            return
        self._reschedule_future_tick(source.guild.id)
        await interaction.edit_original_response(
            content=f"Feature {self.feature_display_name} enabled in this channel.",
            view=None,
        )
        await self.setup_after_enable(interaction)

    async def _default_sleep_until(self, target: datetime) -> None:
        while True:
            seconds = (target - self._now()).total_seconds()
            if seconds <= 0:
                return
            await asyncio.sleep(seconds)

    @staticmethod
    def _config_tick_spec(
        config: ShiftNoticeConfig,
        scheduled_tick: datetime,
    ) -> ShiftNoticeTickSpec | None:
        feature_channel = config.feature_channel
        minute = config.minute_of_hour
        if minute is None or not feature_channel.is_enabled:
            return None
        return ShiftNoticeTickSpec(
            config_id=config.id,
            feature_channel_id=feature_channel.id,
            guild_id=config.guild_id,
            channel_id=feature_channel.channel_id,
            minute_of_hour=minute,
            scheduled_tick=scheduled_tick,
            target_boundary=boundary_for_scheduled_tick(scheduled_tick, minute),
        )

    async def _enabled_configs(self) -> list[ShiftNoticeConfig]:
        return list(
            await ShiftNoticeConfig.filter(
                feature_channel__is_enabled=True,
            )
            .exclude(minute_of_hour=None)
            .select_related("feature_channel")
        )

    async def _schedule_tick(self, spec: ShiftNoticeTickSpec) -> None:
        async with self._tick_registry_lock:
            if self._is_unloading:
                return
            current_spec = self._tick_specs.get(spec.guild_id)
            current_task = self._tick_tasks.get(spec.guild_id)
            if (
                current_spec == spec
                and current_task is not None
                and not current_task.done()
            ):
                return
            if current_task is not None:
                self._tick_tasks.pop(spec.guild_id, None)
                self._tick_specs.pop(spec.guild_id, None)
                current_task.cancel()
                await asyncio.gather(current_task, return_exceptions=True)
            if self._is_unloading:
                return
            self._tick_specs[spec.guild_id] = spec
            task = asyncio.create_task(
                self._run_tick(spec),
                name=(
                    f"shift-notice-tick-{spec.guild_id}-"
                    f"{spec.scheduled_tick:%Y%m%d%H%M}"
                ),
            )
            self._tick_tasks[spec.guild_id] = task
            task.add_done_callback(
                lambda completed, guild_id=spec.guild_id: self._remove_tick_if_current(
                    guild_id,
                    completed,
                )
            )

    def _remove_tick_if_current(
        self,
        guild_id: int,
        completed: asyncio.Task[None],
    ) -> None:
        if self._tick_tasks.get(guild_id) is not completed:
            return
        self._tick_tasks.pop(guild_id, None)
        self._tick_specs.pop(guild_id, None)

    async def _cancel_scheduled_delivery(self, guild_id: int) -> None:
        async with self._tick_registry_lock:
            task = self._tick_tasks.pop(guild_id, None)
            self._tick_specs.pop(guild_id, None)
            if task is None:
                return
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    def _is_current_tick(self, spec: ShiftNoticeTickSpec) -> bool:
        current = self._tick_specs.get(spec.guild_id)
        return current == spec

    def _guild_for_id(self, guild_id: int) -> object | None:
        getter = getattr(self.bot, "get_guild", None)
        if callable(getter):
            guild = getter(guild_id)
            if guild is not None:
                return guild
        return next(
            (
                guild
                for guild in getattr(self.bot, "guilds", ())
                if getattr(guild, "id", None) == guild_id
            ),
            None,
        )

    async def _delivery_context(
        self,
        spec: ShiftNoticeTickSpec,
    ) -> tuple[object, TextChannel, ShiftNoticeConfig] | None:
        if not self._is_current_tick(spec):
            return None
        config = await get_guild_config(spec.guild_id)
        if config is None:
            self.logger.warning(
                "Shift Notice automatic destination unavailable. "
                "operation=automatic_delivery stage=destination guild_id=%s "
                "config_id=%s destination_channel_id=%s",
                spec.guild_id,
                spec.config_id,
                spec.channel_id,
            )
            return None
        feature_channel = config.feature_channel
        if (
            config.id != spec.config_id
            or config.guild_id != spec.guild_id
            or feature_channel.id != spec.feature_channel_id
            or feature_channel.guild_id != spec.guild_id
            or feature_channel.channel_id != spec.channel_id
            or not feature_channel.is_enabled
            or config.minute_of_hour != spec.minute_of_hour
        ):
            return None
        guild = self._guild_for_id(spec.guild_id)
        if guild is None:
            self.logger.warning(
                "Shift Notice automatic destination unavailable. "
                "operation=automatic_delivery stage=destination guild_id=%s "
                "config_id=%s destination_channel_id=%s",
                spec.guild_id,
                spec.config_id,
                spec.channel_id,
            )
            return None
        destination = guild.get_channel(spec.channel_id)
        if not is_usable_shift_notice_destination(destination, guild):
            self.logger.warning(
                "Shift Notice automatic destination unusable. "
                "operation=automatic_delivery stage=destination guild_id=%s "
                "config_id=%s destination_channel_id=%s",
                spec.guild_id,
                spec.config_id,
                spec.channel_id,
            )
            return None
        return guild, destination, config

    async def _prepare_automatic_payload(
        self,
        spec: ShiftNoticeTickSpec,
    ) -> tuple[object, TextChannel, ShiftNoticeCatalog, tuple[str, ...]] | None:
        catalog = None
        languages: tuple[str, ...] = ()
        try:
            context = await self._delivery_context(spec)
            if context is None:
                return None
            guild, destination, _ = context
            catalog = await self._manager.load_source_catalog(spec.guild_id)
            if (
                catalog.envelope_start is None
                or catalog.envelope_end is None
                or not (
                    catalog.envelope_start
                    <= spec.target_boundary
                    <= catalog.envelope_end
                )
            ):
                return None
            languages = tuple(
                await get_announcement_languages(spec.guild_id, self.logger)
            )
            snapshot = await self._manager.build_snapshot(
                catalog,
                spec.target_boundary,
                lambda labels: resolve_schedule_role_label_matches(
                    labels,
                    tuple(guild.members),
                ),
            )
            image_bytes = await asyncio.to_thread(
                self._renderer,
                _snapshot_render_input(snapshot),
            )
            payload = build_normal_message(
                snapshot,
                image_bytes,
                languages,
                upload_limit=guild.filesize_limit,
            )
            return payload, destination, catalog, languages  # noqa: TRY300
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise _AutomaticPreparationError(
                exc,
                catalog=catalog,
                languages=languages,
            ) from exc

    @staticmethod
    def _retryable(exc: Exception) -> bool:
        if isinstance(exc, (OperationalError, DBConnectionError)):
            return True
        if isinstance(exc, GoogleSheetsError):
            return exc.kind in {
                GoogleSheetsErrorKind.QUOTA,
                GoogleSheetsErrorKind.TRANSIENT,
            }
        if isinstance(exc, StorageError):
            return exc.kind in {
                StorageErrorKind.GOOGLE_SHEETS_QUOTA,
                StorageErrorKind.GOOGLE_SHEETS_TRANSIENT,
                StorageErrorKind.DATABASE_UNAVAILABLE,
            }
        return False

    @staticmethod
    def _failure_event_hour(
        target_boundary: datetime,
        catalog: object | None,
    ) -> int:
        if catalog is not None:
            owners = getattr(catalog, "slot_owners", {})
            source_id = owners.get(target_boundary)
            sources = tuple(getattr(catalog, "complete_sources", ()))
            candidates = (
                tuple(item for item in sources if item.id == source_id)
                if source_id is not None
                else ()
            ) + tuple(item for item in sources if item.id != source_id)
            for source in candidates:
                try:
                    start = source.civil_start(source.first_hour)
                    end = source.civil_start(source.end_hour)
                    if start <= target_boundary <= end:
                        return source.event_hour(target_boundary)
                except (AttributeError, ValueError):
                    continue
        return target_boundary.hour

    async def _final_destination(
        self,
        spec: ShiftNoticeTickSpec,
    ) -> TextChannel | None:
        deadline = spec.target_boundary + _RETRY_DEADLINE
        attempt = 0
        while True:
            try:
                context = await self._delivery_context(spec)
            except asyncio.CancelledError:
                raise
            except (OperationalError, DBConnectionError) as exc:
                remaining = (deadline - self._now()).total_seconds()
                delay = min(
                    _RETRY_MAX_SECONDS,
                    _RETRY_BASE_SECONDS * 2**attempt,
                    max(0.0, remaining),
                )
                if delay <= 0:
                    self.logger.warning(
                        "Shift Notice final destination revalidation exhausted. "
                        "operation=automatic_revalidate stage=destination "
                        "guild_id=%s config_id=%s destination_channel_id=%s "
                        "exception_class=%s",
                        spec.guild_id,
                        spec.config_id,
                        spec.channel_id,
                        type(exc).__name__,
                    )
                    return None
                self.logger.warning(
                    "Shift Notice final destination revalidation failed; retrying. "
                    "operation=automatic_revalidate stage=destination "
                    "guild_id=%s config_id=%s destination_channel_id=%s "
                    "attempt=%s retry_delay=%s exception_class=%s",
                    spec.guild_id,
                    spec.config_id,
                    spec.channel_id,
                    attempt,
                    delay,
                    type(exc).__name__,
                )
                await self._retry_sleep(delay)
                attempt += 1
                if self._now() >= deadline:
                    self.logger.warning(
                        "Shift Notice final destination revalidation exhausted. "
                        "operation=automatic_revalidate stage=destination "
                        "guild_id=%s config_id=%s destination_channel_id=%s "
                        "exception_class=%s",
                        spec.guild_id,
                        spec.config_id,
                        spec.channel_id,
                        type(exc).__name__,
                    )
                    return None
                continue
            if context is None:
                return None
            return context[1]

    async def _send_automatic_payload(
        self,
        destination: TextChannel,
        payload: object,
    ) -> None:
        image_bytes = getattr(payload, "image_bytes", None)
        kwargs: dict[str, object] = {
            "embeds": list(payload.embeds),
            "allowed_mentions": discord.AllowedMentions.none(),
        }
        if image_bytes is not None:
            filename = payload.filename
            if filename is None:
                return
            kwargs["file"] = discord.File(BytesIO(image_bytes), filename=filename)
        await destination.send(**kwargs)

    async def _failure_payload(
        self,
        spec: ShiftNoticeTickSpec,
        catalog: object | None,
        languages: tuple[str, ...],
    ) -> object | None:
        if not languages:
            try:
                languages = tuple(
                    await get_announcement_languages(spec.guild_id, self.logger)
                )
            except Exception:  # noqa: BLE001
                languages = ("en",)
        try:
            return build_failure_message(
                spec.target_boundary,
                self._failure_event_hour(spec.target_boundary, catalog),
                languages or ("en",),
            )
        except Exception as exc:  # noqa: BLE001
            self.logger.warning(
                "Shift Notice failure payload construction failed. "
                "operation=automatic_failure stage=failure_payload guild_id=%s "
                "config_id=%s destination_channel_id=%s target_boundary=%s "
                "exception_class=%s",
                spec.guild_id,
                spec.config_id,
                spec.channel_id,
                spec.target_boundary.isoformat(),
                type(exc).__name__,
            )
            return None

    async def _run_tick(  # noqa: C901, PLR0911, PLR0912, PLR0915
        self,
        spec: ShiftNoticeTickSpec,
    ) -> None:
        try:
            if not self._is_current_tick(spec):
                return
            prepare_at = spec.scheduled_tick - _PREPARE_LEAD
            if self._now() < prepare_at:
                await self._sleep_until(prepare_at)
            retry_deadline = spec.target_boundary + _RETRY_DEADLINE
            attempt = 0
            payload = None
            catalog = None
            languages: tuple[str, ...] = ()
            if self._now() >= retry_deadline:
                payload = await self._failure_payload(spec, catalog, languages)
                if payload is None:
                    return
            while True:
                if not self._is_current_tick(spec):
                    return
                if payload is not None:
                    break
                try:
                    prepared = await self._prepare_automatic_payload(spec)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    cause = (
                        exc.cause
                        if isinstance(exc, _AutomaticPreparationError)
                        else exc
                    )
                    if isinstance(exc, _AutomaticPreparationError):
                        catalog = exc.catalog
                        languages = exc.languages
                    if self._retryable(cause):
                        remaining = (retry_deadline - self._now()).total_seconds()
                        delay = min(
                            _RETRY_MAX_SECONDS,
                            _RETRY_BASE_SECONDS * 2**attempt,
                            max(0.0, remaining),
                        )
                        if delay > 0:
                            self.logger.warning(
                                "Shift Notice automatic preparation failed; retrying. "
                                "operation=automatic_prepare guild_id=%s config_id=%s "
                                "attempt=%s retry_delay=%s exception_class=%s",
                                spec.guild_id,
                                spec.config_id,
                                attempt,
                                delay,
                                type(cause).__name__,
                            )
                            await self._retry_sleep(delay)
                            attempt += 1
                            if self._now() >= retry_deadline:
                                payload = await self._failure_payload(
                                    spec,
                                    catalog,
                                    languages,
                                )
                                if payload is None:
                                    return
                                break
                            continue
                    self.logger.warning(
                        "Shift Notice automatic preparation failed. "
                        "operation=automatic_prepare stage=prepare guild_id=%s "
                        "config_id=%s destination_channel_id=%s target_boundary=%s "
                        "exception_class=%s",
                        spec.guild_id,
                        spec.config_id,
                        spec.channel_id,
                        spec.target_boundary.isoformat(),
                        type(cause).__name__,
                    )
                    payload = await self._failure_payload(spec, catalog, languages)
                    if payload is None:
                        return
                    break
                if prepared is None:
                    return
                payload, _, catalog, languages = prepared
                break

            if self._now() < spec.scheduled_tick:
                await self._sleep_until(spec.scheduled_tick)
            destination = await self._final_destination(spec)
            if destination is None:
                return
            try:
                await self._send_automatic_payload(destination, payload)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self.logger.warning(
                    "Shift Notice automatic send failed; delivery is ambiguous. "
                    "operation=automatic_send guild_id=%s config_id=%s "
                    "destination_channel_id=%s exception_class=%s",
                    spec.guild_id,
                    spec.config_id,
                    spec.channel_id,
                    type(exc).__name__,
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            self.logger.warning(
                "Shift Notice automatic task failed. operation=automatic_task "
                "guild_id=%s config_id=%s exception_class=%s",
                spec.guild_id,
                spec.config_id,
                type(exc).__name__,
            )

    async def _reschedule_future_tick_async(self, guild_id: int) -> None:
        try:
            config = await get_guild_config(guild_id)
            now = self._now()
            minute = None if config is None else config.minute_of_hour
            if (
                config is None
                or not config.feature_channel.is_enabled
                or minute is None
            ):
                await self._cancel_scheduled_delivery(guild_id)
                return
            tick = _strict_future_tick(now, minute)
            if tick is None:
                await self._cancel_scheduled_delivery(guild_id)
                return
            spec = self._config_tick_spec(config, tick)
            if spec is not None:
                await self._schedule_tick(spec)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            self.logger.warning(
                "Shift Notice future tick reschedule failed. "
                "operation=reschedule guild_id=%s exception_class=%s",
                guild_id,
                type(exc).__name__,
            )

    def _reschedule_future_tick(self, guild_id: int) -> asyncio.Task[None] | None:
        if self._is_unloading:
            return None
        try:
            task = asyncio.create_task(
                self._reschedule_future_tick_async(guild_id),
                name=f"shift-notice-reschedule-{guild_id}",
            )
        except RuntimeError:
            return None
        self._reschedule_tasks.add(task)
        task.add_done_callback(self._reschedule_tasks.discard)
        return task

    async def _dispatcher_pass(self) -> None:
        now = self._now()
        scheduled_tick = _following_exact_minute(now)
        try:
            configs = await self._enabled_configs()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            self.logger.warning(
                "Shift Notice dispatcher config query failed. operation=dispatch "
                "exception_class=%s",
                type(exc).__name__,
            )
            return
        for config in configs:
            if config.minute_of_hour != scheduled_tick.minute:
                continue
            try:
                spec = self._config_tick_spec(config, scheduled_tick)
                if spec is not None:
                    await self._schedule_tick(spec)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self.logger.warning(
                    "Shift Notice dispatcher guild pass failed. "
                    "operation=dispatch guild_id=%s exception_class=%s",
                    getattr(config, "guild_id", None),
                    type(exc).__name__,
                )

    @tasks.loop(minutes=1, reconnect=True)
    async def _dispatch_shift_notice_ticks(self) -> None:
        await self._dispatcher_pass()

    @_dispatch_shift_notice_ticks.before_loop
    async def _dispatch_shift_notice_ticks_before_loop(self) -> None:
        await self.bot.wait_until_ready()
        await self._sleep_until(_following_exact_minute(self._now()))

    async def _bootstrap_shift_notice(self) -> None:
        await self.bot.wait_until_ready()
        now = self._now()
        try:
            configs = await self._enabled_configs()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            self.logger.warning(
                "Shift Notice bootstrap config query failed. operation=bootstrap "
                "exception_class=%s",
                type(exc).__name__,
            )
            return
        for config in configs:
            minute = config.minute_of_hour
            if minute is None:
                continue
            tick = _strict_future_tick(now, minute)
            if tick is None:
                continue
            try:
                spec = self._config_tick_spec(config, tick)
                if spec is not None:
                    await self._schedule_tick(spec)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self.logger.warning(
                    "Shift Notice bootstrap guild pass failed. "
                    "operation=bootstrap guild_id=%s exception_class=%s",
                    getattr(config, "guild_id", None),
                    type(exc).__name__,
                )

    async def cog_load(self) -> None:
        self._is_unloading = False
        if not self._dispatcher.is_running():
            self._dispatcher.start()
        if self._bootstrap_task is None:
            self._bootstrap_task = asyncio.create_task(
                self._bootstrap_shift_notice(),
                name="shift-notice-bootstrap",
            )

    async def cog_unload(self) -> None:
        self._is_unloading = True
        if self._dispatcher.is_running():
            self._dispatcher.cancel()
        dispatcher_task = self._dispatcher.get_task()
        tasks_to_wait: list[asyncio.Task[object]] = []
        if dispatcher_task is not None:
            tasks_to_wait.append(dispatcher_task)
        if self._bootstrap_task is not None:
            tasks_to_wait.append(self._bootstrap_task)
        async with self._tick_registry_lock:
            tick_tasks = tuple(self._tick_tasks.values())
            self._tick_tasks.clear()
            self._tick_specs.clear()
            for task in tick_tasks:
                task.cancel()
        tasks_to_wait.extend(tick_tasks)
        tasks_to_wait.extend(self._reschedule_tasks)
        self._bootstrap_task = None
        self._reschedule_tasks.clear()
        for task in tasks_to_wait:
            task.cancel()
        if tasks_to_wait:
            await asyncio.gather(*tasks_to_wait, return_exceptions=True)

    async def _cleanup_after_disable(
        self,
        membership: FeatureChannel,
    ) -> str | None:
        await self._cancel_scheduled_delivery(membership.guild_id)
        return None

    async def _cleanup_before_clear(
        self,
        membership: FeatureChannel,
    ) -> str | None:
        await self._cancel_scheduled_delivery(membership.guild_id)
        return None

    @app_commands.command(
        name="send_latest",
        description="Resend the latest eligible shift handoff notice.",
    )
    @app_commands.check(
        FeatureChannelBase.feature_enabled_app_command_predicate(
            SHIFT_NOTICE_FEATURE_NAME,
            SHIFT_NOTICE_DISPLAY_NAME,
        )
    )
    async def send_latest(  # noqa: C901, PLR0911, PLR0915
        self,
        interaction: Interaction,
    ) -> None:
        source = require_guild_channel_source(
            interaction,
            action="send the latest Shift Notice",
        )
        try:
            config = await get_destination_config(
                source.guild.id,
                source.channel.id,
                require_enabled=True,
            )
        except Exception as exc:  # noqa: BLE001
            self._log_manual_failure(
                stage="config",
                guild_id=source.guild.id,
                config_id=None,
                destination_channel_id=source.channel.id,
                catalog=None,
                target_boundary=None,
                exc=exc,
            )
            await interaction.response.send_message(
                MANUAL_SEND_FAILURE_MESSAGE,
                ephemeral=True,
            )
            return
        if config is None:
            raise FeatureNotEnabled(self.feature_name, self.feature_display_name)
        if not is_usable_shift_notice_destination(source.channel, source.guild):
            self.logger.warning(
                "Shift Notice destination unusable. operation=send_latest "
                "stage=destination guild_id=%s config_id=%s "
                "destination_channel_id=%s",
                source.guild.id,
                config.id,
                source.channel.id,
            )
            await interaction.response.send_message(
                INVALID_DESTINATION_MESSAGE,
                ephemeral=True,
            )
            return
        if config.minute_of_hour is None:
            await interaction.response.send_message(
                "The Shift Notice minute is not configured yet.",
                ephemeral=True,
            )
            return

        try:
            catalog = await self._manager.load_source_catalog(source.guild.id)
        except Exception as exc:  # noqa: BLE001
            self._log_manual_failure(
                stage="catalog",
                guild_id=source.guild.id,
                config_id=config.id,
                destination_channel_id=source.channel.id,
                catalog=None,
                target_boundary=None,
                exc=exc,
            )
            await interaction.response.send_message(
                MANUAL_SEND_FAILURE_MESSAGE,
                ephemeral=True,
            )
            return
        if catalog.envelope_start is None or catalog.envelope_end is None:
            await interaction.response.send_message(
                "No eligible boundary exists for Shift Notice.",
                ephemeral=True,
            )
            return
        target_boundary = latest_reached_boundary(
            self._now(),
            config.minute_of_hour,
            catalog.envelope_start,
            catalog.envelope_end,
        )
        if target_boundary is None:
            await interaction.response.send_message(
                "No Shift Notice is available yet.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        members = tuple(source.guild.members)
        try:
            snapshot = await self._manager.build_snapshot(
                catalog,
                target_boundary,
                lambda labels: resolve_schedule_role_label_matches(labels, members),
            )
            render_input = _snapshot_render_input(snapshot)
            image_bytes = await asyncio.to_thread(self._renderer, render_input)
            languages = await get_announcement_languages(
                source.guild.id,
                self.logger,
            )
            spec = build_normal_message(
                snapshot,
                image_bytes,
                languages,
                upload_limit=source.guild.filesize_limit,
            )
        except Exception as exc:  # noqa: BLE001
            self._log_manual_failure(
                stage="prepare",
                guild_id=source.guild.id,
                config_id=config.id,
                destination_channel_id=source.channel.id,
                catalog=catalog,
                target_boundary=target_boundary,
                exc=exc,
            )
            await interaction.followup.send(
                MANUAL_SEND_FAILURE_MESSAGE,
                ephemeral=True,
            )
            return

        try:
            current = await get_destination_config(
                source.guild.id,
                source.channel.id,
                require_enabled=True,
            )
        except Exception as exc:  # noqa: BLE001
            self._log_manual_failure(
                stage="revalidate",
                guild_id=source.guild.id,
                config_id=config.id,
                destination_channel_id=source.channel.id,
                catalog=catalog,
                target_boundary=target_boundary,
                exc=exc,
            )
            await interaction.followup.send(
                MANUAL_SEND_FAILURE_MESSAGE,
                ephemeral=True,
            )
            return
        destination = source.guild.get_channel(source.channel.id)
        if (
            current is None
            or current.id != config.id
            or current.minute_of_hour != config.minute_of_hour
            or not is_usable_shift_notice_destination(destination, source.guild)
        ):
            await interaction.followup.send(MANUAL_STALE_MESSAGE, ephemeral=True)
            return

        if spec.image_bytes is None or spec.filename is None:
            await interaction.followup.send(
                MANUAL_SEND_FAILURE_MESSAGE,
                ephemeral=True,
            )
            return
        file = discord.File(BytesIO(spec.image_bytes), filename=spec.filename)
        try:
            await destination.send(
                file=file,
                embeds=list(spec.embeds),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except Exception as exc:  # noqa: BLE001
            self._log_manual_failure(
                stage="send",
                guild_id=source.guild.id,
                config_id=config.id,
                destination_channel_id=destination.id,
                catalog=catalog,
                target_boundary=target_boundary,
                exc=exc,
            )
            await interaction.followup.send(
                MANUAL_SEND_FAILURE_MESSAGE,
                ephemeral=True,
            )
            return
        await interaction.followup.send("Shift Notice sent.", ephemeral=True)

    def _log_manual_failure(  # noqa: PLR0913
        self,
        *,
        stage: str,
        guild_id: int,
        config_id: int | None,
        destination_channel_id: int,
        catalog: object | None,
        target_boundary: datetime | None,
        exc: Exception,
    ) -> None:
        sources = tuple(getattr(catalog, "complete_sources", ()))
        self.logger.warning(
            "Shift Notice operation failed. operation=send_latest stage=%s "
            "guild_id=%s config_id=%s destination_channel_id=%s source_ids=%s "
            "worksheet_ids=%s target_boundary=%s exception_class=%s "
            "exception_message=operation_failed",
            stage,
            guild_id,
            config_id,
            destination_channel_id,
            tuple(source.id for source in sources),
            tuple(source.final_schedule_worksheet_id for source in sources),
            None if target_boundary is None else target_boundary.isoformat(),
            type(exc).__name__,
        )


async def setup(bot: Rhoboto) -> None:
    await bot.add_cog(ShiftNotice(bot))
