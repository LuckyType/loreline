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
