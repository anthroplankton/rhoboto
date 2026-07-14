from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime

import discord

from models.shift_timeline_event_state import ShiftTimelineEventKind

ShiftTimelineEventKey = tuple[int, ShiftTimelineEventKind]
ShiftTimelineHandler = Callable[
    [int, ShiftTimelineEventKind, datetime, int],
    Awaitable[None],
]


class ShiftTimelineScheduler:
    def __init__(
        self,
        handler: ShiftTimelineHandler,
        *,
        sleep_until: Callable[[datetime], Awaitable[None]] = discord.utils.sleep_until,
        retry_sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        logger: logging.Logger | None = None,
    ) -> None:
        self._handler = handler
        self._sleep_until = sleep_until
        self._retry_sleep = retry_sleep
        self._logger = logger if logger is not None else logging.getLogger(__name__)
        self._tasks: dict[ShiftTimelineEventKey, asyncio.Task[None]] = {}

    def schedule(
        self,
        *,
        shift_register_id: int,
        event_kind: ShiftTimelineEventKind,
        scheduled_at: datetime,
        delivery_nonce: int,
    ) -> None:
        key = (shift_register_id, event_kind)
        self.cancel(shift_register_id, event_kind)
        task = asyncio.create_task(
            self._run(
                shift_register_id=shift_register_id,
                event_kind=event_kind,
                scheduled_at=scheduled_at,
                delivery_nonce=delivery_nonce,
            ),
            name=f"shift-timeline-{shift_register_id}-{event_kind.value}",
        )
        self._tasks[key] = task
        task.add_done_callback(
            lambda completed_task: self._remove_if_current(key, completed_task)
        )

    def cancel(
        self,
        shift_register_id: int,
        event_kind: ShiftTimelineEventKind,
    ) -> None:
        task = self._tasks.pop((shift_register_id, event_kind), None)
        if task is not None:
            task.cancel()

    def cancel_shift_register(self, shift_register_id: int) -> None:
        for event_kind in ShiftTimelineEventKind:
            self.cancel(shift_register_id, event_kind)

    async def close(self) -> None:
        tasks = list(self._tasks.values())
        self._tasks.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _run(
        self,
        *,
        shift_register_id: int,
        event_kind: ShiftTimelineEventKind,
        scheduled_at: datetime,
        delivery_nonce: int,
    ) -> None:
        await self._sleep_until(scheduled_at)
        attempt = 0
        while True:
            try:
                await self._handler(
                    shift_register_id,
                    event_kind,
                    scheduled_at,
                    delivery_nonce,
                )
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                delay = min(60 * 2**attempt, 3600)
                attempt += 1
                self._logger.warning(
                    "Shift timeline event failed; retrying in %s seconds. "
                    "shift_register=%s event=%s",
                    delay,
                    shift_register_id,
                    event_kind.value,
                    exc_info=True,
                )
                await self._retry_sleep(delay)
                continue
            return

    def _remove_if_current(
        self,
        key: ShiftTimelineEventKey,
        task: asyncio.Task[None],
    ) -> None:
        if self._tasks.get(key) is task:
            del self._tasks[key]
