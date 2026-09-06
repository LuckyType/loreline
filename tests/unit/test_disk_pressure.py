"""What capture does about the disk it is recording onto.

Two behaviours, both found by running a real deployment low on space. Free
space is graded for the health badge but was never looked at *during* a
session, so a capture could fill the disk with nothing but a badge on an
unwatched page to warn anyone. And when it did fill, the ``ENOSPC`` came out of
the writer as an ordinary crash, which is the one description that makes a GM
doubt audio that is in fact complete.
"""

from __future__ import annotations

import asyncio
import errno
from collections.abc import AsyncIterator
from typing import cast

import pytest

from loreline.audio.chunker import Utterance, VadChunker
from loreline.persistence import SessionAudioWriter
from loreline.session.manager import (
    _CAPTURE_DONE,  # pyright: ignore[reportPrivateUsage]
    DiskFullError,
    _capture_utterances,  # pyright: ignore[reportPrivateUsage]
    _CaptureStats,  # pyright: ignore[reportPrivateUsage]
    _disk_full_message,  # pyright: ignore[reportPrivateUsage]
    _DiskWatch,  # pyright: ignore[reportPrivateUsage]
    _low_disk_message,  # pyright: ignore[reportPrivateUsage]
)

_FRAME = b"\x10\x10" * 320  # 20 ms of 16 kHz mono s16le
_FRAME_S = 0.02


class _FakeSource:
    """Yields a fixed number of voiced frames, then ends."""

    def __init__(self, n_frames: int) -> None:
        self._n = n_frames

    def stop(self) -> None:
        return None

    async def frames(self) -> AsyncIterator[tuple[bytes, float]]:
        for i in range(self._n):
            await asyncio.sleep(0)
            yield _FRAME, i * _FRAME_S


class _Readings:
    """Free-space readings played back in order; the last one repeats."""

    def __init__(self, *values: int) -> None:
        self._values = list(values)

    def __call__(self) -> int:
        return self._values.pop(0) if len(self._values) > 1 else self._values[0]


class _FailingWriter:
    """An audio writer on a filesystem that stops accepting writes.

    Stands in for ``SessionAudioWriter``: the real one raises whatever the OS
    hands it, and a full disk hands it ``ENOSPC`` - from the frame write, or
    from the index sidecar that every marked utterance rewrites.
    """

    def __init__(
        self,
        *,
        frames_before_failure: int = 0,
        frame_errno: int | None = errno.ENOSPC,
        mark_errno: int | None = None,
    ) -> None:
        self.frames = 0
        self._after = frames_before_failure
        self._frame_errno = frame_errno
        self._mark_errno = mark_errno

    def append_frame(self, frame: bytes) -> None:
        self.frames += 1
        if self._frame_errno is not None and self.frames > self._after:
            raise OSError(self._frame_errno, "the disk refused the write")

    def mark_utterance(self, utterance: Utterance) -> None:
        if self._mark_errno is not None:
            raise OSError(self._mark_errno, "No space left on device")

    def close(self) -> None:
        return None


def _as_writer(writer: _FailingWriter) -> SessionAudioWriter:
    """The capture path only ever calls the three methods above."""
    return cast("SessionAudioWriter", writer)


async def _capture(
    writer: _FailingWriter, *, n_frames: int = 50, max_utterance_s: float = 15.0
) -> None:
    """Run the capture loop against ``writer`` until it ends or raises."""
    chunker = VadChunker(
        sample_rate=16000, frame_ms=20, silence_ms=20, max_utterance_s=max_utterance_s
    )
    queue: asyncio.Queue[object] = asyncio.Queue(maxsize=16)
    await asyncio.wait_for(
        _capture_utterances(
            _FakeSource(n_frames),
            lambda _f: True,
            chunker,
            _as_writer(writer),
            _CaptureStats(sample_rate=16000),
            queue,
        ),
        timeout=2.0,
    )


async def test_low_disk_alerts_once_per_crossing() -> None:
    """A session sitting near the floor warns once, not on every check."""
    alerted: list[int] = []

    async def on_low(free: int) -> None:
        alerted.append(free)

    watch = _DiskWatch(
        # Comfortable, under the floor, further under, recovered, under again.
        free_bytes=_Readings(900, 400, 300, 2000, 100),
        threshold_bytes=500,
        on_low=on_low,
        interval_s=0.0,
    )
    for _ in range(5):
        await watch.check()

    # Not the 300: still low, already said so. The 100 is a fresh crossing,
    # because free space climbed clear of the floor in between.
    assert alerted == [400, 100]


async def test_the_disk_is_read_once_per_interval_not_once_per_frame() -> None:
    """check() is called from the frame loop, so it has to throttle itself."""
    reads = 0

    def free_bytes() -> int:
        nonlocal reads
        reads += 1
        return 10_000

    async def never(free: int) -> None:
        raise AssertionError(f"alerted at {free} bytes with room to spare")

    watch = _DiskWatch(free_bytes=free_bytes, threshold_bytes=500, on_low=never, interval_s=60.0)
    for _ in range(50):  # one second of frames
        await watch.check()

    assert reads == 1


async def test_a_full_disk_stops_capture_as_a_disk_full_stop() -> None:
    """ENOSPC on a frame write is named, not left as an anonymous OSError."""
    writer = _FailingWriter(frames_before_failure=5)

    with pytest.raises(DiskFullError) as caught:
        await _capture(writer)

    assert writer.frames == 6  # five stored, the sixth found no room
    # Counted the way the capture counts it: samples in, over the sample rate.
    assert caught.value.saved_seconds == 6 * 320 / 16000


async def test_a_full_disk_still_releases_the_router() -> None:
    """The sentinel matters most here: the session ends on the router seeing it."""
    queue: asyncio.Queue[object] = asyncio.Queue(maxsize=16)
    chunker = VadChunker(sample_rate=16000, frame_ms=20)

    with pytest.raises(DiskFullError):
        await asyncio.wait_for(
            _capture_utterances(
                _FakeSource(50),
                lambda _f: True,
                chunker,
                _as_writer(_FailingWriter(frames_before_failure=2)),
                _CaptureStats(sample_rate=16000),
                queue,
            ),
            timeout=2.0,
        )

    drained: list[object] = []
    while not queue.empty():
        drained.append(queue.get_nowait())
    assert drained[-1] is _CAPTURE_DONE


async def test_a_full_disk_on_the_index_write_is_the_same_clean_stop() -> None:
    """The sidecar rewrite runs out of space too, and means the same thing."""
    writer = _FailingWriter(frame_errno=None, mark_errno=errno.ENOSPC)

    with pytest.raises(DiskFullError) as caught:
        await _capture(writer, max_utterance_s=0.1)  # utterances complete mid-loop

    assert caught.value.saved_seconds > 0


async def test_a_write_error_that_is_not_a_full_disk_stays_a_crash() -> None:
    """Only ENOSPC reads as a clean stop; a failing disk is still broken."""
    writer = _FailingWriter(frames_before_failure=1, frame_errno=errno.EIO)

    with pytest.raises(OSError) as caught:
        await _capture(writer)

    assert not isinstance(caught.value, DiskFullError)
    assert caught.value.errno == errno.EIO


def test_the_low_disk_message_says_how_long_is_left() -> None:
    # 16 kHz mono s16le is 32000 bytes a second, so 480 MB is about 4.4 hours.
    message = _low_disk_message(480 * 1024 * 1024, 500 * 1024 * 1024, 32000)

    assert "480 MB" in message
    assert "500 MB" in message
    assert "4.4 hours" in message


def test_the_disk_full_message_says_what_survived() -> None:
    message = _disk_full_message(2 * 3600)

    assert "2.0 hours" in message
    assert "saved and complete" in message
    # No hint of corruption: the recording is the one thing that is fine.
    assert "error" not in message.lower()
