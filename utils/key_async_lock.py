import asyncio
from collections.abc import AsyncIterator, Hashable
from contextlib import asynccontextmanager
from weakref import WeakValueDictionary


class KeyAsyncLock:
    def __init__(self) -> None:
        self._locks: WeakValueDictionary[Hashable, asyncio.Lock] = WeakValueDictionary()

    @asynccontextmanager
    async def __call__(self, key: Hashable) -> AsyncIterator[None]:
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        async with lock:
            yield
