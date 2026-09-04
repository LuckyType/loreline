"""The capture source opens the device at the device's rate, not the pipeline's.

Most non-USB microphones serve only their native rate on the raw ALSA path, so
a stream opened at the STT provider's 16 kHz never opens at all - and it used to
fail *after* the session had been declared started, leaving a ticking
"Recording" timer over a WAV stuck at its 44-byte header. The device is opened
at its own rate now, and the frames are resampled on the way out.

``sounddevice`` is faked outright here rather than skipped when the ``audio``
extra is missing (which is what tests/unit/test_audio.py has to do for the
PortAudio recovery path): none of this needs a device, and the frame contract is
worth checking in the lane that runs on every push. Only the test that puts real
audio through the real resampler needs a native library.
"""

from __future__ import annotations

import asyncio
import sys
from typing import TYPE_CHECKING, Any, cast

import pytest

from loreline.audio.capture import SoundDeviceSource

if TYPE_CHECKING:
    from types import ModuleType

    from pytest import MonkeyPatch

_TARGET_RATE = 16000
_FRAME_MS = 20
_FRAME_SAMPLES = int(_TARGET_RATE * _FRAME_MS / 1000)  # 320
_FRAME_BYTES = _FRAME_SAMPLES * 2  # mono s16le


class _PortAudioError(Exception):
    """Stands in for ``sounddevice.PortAudioError``."""


class _FakeStream:
    """What the fake ``RawInputStream`` returns: a stream that plays blocks.

    Feeding starts on ``__enter__`` (as a real device starts on ``start()``)
    and hands the capture callback exactly the blocks a device running at
    ``samplerate`` would deliver.
    """

    def __init__(self, device: _FakeSoundDevice, blocksize: int, callback: Any) -> None:
        self._device = device
        self._blocksize = blocksize
        self._callback = callback
        self._feed: asyncio.Task[None] | None = None
        self.entered = False
        self.closed = False

    def __enter__(self) -> _FakeStream:
        self.entered = True
        if self._device.blocks:
            self._feed = asyncio.create_task(self._play())
        return self

    def __exit__(self, *_args: object) -> bool:
        self.closed = True
        if self._feed is not None:
            self._feed.cancel()
        return False

    async def _play(self) -> None:
        for index in range(self._device.blocks):
            await asyncio.sleep(0)
            self._callback(self._device.block(index, self._blocksize), self._blocksize, None, None)
        self._device.played.set()


class _FakeSoundDevice:
    """A ``sounddevice`` stand-in: one device, at one rate, playing N blocks."""

    PortAudioError = _PortAudioError

    def __init__(
        self,
        *,
        device_rate: float | None = _TARGET_RATE,
        blocks: int = 0,
        fail_opens: int = 0,
    ) -> None:
        self._device_rate = device_rate
        self.blocks = blocks
        self.fail_opens = fail_opens
        self.opened: list[dict[str, Any]] = []
        self.streams: list[_FakeStream] = []
        self.played = asyncio.Event()

    def query_devices(self, device: object = None, kind: object = None) -> dict[str, Any]:
        _ = (device, kind)
        if self._device_rate is None:  # a device PortAudio cannot describe
            raise self.PortAudioError("Error querying device -1")
        return {"default_samplerate": self._device_rate, "max_input_channels": 1}

    def RawInputStream(self, **kwargs: Any) -> _FakeStream:  # noqa: N802 - sounddevice's name
        self.opened.append(kwargs)
        if len(self.opened) <= self.fail_opens:
            raise self.PortAudioError("Invalid sample rate [PaErrorCode -9997]")
        stream = _FakeStream(self, int(kwargs["blocksize"]), kwargs["callback"])
        self.streams.append(stream)
        return stream

    def block(self, index: int, blocksize: int) -> bytes:
        """One block of PCM, distinguishable from its neighbours."""
        return (index % 250 + 1).to_bytes(2, "little") * blocksize


def _as_module(sd: _FakeSoundDevice) -> ModuleType:
    """``import sounddevice`` only ever asks this object for attributes."""
    return cast("ModuleType", sd)


def _install(monkeypatch: MonkeyPatch, sd: _FakeSoundDevice) -> None:
    monkeypatch.setitem(sys.modules, "sounddevice", _as_module(sd))
    monkeypatch.setattr("loreline.audio.portaudio.reinitialize", lambda: None)


async def _capture(sd: _FakeSoundDevice, source: SoundDeviceSource) -> list[bytes]:
    """Collect every frame the source yields for the blocks the device plays."""
    frames: list[bytes] = []

    async def drain() -> None:
        async for frame, _ts in source.frames():
            frames.append(frame)

    task = asyncio.create_task(drain())
    await asyncio.wait_for(sd.played.wait(), timeout=5.0)
    # The blocks are handed over; give the capture loop a moment to put them
    # through the resampler before stopping it, since stop() is checked between
    # blocks and drops whatever is still queued behind it.
    await asyncio.sleep(0.05)
    source.stop()
    await asyncio.wait_for(task, timeout=5.0)
    return frames


async def test_device_rate_is_resampled_to_the_pipeline_rate(monkeypatch: MonkeyPatch) -> None:
    """A 48 kHz device is opened at 48 kHz and still yields 16 kHz frames.

    The count is what matters: one second of audio must arrive as ~50 frames of
    320 samples, not as the 150 the raw device rate would make of it.
    """
    pytest.importorskip("soxr")  # the real resampler; ships with the audio extra
    sd = _FakeSoundDevice(device_rate=48000, blocks=50)  # 50 * 20 ms = 1 s
    _install(monkeypatch, sd)

    source = SoundDeviceSource(device=0, sample_rate=_TARGET_RATE, frame_ms=_FRAME_MS)
    frames = await _capture(sd, source)

    assert sd.opened[0]["samplerate"] == 48000  # the device's rate, not ours
    assert sd.opened[0]["blocksize"] == 960  # 20 ms at the device's rate
    assert {len(frame) for frame in frames} == {_FRAME_BYTES}  # nothing ragged
    captured = sum(len(frame) for frame in frames) // 2
    # One second at the target rate, minus at most soxr's filter delay (a few
    # hundred samples still inside the resampler when the stream stopped).
    assert 15_000 <= captured <= _TARGET_RATE


async def test_matching_device_rate_passes_through_untouched(monkeypatch: MonkeyPatch) -> None:
    """A device already at the pipeline's rate needs no resampler at all.

    Which is also why this test runs without soxr installed: the pass-through
    path must not reach for a native library it does not need.
    """
    sd = _FakeSoundDevice(device_rate=_TARGET_RATE, blocks=5)
    _install(monkeypatch, sd)

    source = SoundDeviceSource(device=0, sample_rate=_TARGET_RATE, frame_ms=_FRAME_MS)
    frames = await _capture(sd, source)

    assert sd.opened[0]["samplerate"] == _TARGET_RATE
    assert b"".join(frames) == b"".join(sd.block(i, _FRAME_SAMPLES) for i in range(5))


async def test_undescribable_device_falls_back_to_the_wanted_rate(
    monkeypatch: MonkeyPatch,
) -> None:
    """A device whose rate cannot be queried is still worth trying to open."""
    sd = _FakeSoundDevice(device_rate=None, blocks=3)
    _install(monkeypatch, sd)

    source = SoundDeviceSource(device="hw:1,0", sample_rate=_TARGET_RATE, frame_ms=_FRAME_MS)
    frames = await _capture(sd, source)

    assert sd.opened[0]["samplerate"] == _TARGET_RATE
    assert len(frames) == 3


async def test_preflight_opens_and_releases_the_device(monkeypatch: MonkeyPatch) -> None:
    """The pre-flight is a real open of the real device, and it lets go again."""
    sd = _FakeSoundDevice(device_rate=_TARGET_RATE)
    _install(monkeypatch, sd)

    await SoundDeviceSource(device=0, sample_rate=_TARGET_RATE).preflight()

    assert len(sd.opened) == 1
    assert sd.streams[0].entered and sd.streams[0].closed


async def test_preflight_retries_once_after_a_stale_portaudio_error(
    monkeypatch: MonkeyPatch,
) -> None:
    """Same one reinit-and-retry the capture itself does (see audio.portaudio)."""
    reinits: list[int] = []
    sd = _FakeSoundDevice(device_rate=_TARGET_RATE, fail_opens=1)
    monkeypatch.setitem(sys.modules, "sounddevice", _as_module(sd))
    monkeypatch.setattr("loreline.audio.portaudio.reinitialize", lambda: reinits.append(1))

    await SoundDeviceSource(device=0, sample_rate=_TARGET_RATE).preflight()

    assert len(sd.opened) == 2
    assert reinits == [1]


async def test_preflight_raises_when_the_device_stays_shut(monkeypatch: MonkeyPatch) -> None:
    """Two failures is a real problem, and start() turns this into a 400."""
    sd = _FakeSoundDevice(device_rate=_TARGET_RATE, fail_opens=2)
    _install(monkeypatch, sd)

    with pytest.raises(_PortAudioError):
        await SoundDeviceSource(device=7, sample_rate=_TARGET_RATE).preflight()
    assert len(sd.opened) == 2  # not retried a third time
