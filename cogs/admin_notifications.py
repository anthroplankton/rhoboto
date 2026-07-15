from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import discord
from discord import ButtonStyle, Interaction, TextChannel, app_commands
from discord.ui import Button, View

from cogs.base.discord_context import require_guild_channel_source
from cogs.base.feature_channel_base import FeatureChannelBase, FeatureNotEnabled
from components.ui_admin_notifications import (
    ADMIN_NOTIFICATIONS_DISPLAY_NAME,
    ADMIN_NOTIFICATIONS_FEATURE_NAME,
    NOT_CONFIGURED_MESSAGE,
    STALE_SETTINGS_MESSAGE,
    AdminNotificationsSetupView,
    AdminNotificationsUIActions,
    ReplaceAdminNotificationsDestinationView,
    build_admin_notifications_settings_panel,
    lead_time_error_message,
    mention_selection_error_message,
)
from components.ui_auto_guide import AUTO_GUIDE_GOOGLE_SHEETS_LABEL
from components.ui_settings_flow import (
    SETTINGS_STORAGE_EXCEPTIONS,
    SettingsPanel,
    prepare_replacement_settings_view,
    send_current_panel_followup,
    send_settings_partial_success,
    send_settings_storage_error,
    send_settings_view_followup,
)
from models.admin_notifications import (
    AdminNotificationDeliveryStatus,
    AdminNotificationsConfig,
)
from utils.admin_notifications import (
    MentionSelectionError,
    ReminderMessageError,
    build_reminder_message,
    milestone_datetime,
    parse_reminder_lead_minutes,
    resolve_saved_mentions,
    validate_selected_mentions,
)
from utils.admin_notifications_manager import (
    AdminNotificationsStaleStateError,
    DeliverySchedule,
    ReconcileResult,
    claim_destination,
    complete_setup,
    get_delivery_with_context,
    get_destination_config,
    get_guild_config,
    mark_delivery_expired,
    mark_delivery_failed,
    mark_delivery_sent,
    reconcile_occurrences,
    record_delivery_attempt,
    replace_unavailable_destination,
    save_lead_time,
    save_mentions,
    save_shift_timeline_reminders_enabled,
)
from utils.announcement_languages import get_announcement_languages
from utils.key_async_lock import KeyAsyncLock

if TYPE_CHECKING:
    from bot import Rhoboto
    from models.feature_channel import FeatureChannel


logger = logging.getLogger(__name__)

INVALID_DESTINATION_MESSAGE = (
    "⚠️ Admin Notifications requires a normal text channel where the bot can "
    "view the channel and send messages."
)
REPLACEMENT_PROMPT = (
    "‼️ The configured Admin Notifications channel is unavailable. "
    "Replace it with this channel?"
)


class _RetryableDeliveryError(RuntimeError):
    """A delivery may succeed after a later permission or storage retry."""


SleepUntil = Callable[[datetime], Awaitable[None]]
RetrySleep = Callable[[float], Awaitable[None]]


def is_usable_admin_notification_destination(
    channel: object,
    guild: object,
) -> bool:
    if not isinstance(channel, TextChannel):
        return False
    bot_member = getattr(guild, "me", None)
    if bot_member is None:
        return False
    permissions = channel.permissions_for(bot_member)
    return bool(permissions.view_channel and permissions.send_messages)


def configured_elsewhere_message(channel_id: int) -> str:
    return (
        "Admin Notifications is already configured in "
        f"<#{channel_id}>. Use `/admin_notifications settings` there."
    )


class AdminNotifications(
    FeatureChannelBase, group_name=ADMIN_NOTIFICATIONS_FEATURE_NAME
):
    feature_name = ADMIN_NOTIFICATIONS_FEATURE_NAME
    feature_display_name = ADMIN_NOTIFICATIONS_DISPLAY_NAME

    def __init__(
        self,
        bot: Rhoboto,
        *,
        now: Callable[[], datetime] | None = None,
        sleep_until: SleepUntil | None = None,
        retry_sleep: RetrySleep | None = None,
    ) -> None:
        super().__init__(bot)
        self._now = now or (lambda: datetime.now(UTC))
        self._sleep_until = sleep_until or discord.utils.sleep_until
        self._retry_sleep = retry_sleep or asyncio.sleep
        self._guild_lock = KeyAsyncLock()
        self._delivery_tasks: dict[int, asyncio.Task[None]] = {}
        self._delivery_specs: dict[int, DeliverySchedule] = {}
        self._reconcile_events: dict[int, asyncio.Event] = {}
        self._reconcile_tasks: dict[int, asyncio.Task[None]] = {}
        self._bootstrap_task: asyncio.Task[None] | None = None
        self._is_unloading = False

    def _ui_actions(self) -> AdminNotificationsUIActions:
        return AdminNotificationsUIActions(
            setup_is_current=self._setup_is_current,
            save_setup=self._save_setup,
            replace_destination=self._replace_destination,
            save_lead=self._save_lead,
            save_mentions=self._save_mentions,
            set_shift_reminders=self._set_shift_reminders,
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
        if not is_usable_admin_notification_destination(source.channel, source.guild):
            await interaction.response.send_message(
                INVALID_DESTINATION_MESSAGE,
                ephemeral=True,
            )
            return

        claim = None
        reconcile_error: Exception | None = None
        async with self._guild_lock(source.guild.id):
            try:
                claim = await claim_destination(source.guild.id, source.channel.id)
                if claim.owns_requested_destination:
                    try:
                        await self._reconcile_guild_locked(source.guild.id)
                    except Exception as exc:  # noqa: BLE001
                        reconcile_error = exc
            except Exception as exc:  # noqa: BLE001
                await self._send_interaction_storage_error_or_raise(
                    interaction,
                    exc,
                    source=source,
                    operation="enable",
                )
                return

        if claim is None:
            return
        if reconcile_error is not None:
            self.request_reconcile_guild(source.guild.id)

        if claim.owns_requested_destination:
            await interaction.response.send_message(
                f"Feature {self.feature_display_name} enabled in this channel.",
                ephemeral=True,
            )
            await self.setup_after_enable(interaction)
            return

        stored_channel = source.guild.get_channel(claim.channel_id)
        if is_usable_admin_notification_destination(stored_channel, source.guild):
            await interaction.response.send_message(
                configured_elsewhere_message(claim.channel_id),
                ephemeral=True,
            )
            return

        view = ReplaceAdminNotificationsDestinationView(
            requesting_user_id=interaction.user.id,
            config_id=claim.config_id,
            expected_channel_id=claim.channel_id,
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
            ADMIN_NOTIFICATIONS_FEATURE_NAME,
            ADMIN_NOTIFICATIONS_DISPLAY_NAME,
        )
    )
    async def settings(self, interaction: Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        await self.setup_after_enable(interaction)

    async def setup_after_enable(self, interaction: Interaction) -> None:
        source = require_guild_channel_source(
            interaction,
            action="show Admin Notifications settings",
        )
        config = await get_destination_config(source.guild.id, source.channel.id)
        if config is None:
            await interaction.followup.send(NOT_CONFIGURED_MESSAGE, ephemeral=True)
            return
        actions = self._ui_actions()
        if config.reminder_lead_minutes is None:
            view = AdminNotificationsSetupView(
                requesting_user_id=interaction.user.id,
                config_id=config.id,
                expected_updated_at=config.updated_at,
                actions=actions,
                expected_channel_id=source.channel.id,
            )
            await send_settings_view_followup(
                interaction,
                view=view,
                content=(
                    "Admin Notifications is not yet configured for this channel. "
                    "Click below to set up."
                ),
            )
            return

        panel = await self._build_settings_panel(
            source.guild,
            source.channel,
            config,
            actions=actions,
        )
        await send_current_panel_followup(interaction, panel)

    async def _build_settings_panel(
        self,
        guild: object,
        destination: TextChannel,
        config: AdminNotificationsConfig,
        *,
        actions: AdminNotificationsUIActions,
        is_save_action: bool = False,
    ) -> SettingsPanel:
        mentions = resolve_saved_mentions(
            guild,
            destination,
            role_ids=config.mention_role_ids,
            user_ids=config.mention_user_ids,
        )
        return build_admin_notifications_settings_panel(
            config,
            destination=destination,
            mentions=mentions,
            scheduled_reminder_count=self.scheduled_reminder_count(config.guild_id),
            actions=actions,
            is_save_action=is_save_action,
        )

    async def _setup_is_current(
        self,
        config_id: int,
        expected_updated_at: datetime,
    ) -> bool:
        config = await AdminNotificationsConfig.get_or_none(id=config_id)
        return config is not None and config.updated_at == expected_updated_at

    async def _owned_config(
        self,
        interaction: Interaction,
    ) -> tuple[object, TextChannel, AdminNotificationsConfig] | None:
        source = require_guild_channel_source(
            interaction,
            action="update Admin Notifications settings",
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
        panel = await self._build_settings_panel(
            guild,
            destination,
            config,
            actions=self._ui_actions(),
            is_save_action=True,
        )
        if current_view is None:
            await send_current_panel_followup(interaction, panel)
            return
        replacement = prepare_replacement_settings_view(current_view, panel.view)
        await interaction.edit_original_response(
            content=None,
            embed=panel.embed,
            view=replacement,
        )

    async def _run_reconcile_after_save(
        self,
        interaction: Interaction,
        guild_id: int,
        *,
        operation: str,
    ) -> bool:
        try:
            await self._reconcile_guild_locked(guild_id)
        except SETTINGS_STORAGE_EXCEPTIONS as exc:
            self.request_reconcile_guild(guild_id)
            await send_settings_partial_success(
                interaction,
                exc,
                operation=operation,
                feature_name=self.feature_name,
                log=self.logger,
            )
            return False
        return True

    async def _save_setup(
        self,
        interaction: Interaction,
        config_id: int,
        expected_updated_at: datetime,
        raw_lead: str,
    ) -> None:
        owned = await self._owned_config(interaction)
        if owned is None:
            return
        guild, destination, _ = owned
        try:
            new_lead = parse_reminder_lead_minutes(raw_lead)
        except ValueError:
            await interaction.followup.send(lead_time_error_message(), ephemeral=True)
            return
        async with self._guild_lock(guild.id):
            try:
                await complete_setup(
                    config_id,
                    expected_updated_at=expected_updated_at,
                    expected_lead=None,
                    new_lead=new_lead,
                )
                await self._run_reconcile_after_save(
                    interaction,
                    guild.id,
                    operation="admin_notifications_setup",
                )
            except AdminNotificationsStaleStateError:
                await interaction.followup.send(STALE_SETTINGS_MESSAGE, ephemeral=True)
                return
            except SETTINGS_STORAGE_EXCEPTIONS as exc:
                await send_settings_storage_error(
                    interaction,
                    exc,
                    operation="admin_notifications_setup",
                    feature_name=self.feature_name,
                    log=self.logger,
                )
                return
        await self._refresh_settings_response(
            interaction,
            guild,
            destination,
            current_view=None,
        )

    async def _save_lead(  # noqa: PLR0913
        self,
        interaction: Interaction,
        config_id: int,
        expected_updated_at: datetime,
        expected_lead: int | None,
        raw_lead: str,
        current_view: View,
    ) -> None:
        owned = await self._owned_config(interaction)
        if owned is None:
            return
        guild, destination, _ = owned
        try:
            new_lead = parse_reminder_lead_minutes(raw_lead)
        except ValueError:
            await interaction.followup.send(lead_time_error_message(), ephemeral=True)
            return
        async with self._guild_lock(guild.id):
            try:
                await save_lead_time(
                    config_id,
                    expected_updated_at=expected_updated_at,
                    expected_lead=expected_lead,
                    new_lead=new_lead,
                )
                await self._run_reconcile_after_save(
                    interaction,
                    guild.id,
                    operation="admin_notifications_lead_time",
                )
            except AdminNotificationsStaleStateError:
                await interaction.followup.send(STALE_SETTINGS_MESSAGE, ephemeral=True)
                return
            except SETTINGS_STORAGE_EXCEPTIONS as exc:
                await send_settings_storage_error(
                    interaction,
                    exc,
                    operation="admin_notifications_lead_time",
                    feature_name=self.feature_name,
                    log=self.logger,
                )
                return
        await self._refresh_settings_response(
            interaction,
            guild,
            destination,
            current_view=current_view,
        )

    async def _save_mentions(  # noqa: PLR0913
        self,
        interaction: Interaction,
        config_id: int,
        expected_updated_at: datetime,
        expected_role_ids: list[int],
        expected_user_ids: list[int],
        values: object,
        current_view: View,
    ) -> None:
        owned = await self._owned_config(interaction)
        if owned is None:
            return
        guild, destination, _ = owned
        try:
            role_ids, user_ids = validate_selected_mentions(
                guild,
                destination,
                values,
            )
        except MentionSelectionError:
            await interaction.followup.send(
                mention_selection_error_message(),
                ephemeral=True,
            )
            return
        async with self._guild_lock(guild.id):
            try:
                await save_mentions(
                    config_id,
                    expected_updated_at=expected_updated_at,
                    expected_role_ids=expected_role_ids,
                    expected_user_ids=expected_user_ids,
                    new_role_ids=role_ids,
                    new_user_ids=user_ids,
                )
                await self._run_reconcile_after_save(
                    interaction,
                    guild.id,
                    operation="admin_notifications_mentions",
                )
            except AdminNotificationsStaleStateError:
                await interaction.followup.send(STALE_SETTINGS_MESSAGE, ephemeral=True)
                return
            except SETTINGS_STORAGE_EXCEPTIONS as exc:
                await send_settings_storage_error(
                    interaction,
                    exc,
                    operation="admin_notifications_mentions",
                    feature_name=self.feature_name,
                    log=self.logger,
                )
                return
        await self._refresh_settings_response(
            interaction,
            guild,
            destination,
            current_view=current_view,
        )

    async def _set_shift_reminders(  # noqa: PLR0913
        self,
        interaction: Interaction,
        config_id: int,
        expected_updated_at: datetime,
        expected_enabled: bool,  # noqa: FBT001
        new_enabled: bool,  # noqa: FBT001
        current_view: View,
    ) -> None:
        owned = await self._owned_config(interaction)
        if owned is None:
            return
        guild, destination, _ = owned
        async with self._guild_lock(guild.id):
            try:
                await save_shift_timeline_reminders_enabled(
                    config_id,
                    expected_updated_at=expected_updated_at,
                    expected_enabled=expected_enabled,
                    new_enabled=new_enabled,
                )
                await self._run_reconcile_after_save(
                    interaction,
                    guild.id,
                    operation="admin_notifications_shift_reminders",
                )
            except AdminNotificationsStaleStateError:
                await interaction.followup.send(STALE_SETTINGS_MESSAGE, ephemeral=True)
                return
            except SETTINGS_STORAGE_EXCEPTIONS as exc:
                await send_settings_storage_error(
                    interaction,
                    exc,
                    operation="admin_notifications_shift_reminders",
                    feature_name=self.feature_name,
                    log=self.logger,
                )
                return
        await self._refresh_settings_response(
            interaction,
            guild,
            destination,
            current_view=current_view,
        )

    async def _replace_destination(
        self,
        interaction: Interaction,
        config_id: int,
        expected_channel_id: int,
    ) -> None:
        source = require_guild_channel_source(
            interaction,
            action="replace Admin Notifications destination",
        )
        if not is_usable_admin_notification_destination(source.channel, source.guild):
            await interaction.edit_original_response(
                content=INVALID_DESTINATION_MESSAGE,
                view=None,
            )
            return
        async with self._guild_lock(source.guild.id):
            config = await get_guild_config(source.guild.id)
            old_channel = source.guild.get_channel(
                config.feature_channel.channel_id if config is not None else 0
            )
            if (
                config is None
                or config.id != config_id
                or config.feature_channel.channel_id != expected_channel_id
                or is_usable_admin_notification_destination(old_channel, source.guild)
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
                await self._run_reconcile_after_save(
                    interaction,
                    source.guild.id,
                    operation="admin_notifications_destination_replacement",
                )
            except AdminNotificationsStaleStateError:
                await interaction.edit_original_response(
                    content=STALE_SETTINGS_MESSAGE,
                    view=None,
                )
                return
            except SETTINGS_STORAGE_EXCEPTIONS as exc:
                await send_settings_storage_error(
                    interaction,
                    exc,
                    operation="admin_notifications_destination_replacement",
                    feature_name=self.feature_name,
                    log=self.logger,
                )
                return
        await interaction.edit_original_response(
            content=f"Feature {self.feature_display_name} enabled in this channel.",
            view=None,
        )
        await self.setup_after_enable(interaction)

    async def _disable_channel(self, guild_id: int, channel_id: int) -> bool:
        async with self._guild_lock(guild_id):
            result = await super()._disable_channel(guild_id, channel_id)
            if result:
                await self._cancel_delivery_tasks_for_guild(guild_id)
            return result

    async def _clear_feature_settings(self, guild_id: int, channel_id: int) -> None:
        async with self._guild_lock(guild_id):
            await self._cancel_delivery_tasks_for_guild(guild_id)
            await self._cancel_reconcile_worker(guild_id)
            await super()._clear_feature_settings(guild_id, channel_id)

    async def _cancel_delivery_tasks_for_guild(self, guild_id: int) -> None:
        tasks = [
            task
            for delivery_id, task in self._delivery_tasks.items()
            if self._delivery_specs.get(delivery_id, None) is not None
            and self._delivery_specs[delivery_id].guild_id == guild_id
        ]
        for delivery_id in [
            delivery_id
            for delivery_id, spec in self._delivery_specs.items()
            if spec.guild_id == guild_id
        ]:
            self._delivery_specs.pop(delivery_id, None)
            self._delivery_tasks.pop(delivery_id, None)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _cancel_reconcile_worker(self, guild_id: int) -> None:
        task = self._reconcile_tasks.pop(guild_id, None)
        self._reconcile_events.pop(guild_id, None)
        if task is None:
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def _reconcile_guild_locked(self, guild_id: int) -> ReconcileResult:
        result = await reconcile_occurrences(guild_id, self._now())
        self._apply_delivery_schedules(guild_id, result.schedules)
        return result

    async def reconcile_guild(self, guild_id: int) -> ReconcileResult:
        async with self._guild_lock(guild_id):
            return await self._reconcile_guild_locked(guild_id)

    def request_reconcile_guild(self, guild_id: int) -> None:
        if self._is_unloading:
            return
        event = self._reconcile_events.setdefault(guild_id, asyncio.Event())
        event.set()
        current = self._reconcile_tasks.get(guild_id)
        if current is not None and not current.done():
            return
        task = asyncio.create_task(
            self._run_reconcile_requests(guild_id, event),
            name=f"admin-notifications-reconcile-{guild_id}",
        )
        self._reconcile_tasks[guild_id] = task
        task.add_done_callback(
            lambda completed: self._remove_reconcile_task_if_current(
                guild_id,
                completed,
            )
        )

    async def _run_reconcile_requests(
        self,
        guild_id: int,
        event: asyncio.Event,
    ) -> None:
        failure_count = 0
        while True:
            event.clear()
            try:
                await self.reconcile_guild(guild_id)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                delay = min(60 * 2 ** min(failure_count, 6), 3600)
                failure_count += 1
                self.logger.warning(
                    "Admin Notifications reconciliation failed; retrying in %s "
                    "seconds. Guild=%s",
                    delay,
                    guild_id,
                    exc_info=True,
                )
                await self._wait_for_reconcile_request(event, delay)
                continue
            failure_count = 0
            if not event.is_set():
                return

    async def _wait_for_reconcile_request(
        self,
        event: asyncio.Event,
        delay: float,
    ) -> None:
        try:
            await asyncio.wait_for(event.wait(), timeout=delay)
        except TimeoutError:
            return

    def _remove_reconcile_task_if_current(
        self,
        guild_id: int,
        task: asyncio.Task[None],
    ) -> None:
        if self._reconcile_tasks.get(guild_id) is not task:
            return
        del self._reconcile_tasks[guild_id]
        event = self._reconcile_events.get(guild_id)
        if event is None:
            return
        if self._is_unloading:
            self._reconcile_events.pop(guild_id, None)
        elif event.is_set():
            self.request_reconcile_guild(guild_id)
        else:
            self._reconcile_events.pop(guild_id, None)

    def _apply_delivery_schedules(
        self,
        guild_id: int,
        schedules: tuple[DeliverySchedule, ...],
    ) -> None:
        desired = {
            schedule.delivery_id: schedule
            for schedule in schedules
            if schedule.guild_id == guild_id
        }
        current_ids = {
            delivery_id
            for delivery_id, schedule in self._delivery_specs.items()
            if schedule.guild_id == guild_id
        }
        for delivery_id in current_ids - desired.keys():
            task = self._delivery_tasks.pop(delivery_id, None)
            self._delivery_specs.pop(delivery_id, None)
            if task is not None:
                task.cancel()

        for delivery_id, schedule in desired.items():
            existing = self._delivery_specs.get(delivery_id)
            existing_task = self._delivery_tasks.get(delivery_id)
            if (
                existing == schedule
                and existing_task is not None
                and not existing_task.done()
            ):
                continue
            if existing_task is not None:
                existing_task.cancel()
            task = asyncio.create_task(
                self._run_delivery(schedule),
                name=f"admin-notifications-delivery-{delivery_id}",
            )
            self._delivery_specs[delivery_id] = schedule
            self._delivery_tasks[delivery_id] = task
            task.add_done_callback(
                lambda completed, delivery_id=delivery_id: (
                    self._remove_delivery_task_if_current(delivery_id, completed)
                )
            )

    def _remove_delivery_task_if_current(
        self,
        delivery_id: int,
        task: asyncio.Task[None],
    ) -> None:
        if self._delivery_tasks.get(delivery_id) is not task:
            return
        self._delivery_tasks.pop(delivery_id, None)
        self._delivery_specs.pop(delivery_id, None)

    async def _run_delivery(self, schedule: DeliverySchedule) -> None:
        await self._sleep_until(schedule.wake_at)
        failure_count = 0
        while True:
            try:
                finished = await self._deliver_once(schedule)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                backoff = min(60 * 2 ** min(failure_count, 6), 3600)
                remaining = max(
                    (schedule.milestone_at - self._now()).total_seconds(),
                    0,
                )
                delay = min(backoff, remaining)
                failure_count += 1
                self.logger.warning(
                    "Admin notification delivery failed; retrying in %s seconds. "
                    "Guild=%s Delivery=%s",
                    delay,
                    schedule.guild_id,
                    schedule.delivery_id,
                    exc_info=True,
                )
                if delay > 0:
                    await self._retry_sleep(delay)
                else:
                    await asyncio.sleep(0)
                continue
            if finished:
                return

    def _guild_for_delivery(self, guild_id: int) -> object | None:
        getter = getattr(self.bot, "get_guild", None)
        if callable(getter):
            guild = getter(guild_id)
            if guild is not None:
                return guild
        return next(
            (
                guild
                for guild in getattr(self.bot, "guilds", ())
                if guild.id == guild_id
            ),
            None,
        )

    async def _mark_delivery_at_cutoff(
        self,
        delivery: object,
    ) -> None:
        try:
            if getattr(delivery, "attempted_at", None) is None:
                await mark_delivery_expired(delivery.id)
            else:
                await mark_delivery_failed(delivery.id)
        except AdminNotificationsStaleStateError:
            return

    async def _recover_delivery_from_history(
        self,
        delivery: object,
        destination: TextChannel,
        guild: object,
    ) -> bool:
        permissions = destination.permissions_for(getattr(guild, "me", None))
        if not getattr(permissions, "read_message_history", False):
            return False
        history_method = getattr(destination, "history", None)
        if not callable(history_method):
            return False
        try:
            history = history_method(
                limit=100,
                after=delivery.attempted_at - timedelta(minutes=1),
                oldest_first=True,
            )
            async for message in history:
                author = getattr(message, "author", None)
                bot_user = getattr(self.bot, "user", None)
                if (
                    bot_user is not None
                    and author is not None
                    and getattr(author, "id", None) == bot_user.id
                    and str(getattr(message, "nonce", None))
                    == str(delivery.delivery_nonce)
                ):
                    with suppress(AdminNotificationsStaleStateError):
                        await mark_delivery_sent(delivery.id, message.id)
                    return True
        except (discord.Forbidden, discord.HTTPException):
            self.logger.warning(
                "Admin notification history recovery was unavailable. "
                "Guild=%s Delivery=%s",
                getattr(delivery.admin_notifications_config, "guild_id", None),
                delivery.id,
            )
        except Exception:  # noqa: BLE001
            self.logger.warning(
                "Admin notification history recovery failed. Guild=%s Delivery=%s",
                getattr(delivery.admin_notifications_config, "guild_id", None),
                delivery.id,
                exc_info=True,
            )
        return False

    async def _deliver_once(  # noqa: PLR0911
        self,
        schedule: DeliverySchedule,
    ) -> bool:
        guild = self._guild_for_delivery(schedule.guild_id)
        if guild is None:
            raise _RetryableDeliveryError
        async with self._guild_lock(schedule.guild_id):
            try:
                delivery = await get_delivery_with_context(schedule.delivery_id)
            except AdminNotificationsStaleStateError:
                self.request_reconcile_guild(schedule.guild_id)
                return True

            config = delivery.admin_notifications_config
            shift_register = delivery.shift_register
            notification_feature = config.feature_channel
            shift_feature = shift_register.feature_channel
            if (
                config.id != schedule.config_id
                or config.guild_id != schedule.guild_id
                or notification_feature.guild_id != schedule.guild_id
                or not notification_feature.is_enabled
                or config.reminder_lead_minutes is None
                or not config.shift_timeline_reminders_enabled
                or shift_register.id != schedule.shift_register_id
                or shift_feature.guild_id != schedule.guild_id
                or delivery.milestone_kind != schedule.milestone_kind
                or delivery.milestone_at != schedule.milestone_at
                or delivery.delivery_nonce != schedule.delivery_nonce
                or delivery.status != AdminNotificationDeliveryStatus.SCHEDULED
                or delivery.message_id is not None
            ):
                self.request_reconcile_guild(schedule.guild_id)
                return True

            current_milestone = milestone_datetime(
                shift_register,
                delivery.milestone_kind,
            )
            expected_reminder = (
                current_milestone - timedelta(minutes=config.reminder_lead_minutes)
                if current_milestone is not None
                else None
            )
            if (
                current_milestone != delivery.milestone_at
                or expected_reminder != delivery.reminder_at
                or expected_reminder != schedule.reminder_at
            ):
                self.request_reconcile_guild(schedule.guild_id)
                return True

            now = self._now()
            if current_milestone is None:
                self.request_reconcile_guild(schedule.guild_id)
                return True
            if current_milestone <= now:
                await self._mark_delivery_at_cutoff(delivery)
                return True

            destination_id = notification_feature.channel_id
            destination = getattr(guild, "get_channel", lambda _: None)(destination_id)
            if not is_usable_admin_notification_destination(destination, guild):
                raise _RetryableDeliveryError

            if (
                delivery.attempted_at is not None
                and await self._recover_delivery_from_history(
                    delivery,
                    destination,
                    guild,
                )
            ):
                return True

            mentions = resolve_saved_mentions(
                guild,
                destination,
                role_ids=config.mention_role_ids,
                user_ids=config.mention_user_ids,
            )
            languages = await get_announcement_languages(
                schedule.guild_id,
                self.logger,
            )
            try:
                reminder = build_reminder_message(
                    shift_register=shift_register,
                    kind=delivery.milestone_kind,
                    milestone_at=delivery.milestone_at,
                    source_channel=f"<#{shift_feature.channel_id}>",
                    languages=languages,
                    mentions=mentions,
                )
            except ReminderMessageError:
                await mark_delivery_failed(delivery.id)
                self.logger.exception(
                    "Admin notification message construction failed. "
                    "Guild=%s Delivery=%s Kind=%s",
                    schedule.guild_id,
                    delivery.id,
                    delivery.milestone_kind.value,
                )
                return True

            view = View(timeout=None)
            view.add_item(
                Button(
                    label=AUTO_GUIDE_GOOGLE_SHEETS_LABEL,
                    emoji="👀",
                    style=ButtonStyle.link,
                    url=reminder.sheet_url,
                )
            )
            await record_delivery_attempt(delivery.id, now)
            message = await destination.send(
                reminder.content,
                allowed_mentions=reminder.allowed_mentions,
                view=view,
                nonce=delivery.delivery_nonce,
            )
            await mark_delivery_sent(delivery.id, message.id)
            return True

    def scheduled_reminder_count(self, guild_id: int) -> int:
        return sum(spec.guild_id == guild_id for spec in self._delivery_specs.values())

    async def _cleanup_after_disable(
        self,
        membership: FeatureChannel,
    ) -> str | None:
        self.request_reconcile_guild(membership.guild_id)
        return None

    async def cog_load(self) -> None:
        self._bootstrap_task = asyncio.create_task(
            self._bootstrap_notifications(),
            name="admin-notifications-bootstrap",
        )

    async def _bootstrap_notifications(self) -> None:
        try:
            await self.bot.wait_until_ready()
            guild_ids = await AdminNotificationsConfig.all().values_list(
                "guild_id",
                flat=True,
            )
            for guild_id in dict.fromkeys(guild_ids):
                self.request_reconcile_guild(guild_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            self.logger.exception("Admin Notifications bootstrap failed.")

    async def cog_unload(self) -> None:
        self._is_unloading = True
        tasks: list[asyncio.Task[object]] = []
        if self._bootstrap_task is not None:
            tasks.append(self._bootstrap_task)
        tasks.extend(self._reconcile_tasks.values())
        tasks.extend(self._delivery_tasks.values())
        self._bootstrap_task = None
        self._reconcile_tasks.clear()
        self._reconcile_events.clear()
        self._delivery_tasks.clear()
        self._delivery_specs.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


async def setup(bot: Rhoboto) -> None:
    await bot.add_cog(AdminNotifications(bot))
