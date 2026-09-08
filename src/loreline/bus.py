"""Async pub/sub event bus for fan-out to multiple subscribers.

Used both for raw audio (``AudioBus``) and transcript events (``EventBus``).
Each subscriber gets its own bounded queue; slow subscribers drop oldest items
rather than blocking producers.

A subscriber can hand in the filter it wants applied, and the bus runs it where
the item is published rather than where it is read - see :meth:`EventBus.subscribe`
for why that difference is the whole point.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import cast

_CLOSE = object()


def _everything(_item: object) -> bool:
    """The filter a subscriber that asked for no filter gets."""
    return True


class EventBus[T]:
    """Fan-out bus: publish once, deliver to all active subscribers."""

    def __init__(self, *, maxsize: int = 256) -> None:
        self._maxsize = maxsize
        # Each subscriber's queue against the filter it subscribed with.
        self._subscribers: dict[asyncio.Queue[T], Callable[[T], bool]] = {}
        self._lock = asyncio.Lock()

    async def publish(self, item: T) -> None:
        """Deliver ``item`` to every subscriber that wants it (dropping oldest if full)."""
        async with self._lock:
            subscribers = list(self._subscribers.items())
        for queue, wanted in subscribers:
            if not wanted(item):
                continue
            if queue.full():
                _drop_oldest(queue)
            queue.put_nowait(item)

    def publish_nowait(self, item: T) -> None:
        """Synchronously deliver ``item`` (best-effort, single-loop use only).

        Used by sync producers (e.g. a logging processor). Reads the subscriber
        set without the async lock, which is safe within a single event loop.
        """
        for queue, wanted in list(self._subscribers.items()):
            if not wanted(item):
                continue
            if queue.full():
                _drop_oldest(queue)
            queue.put_nowait(item)

    async def aclose(self) -> None:
        """Signal current subscribers to stop iterating once drained.

        A sentinel is enqueued after any pending items, so subscribers process
        everything already published before their ``async for`` loops exit. It
        goes to every subscriber, filter or no filter: "the stream is over" is
        not one of the items a subscriber gets to be uninterested in.
        """
        async with self._lock:
            subscribers = list(self._subscribers)
        for queue in subscribers:
            if queue.full():
                _drop_oldest(queue)
            queue.put_nowait(cast("T", _CLOSE))

    @asynccontextmanager
    async def subscribe(
        self, *, reliable: bool = False, wanted: Callable[[T], bool] | None = None
    ) -> AsyncGenerator[AsyncIterator[T], None]:
        """Context manager yielding an async iterator of published items.

        With ``reliable=True`` the subscriber queue is unbounded so it never
        drops items under backpressure - for durable consumers like persistence.
        Default subscribers use a bounded drop-oldest queue (best-effort UI feeds).

        ``wanted`` decides what reaches this subscriber, and is called in the
        publisher's own call stack, before the item is queued. That timing is
        the reason it lives here instead of in the consumer's loop: a subscriber
        that filters as it reads judges an item produced some time ago against
        whatever happens to be true when its task is next scheduled, and the two
        disagree exactly when it matters. The live panes are the case in point.
        The last line of a session's teardown is written while that session is
        still the one on show and read a moment after it has stopped being one,
        so a read-time filter dropped precisely the lines a GM had been told to
        wait for (see ``loreline.web.routes.logs_ws``). Widening the window
        cannot fix that; deciding at the right moment can. As a bonus a bounded
        queue then holds only what its subscriber will actually send, rather
        than letting items it would discard push out items it wants.

        Must be cheap and must not raise: it runs once per subscriber per
        published item, inside whatever coroutine is doing the publishing.
        """
        queue: asyncio.Queue[T] = asyncio.Queue(maxsize=0 if reliable else self._maxsize)
        async with self._lock:
            self._subscribers[queue] = wanted if wanted is not None else _everything
        try:
            yield _drain(queue)
        finally:
            async with self._lock:
                self._subscribers.pop(queue, None)

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
