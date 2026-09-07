"""Regression tests for capture/STT decoupling.

The capture task must keep draining the mic regardless of STT latency: it pushes
utterances into a bounded queue and never blocks on it (dropping the oldest under
overload), so a slow STT round-trip can't stall capture and overflow the device
buffer (the QueueFull / dropped-audio bug).

It must also always close that queue with the sentinel, however it ends: the
sentinel is the only thing that ends the router's input, so a capture that dies
without one leaves the router waiting on an empty queue forever (the zombie
recording bug).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest

from loreline.audio.chunker import Utterance, VadChunker
from loreline.session.manager import (
    _CAPTURE_DONE,  # pyright: ignore[reportPrivateUsage]
    _capture_utterances,  # pyright: ignore[reportPrivateUsage]
    _CaptureStats,  # pyright: ignore[reportPrivateUsage]
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
        _capture_utterances(source, always_speech, chunker, None, _CaptureStats(), queue),
        timeout=2.0,
    )

    drained: list[object] = []
    while not queue.empty():
        drained.append(queue.get_nowait())
    assert drained[-1] is _CAPTURE_DONE  # sentinel is always delivered last
    assert len(drained) <= 4  # bounded - overflow dropped, no unbounded growth


class _DeadSource:
    """A device that fails on open: raises before yielding a single frame."""

    def stop(self) -> None:
        return None

    async def frames(self) -> AsyncIterator[tuple[bytes, float]]:
        msg = "Error opening RawInputStream: Invalid sample rate"
        raise RuntimeError(msg)
        yield b"", 0.0  # pragma: no cover - unreachable, but makes this a generator


async def test_capture_death_still_closes_the_queue() -> None:
    """A mic that never opens must end the router, not strand it.

    Without the sentinel the router blocks on an empty queue for the life of the
    process: the session keeps reporting "capturing" with nothing being
    recorded, and even Stop only gets out of it by timing out 30 seconds later.
    """
    chunker = VadChunker(sample_rate=16000, frame_ms=20)
    queue: asyncio.Queue[object] = asyncio.Queue(maxsize=4)

    capture = _capture_utterances(
        _DeadSource(), lambda _f: True, chunker, None, _CaptureStats(), queue
    )
    with pytest.raises(RuntimeError):
        await asyncio.wait_for(capture, timeout=2.0)

    assert queue.get_nowait() is _CAPTURE_DONE
    assert queue.empty()


async def test_capture_stats_count_the_audio_that_arrived() -> None:
    """What the dashboard reads to tell a recording from a stalled one."""
    stats = _CaptureStats(sample_rate=16000)
    assert stats.seconds == 0.0

    chunker = VadChunker(sample_rate=16000, frame_ms=20, silence_ms=20, max_utterance_s=0.1)
    queue: asyncio.Queue[object] = asyncio.Queue(maxsize=64)
    await _capture_utterances(
        _FakeSource(n_frames=50), lambda _f: True, chunker, None, stats, queue
    )

    assert stats.seconds == 1.0  # 50 frames of 20 ms, exactly
    assert stats.since_last_frame < 1.0


async def test_dequeue_stops_at_sentinel() -> None:
    queue: asyncio.Queue[object] = asyncio.Queue()
    first = Utterance(pcm=b"\x00\x00", start=0.0, end=0.1)
    queue.put_nowait(first)
    queue.put_nowait(_CAPTURE_DONE)
    queue.put_nowait(Utterance(pcm=b"\x01\x01", start=0.2, end=0.3))  # after sentinel: ignored

    received = [item async for item in _dequeue(queue)]
    assert received == [first]


class _RecordingSink:
    """A ``FrameSink`` that remembers what the capture loop handed it."""

    def __init__(self, *, queues_utterances: bool = False) -> None:
        self.frames: list[tuple[bytes, float, bool]] = []
        self.utterances: list[Utterance] = []
        self.closed = False
        self._queues = queues_utterances

    def frame(self, pcm: bytes, ts: float, *, is_speech: bool) -> None:
        self.frames.append((pcm, ts, is_speech))

    def utterance(self, utterance: Utterance) -> None:
        self.utterances.append(utterance)

    @property
    def queues_utterances(self) -> bool:
        return self._queues

    def done(self) -> None:
        self.closed = True


async def test_streaming_gets_the_frames_and_the_queue_gets_no_utterances() -> None:
    """While a connector decides the turns, an utterance is the index's business.

    The chunker keeps running (the WAV's utterance sidecar is what a re-process
    reads), but queueing what it cuts would either transcribe the session twice
    or fill a queue nobody is draining.
    """
    sink = _RecordingSink()
    chunker = VadChunker(sample_rate=16000, frame_ms=20, silence_ms=20, max_utterance_s=0.1)
    queue: asyncio.Queue[object] = asyncio.Queue(maxsize=64)

    await asyncio.wait_for(
        _capture_utterances(
            _FakeSource(n_frames=30),
            lambda _f: True,
            chunker,
            None,
            _CaptureStats(),
            queue,
            frames=sink,
        ),
        timeout=2.0,
    )

    assert len(sink.frames) == 30  # every frame, with the VAD's verdict on it
    assert all(is_speech for _pcm, _ts, is_speech in sink.frames)
    assert sink.utterances == []  # nothing is asking for them yet
    assert queue.get_nowait() is _CAPTURE_DONE  # and only the sentinel was queued
    assert queue.empty()
    assert sink.closed  # the stream is ended too, or it waits forever


async def test_a_handed_off_session_sends_its_utterances_back_to_the_sink() -> None:
    """After the handoff the router is draining again, so the chunker feeds it."""
    sink = _RecordingSink(queues_utterances=True)
    chunker = VadChunker(sample_rate=16000, frame_ms=20, silence_ms=20, max_utterance_s=0.1)
    queue: asyncio.Queue[object] = asyncio.Queue(maxsize=64)

    await asyncio.wait_for(
        _capture_utterances(
            _FakeSource(n_frames=30),
            lambda _f: True,
            chunker,
            None,
            _CaptureStats(),
            queue,
            frames=sink,
        ),
        timeout=2.0,
    )

    assert sink.utterances  # cut by the chunker, handed to the path that wants them
    assert queue.get_nowait() is _CAPTURE_DONE  # still never the queue directly


async def test_the_stream_is_closed_even_when_the_microphone_never_opens() -> None:
    """The zombie recording in its second shape: nothing ends an open stream."""
    sink = _RecordingSink()
    chunker = VadChunker(sample_rate=16000, frame_ms=20)
    queue: asyncio.Queue[object] = asyncio.Queue(maxsize=4)

    capture = _capture_utterances(
        _DeadSource(), lambda _f: True, chunker, None, _CaptureStats(), queue, frames=sink
    )
    with pytest.raises(RuntimeError):
        await asyncio.wait_for(capture, timeout=2.0)

    assert sink.closed
    assert queue.get_nowait() is _CAPTURE_DONE
