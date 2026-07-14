from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from models.shift_timeline_event_state import ShiftTimelineEventKind
from utils.shift_timeline_scheduler import ShiftTimelineScheduler

EVENT_KIND = ShiftTimelineEventKind.SUBMISSION_DEADLINE
DEADLINE = datetime(2026, 7, 14, 12, 30, tzinfo=UTC)


@pytest.mark.asyncio
async def test_future_schedule_sleeps_until_deadline_and_calls_handler() -> None:
    sleep_calls: list[datetime] = []
    handler_calls: list[tuple[int, ShiftTimelineEventKind, datetime, int]] = []
    handler_called = asyncio.Event()

    async def sleep_until(deadline: datetime) -> None:
        sleep_calls.append(deadline)

    async def handler(
        shift_register_id: int,
        event_kind: ShiftTimelineEventKind,
        scheduled_at: datetime,
        delivery_nonce: int,
    ) -> None:
        handler_calls.append(
            (shift_register_id, event_kind, scheduled_at, delivery_nonce)
        )
        handler_called.set()

    scheduler = ShiftTimelineScheduler(handler, sleep_until=sleep_until)
    scheduler.schedule(
        shift_register_id=42,
        event_kind=EVENT_KIND,
        scheduled_at=DEADLINE,
        delivery_nonce=123,
    )

    await asyncio.wait_for(handler_called.wait(), timeout=1)
    assert sleep_calls == [DEADLINE]
    assert handler_calls == [(42, EVENT_KIND, DEADLINE, 123)]
    await scheduler.close()


@pytest.mark.asyncio
async def test_past_deadline_runs_with_immediate_sleep() -> None:
    sleep_calls: list[datetime] = []
    handler_called = asyncio.Event()

    async def sleep_until(deadline: datetime) -> None:
        sleep_calls.append(deadline)

    async def handler(*_args: object) -> None:
        handler_called.set()

    scheduler = ShiftTimelineScheduler(handler, sleep_until=sleep_until)
    scheduler.schedule(
        shift_register_id=42,
        event_kind=EVENT_KIND,
        scheduled_at=datetime(2020, 1, 1, tzinfo=UTC),
        delivery_nonce=123,
    )

    await asyncio.wait_for(handler_called.wait(), timeout=1)
    assert sleep_calls == [datetime(2020, 1, 1, tzinfo=UTC)]
    await scheduler.close()


@pytest.mark.asyncio
async def test_replacing_key_cancels_old_task_and_runs_only_replacement() -> None:
    calls: list[int] = []
    old_started = asyncio.Event()
    replacement_done = asyncio.Event()

    async def sleep_until(_deadline: datetime) -> None:
        return

    async def handler(
        _shift_register_id: int,
        _event_kind: ShiftTimelineEventKind,
        _scheduled_at: datetime,
        delivery_nonce: int,
    ) -> None:
        calls.append(delivery_nonce)
        if delivery_nonce == 1:
            old_started.set()
            await asyncio.Future()
        replacement_done.set()

    scheduler = ShiftTimelineScheduler(handler, sleep_until=sleep_until)
    scheduler.schedule(
        shift_register_id=42,
        event_kind=EVENT_KIND,
        scheduled_at=DEADLINE,
        delivery_nonce=1,
    )
    await asyncio.wait_for(old_started.wait(), timeout=1)

    scheduler.schedule(
        shift_register_id=42,
        event_kind=EVENT_KIND,
        scheduled_at=DEADLINE,
        delivery_nonce=2,
    )
    await asyncio.wait_for(replacement_done.wait(), timeout=1)
    await asyncio.sleep(0)

    assert calls == [1, 2]
    await scheduler.close()


@pytest.mark.asyncio
async def test_cancel_operations_and_close_are_idempotent() -> None:
    async def sleep_until(_deadline: datetime) -> None:
        await asyncio.Future()

    async def handler(*_args: object) -> None:
        raise AssertionError

    scheduler = ShiftTimelineScheduler(handler, sleep_until=sleep_until)
    scheduler.schedule(
        shift_register_id=42,
        event_kind=EVENT_KIND,
        scheduled_at=DEADLINE,
        delivery_nonce=1,
    )
    scheduler.schedule(
        shift_register_id=43,
        event_kind=EVENT_KIND,
        scheduled_at=DEADLINE,
        delivery_nonce=2,
    )
    await asyncio.sleep(0)

    scheduler.cancel(42, EVENT_KIND)
    scheduler.cancel(42, EVENT_KIND)
    scheduler.cancel_shift_register(42)
    await scheduler.close()
    await scheduler.close()


@pytest.mark.asyncio
async def test_cancelled_error_exits_without_retry() -> None:
    retry_delays: list[float] = []
    handler_started = asyncio.Event()

    async def sleep_until(_deadline: datetime) -> None:
        return

    async def handler(*_args: object) -> None:
        handler_started.set()
        raise asyncio.CancelledError

    async def retry_sleep(delay: float) -> None:
        retry_delays.append(delay)

    scheduler = ShiftTimelineScheduler(
        handler,
        sleep_until=sleep_until,
        retry_sleep=retry_sleep,
    )
    scheduler.schedule(
        shift_register_id=42,
        event_kind=EVENT_KIND,
        scheduled_at=DEADLINE,
        delivery_nonce=1,
    )
    task = scheduler._tasks[(42, EVENT_KIND)]  # noqa: SLF001

    await asyncio.wait_for(handler_started.wait(), timeout=1)
    with pytest.raises(asyncio.CancelledError):
        await task

    assert retry_delays == []
    await scheduler.close()


@pytest.mark.asyncio
async def test_handler_failures_retry_with_capped_exponential_delays() -> None:
    attempts = 0
    retry_delays: list[float] = []
    handler_done = asyncio.Event()
    calls: list[tuple[int, ShiftTimelineEventKind, datetime, int]] = []

    async def sleep_until(_deadline: datetime) -> None:
        return

    async def handler(
        shift_register_id: int,
        event_kind: ShiftTimelineEventKind,
        scheduled_at: datetime,
        delivery_nonce: int,
    ) -> None:
        nonlocal attempts
        attempts += 1
        calls.append((shift_register_id, event_kind, scheduled_at, delivery_nonce))
        if attempts <= 8:
            raise RuntimeError
        handler_done.set()

    async def retry_sleep(delay: float) -> None:
        retry_delays.append(delay)

    scheduler = ShiftTimelineScheduler(
        handler,
        sleep_until=sleep_until,
        retry_sleep=retry_sleep,
    )
    scheduler.schedule(
        shift_register_id=42,
        event_kind=EVENT_KIND,
        scheduled_at=DEADLINE,
        delivery_nonce=123,
    )

    await asyncio.wait_for(handler_done.wait(), timeout=1)
    await asyncio.sleep(0)

    assert retry_delays == [60, 120, 240, 480, 960, 1920, 3600, 3600]
    assert calls == [(42, EVENT_KIND, DEADLINE, 123)] * 9
    await scheduler.close()


@pytest.mark.asyncio
async def test_old_done_callback_does_not_remove_replacement_task() -> None:
    old_started = asyncio.Event()
    replacement_started = asyncio.Event()
    replacement_release = asyncio.Event()

    async def sleep_until(_deadline: datetime) -> None:
        return

    async def handler(
        _shift_register_id: int,
        _event_kind: ShiftTimelineEventKind,
        _scheduled_at: datetime,
        delivery_nonce: int,
    ) -> None:
        if delivery_nonce == 1:
            old_started.set()
            await asyncio.Future()
        replacement_started.set()
        await replacement_release.wait()

    scheduler = ShiftTimelineScheduler(handler, sleep_until=sleep_until)
    key = (42, EVENT_KIND)
    scheduler.schedule(
        shift_register_id=key[0],
        event_kind=key[1],
        scheduled_at=DEADLINE,
        delivery_nonce=1,
    )
    await asyncio.wait_for(old_started.wait(), timeout=1)

    scheduler.schedule(
        shift_register_id=key[0],
        event_kind=key[1],
        scheduled_at=DEADLINE,
        delivery_nonce=2,
    )
    replacement_task = scheduler._tasks[key]  # noqa: SLF001
    await asyncio.wait_for(replacement_started.wait(), timeout=1)
    await asyncio.sleep(0)
    assert scheduler._tasks.get(key) is replacement_task  # noqa: SLF001

    replacement_release.set()
    await replacement_task
    await asyncio.sleep(0)
    assert key not in scheduler._tasks  # noqa: SLF001
    await scheduler.close()
