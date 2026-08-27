"""Regression tests for capture/STT decoupling.

The capture task must keep draining the mic regardless of STT latency: it pushes
utterances into a bounded queue and never blocks on it (dropping the oldest under
overload), so a slow STT round-trip can't stall capture and overflow the device
buffer (the QueueFull / dropped-audio bug).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from loreline.audio.chunker import Utterance, VadChunker
from loreline.session.manager import (
    _CAPTURE_DONE,  # pyright: ignore[reportPrivateUsage]
    _capture_utterances,  # pyright: ignore[reportPrivateUsage]
    _dequeue,  # pyright: ignore[reportPrivateUsage]
    _offer,  # pyright: ignore[reportPrivateUsage]
)


def test_offer_drops_oldest_when_full() -> None:
    queue: asyncio.Queue[object] = asyncio.Queue(maxsize=2)
    _offer(queue, "a")
    _offer(queue, "b")
    _offer(queue, "c")  # full -> drops "a"
    assert queue.get_nowait() == "b"
    assert queue.get_nowait() == "c"
    assert queue.empty()


class _FakeSource:
    """Yields a fixed number of voiced frames, then ends (stoppable)."""

    def __init__(self, n_frames: int) -> None:
        self._n = n_frames
        self._stop = False

    def stop(self) -> None:
        self._stop = True

    async def frames(self) -> AsyncIterator[tuple[bytes, float]]:
        for i in range(self._n):
            if self._stop:
                break
            await asyncio.sleep(0)
            yield b"\x10\x10" * 320, i * 0.02


async def test_capture_never_blocks_on_full_queue() -> None:
    # Tiny queue with no consumer draining: capture must still run to completion
    # (drop-oldest) instead of blocking on put - i.e. it never stalls the mic.
    source = _FakeSource(n_frames=300)
    chunker = VadChunker(sample_rate=16000, frame_ms=20, silence_ms=20, max_utterance_s=0.1)
    queue: asyncio.Queue[object] = asyncio.Queue(maxsize=4)

    def always_speech(_frame: bytes) -> bool:
        return True

    await asyncio.wait_for(
        _capture_utterances(source, always_speech, chunker, None, queue), timeout=2.0
    )

    drained: list[object] = []
    while not queue.empty():
        drained.append(queue.get_nowait())
    assert drained[-1] is _CAPTURE_DONE  # sentinel is always delivered last
    assert len(drained) <= 4  # bounded - overflow dropped, no unbounded growth


async def test_dequeue_stops_at_sentinel() -> None:
    queue: asyncio.Queue[object] = asyncio.Queue()
    first = Utterance(pcm=b"\x00\x00", start=0.0, end=0.1)
    queue.put_nowait(first)
    queue.put_nowait(_CAPTURE_DONE)
    queue.put_nowait(Utterance(pcm=b"\x01\x01", start=0.2, end=0.3))  # after sentinel: ignored

    received = [item async for item in _dequeue(queue)]
    assert received == [first]
