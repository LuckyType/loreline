"""Tests for the async EventBus."""

from __future__ import annotations

import asyncio

from loreline.bus import EventBus


async def test_fanout_to_multiple_subscribers() -> None:
    bus: EventBus[int] = EventBus()
    received_a: list[int] = []
    received_b: list[int] = []

    async def consume(target: list[int]) -> None:
        async with bus.subscribe() as stream:
            async for item in stream:
                target.append(item)
                if len(target) == 3:
                    return

    consumer_a = asyncio.create_task(consume(received_a))
    consumer_b = asyncio.create_task(consume(received_b))

    await asyncio.sleep(0.01)  # let subscribers register
    for i in range(3):
        await bus.publish(i)

    await asyncio.wait_for(asyncio.gather(consumer_a, consumer_b), timeout=1.0)
    assert received_a == [0, 1, 2]
    assert received_b == [0, 1, 2]


async def test_unsubscribe_on_exit() -> None:
    bus: EventBus[int] = EventBus()
    async with bus.subscribe():
        assert bus.subscriber_count == 1
    assert bus.subscriber_count == 0


async def test_default_subscriber_drops_oldest_when_full() -> None:
    bus: EventBus[int] = EventBus(maxsize=2)
    async with bus.subscribe() as stream:
        for i in range(5):
            await bus.publish(i)  # bounded -> only the last two survive
        assert (await anext(stream), await anext(stream)) == (3, 4)


async def test_reliable_subscriber_never_drops() -> None:
    bus: EventBus[int] = EventBus(maxsize=2)
    async with bus.subscribe(reliable=True) as stream:
        for i in range(10):
            await bus.publish(i)  # unbounded -> nothing dropped
        got = [await anext(stream) for _ in range(10)]
    assert got == list(range(10))


async def test_a_subscriber_filter_decides_when_the_item_is_published() -> None:
    """The filter's timing is the reason it belongs to the bus.

    ``live`` stands for anything a subscriber would otherwise re-read as it
    reads the queue: here it flips between the two publishes, and a consumer
    filtering in its own loop would judge both items against the flag as it
    stands when it finally wakes up. Deciding at publish keeps the item that
    was wanted when it was produced, which is the live log pane keeping the
    last lines of a session's teardown.
    """
    bus: EventBus[int] = EventBus()
    live = True

    async with bus.subscribe(wanted=lambda _item: live) as stream:
        await bus.publish(1)
        live = False
        await bus.publish(2)
        # The close sentinel reaches every subscriber, filter or not, so this
        # drains rather than blocking on an item that is never coming.
        await bus.aclose()

        assert [item async for item in stream] == [1]
