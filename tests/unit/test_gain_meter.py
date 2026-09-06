"""The dashboard's live gain meter: peak/RMS from the frames already capturing.

A GM should be able to see the mic is alive before and during a session, not
only after a stall has already been flagged - and without a second device
stream, which is exactly the kind of platform-dependent behaviour the
mic-resampling fix had to work around (some devices refuse a second
simultaneous reader). ``_LevelWatch`` tracks a throttled peak/RMS reading from
the frames already flowing through ``_capture_utterances``, reusing
``loreline.audio.level.levels`` - the same helper ``/ws/audio/level`` uses for
the pre-session mic-test meter - rather than reimplementing the math.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from loreline.audio.chunker import VadChunker
from loreline.audio.level import levels
from loreline.session.manager import (
    _capture_utterances,  # pyright: ignore[reportPrivateUsage]
    _CaptureStats,  # pyright: ignore[reportPrivateUsage]
    _LevelWatch,  # pyright: ignore[reportPrivateUsage]
)

_QUIET_FRAME = b"\x01\x00" * 320  # 20 ms of 16 kHz mono s16le, near silence
_LOUD_FRAME = b"\x00\x40" * 320  # same shape, much louder (peak 0.5)


class _FakeSource:
    """Yields an explicit sequence of frames, then ends.

    Unlike the sibling capture-loop fakes (a fixed count of identical
    frames), this one needs frames of different loudness in a known order.
    """

    def __init__(self, *frames: bytes) -> None:
        self._frames = frames

    def stop(self) -> None:
        return None

    async def frames(self) -> AsyncIterator[tuple[bytes, float]]:
        for i, frame in enumerate(self._frames):
            await asyncio.sleep(0)
            yield frame, i * 0.02


async def test_level_watch_reuses_the_shared_peak_rms_helper() -> None:
    """The published reading is exactly what ``levels()`` says about the frame.

    Locks down "reuse, don't reimplement": with nothing else in play (one
    frame, no throttling in the way), the watch must publish the same numbers
    the pre-session mic-test meter would compute for that frame.
    """
    published: list[tuple[float, float]] = []

    async def publish(reading: tuple[float, float]) -> None:
        published.append(reading)

    watch = _LevelWatch(publish=publish, interval_s=0.0)
    await watch.record(_LOUD_FRAME)

    assert published == [levels(_LOUD_FRAME)]


async def test_level_watch_throttles_to_one_publish_per_interval() -> None:
    """record() runs from the frame loop, so publishing has to throttle itself."""
    published: list[tuple[float, float]] = []

    async def publish(reading: tuple[float, float]) -> None:
        published.append(reading)

    watch = _LevelWatch(publish=publish, interval_s=60.0)
    for _ in range(50):  # one second of frames, all inside the same interval
        await watch.record(_QUIET_FRAME)

    assert len(published) == 1  # the first frame only; the rest held for later


async def test_level_watch_holds_the_loudest_frame_since_the_last_push() -> None:
    """A peak between two throttled pushes must not be lost to a quieter one after it.

    Mirrors the pre-session meter's own ``hold = max(hold, peak)``: the value
    published is the loudest frame seen since the last push, not merely
    whichever frame happened to trigger it.
    """
    published: list[tuple[float, float]] = []

    async def publish(reading: tuple[float, float]) -> None:
        published.append(reading)

    watch = _LevelWatch(publish=publish, interval_s=60.0)
    await watch.record(_QUIET_FRAME)  # first frame: always publishes, resets the hold
    assert len(published) == 1

    await watch.record(_LOUD_FRAME)  # held: the interval has not elapsed
    assert len(published) == 1

    # Force the interval to have elapsed without a real sleep - the same trick
    # test_disk_pressure.py uses (tuning interval_s) taken one step further,
    # since here the elapsing itself is what's under test.
    watch._next_push = 0.0  # pyright: ignore[reportPrivateUsage]
    await watch.record(_QUIET_FRAME)

    assert len(published) == 2
    assert published[1][0] == levels(_LOUD_FRAME)[0]  # the held peak, not this frame's


async def test_capture_feeds_every_frame_to_the_level_watch() -> None:
    """The full capture loop, not just direct calls, reaches the level watch."""
    published: list[tuple[float, float]] = []

    async def publish(reading: tuple[float, float]) -> None:
        published.append(reading)

    watch = _LevelWatch(publish=publish, interval_s=0.0)  # every frame publishes
    chunker = VadChunker(sample_rate=16000, frame_ms=20, silence_ms=20, max_utterance_s=0.1)
    queue: asyncio.Queue[object] = asyncio.Queue(maxsize=64)
    frames = [_QUIET_FRAME, _LOUD_FRAME, _QUIET_FRAME]

    await _capture_utterances(
        _FakeSource(*frames),
        lambda _f: True,
        chunker,
        None,
        _CaptureStats(sample_rate=16000),
        queue,
        level_watch=watch,
    )

    assert published == [levels(frame) for frame in frames]
