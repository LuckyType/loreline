"""Async pub/sub event bus for fan-out to multiple subscribers.

Used both for raw audio (``AudioBus``) and transcript events (``EventBus``).
Each subscriber gets its own bounded queue; slow subscribers drop oldest items
rather than blocking producers.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from typing import cast

_CLOSE = object()


class EventBus[T]:
    """Fan-out bus: publish once, deliver to all active subscribers."""

    def __init__(self, *, maxsize: int = 256) -> None:
        self._maxsize = maxsize
        self._subscribers: set[asyncio.Queue[T]] = set()
        self._lock = asyncio.Lock()

    async def publish(self, item: T) -> None:
        """Deliver ``item`` to every subscriber (dropping oldest if full)."""
        async with self._lock:
            subscribers = list(self._subscribers)
        for queue in subscribers:
            if queue.full():
                _drop_oldest(queue)
            queue.put_nowait(item)

    def publish_nowait(self, item: T) -> None:
        """Synchronously deliver ``item`` (best-effort, single-loop use only).

        Used by sync producers (e.g. a logging processor). Reads the subscriber
        set without the async lock, which is safe within a single event loop.
        """
        for queue in list(self._subscribers):
            if queue.full():
                _drop_oldest(queue)
            queue.put_nowait(item)

    async def aclose(self) -> None:
        """Signal current subscribers to stop iterating once drained.

        A sentinel is enqueued after any pending items, so subscribers process
        everything already published before their ``async for`` loops exit.
        """
        async with self._lock:
            subscribers = list(self._subscribers)
        for queue in subscribers:
            if queue.full():
                _drop_oldest(queue)
            queue.put_nowait(cast("T", _CLOSE))

    @asynccontextmanager
    async def subscribe(self, *, reliable: bool = False) -> AsyncGenerator[AsyncIterator[T], None]:
        """Context manager yielding an async iterator of published items.

        With ``reliable=True`` the subscriber queue is unbounded so it never
        drops items under backpressure - for durable consumers like persistence.
        Default subscribers use a bounded drop-oldest queue (best-effort UI feeds).
        """
        queue: asyncio.Queue[T] = asyncio.Queue(maxsize=0 if reliable else self._maxsize)
        async with self._lock:
            self._subscribers.add(queue)
        try:
            yield _drain(queue)
        finally:
            async with self._lock:
                self._subscribers.discard(queue)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)


def _drop_oldest[T](queue: asyncio.Queue[T]) -> None:
    with contextlib.suppress(asyncio.QueueEmpty):
        queue.get_nowait()


async def _drain[T](queue: asyncio.Queue[T]) -> AsyncIterator[T]:
    while True:
        item = await queue.get()
        if item is _CLOSE:
            return
        yield item
