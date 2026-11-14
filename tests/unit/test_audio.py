"""Tests for the audio chunker and capture recovery."""

from __future__ import annotations

import sys
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pytest

from loreline.audio import Utterance, VadChunker

if TYPE_CHECKING:
    from types import ModuleType

    from pytest import MonkeyPatch

SPEECH = b"\x01\x01"
SILENCE = b"\x00\x00"


class _PortAudioError(Exception):
    """Stands in for ``sounddevice.PortAudioError``."""


def _fake_sounddevice(monkeypatch: MonkeyPatch, open_stream: Any) -> None:
    """Put a ``sounddevice`` in place whose streams are opened by ``open_stream``.

    These tests used to patch the real module and skip where it could not be
    imported. Importing it initializes PortAudio, which fails outright on a
    host whose PortAudio has a PulseAudio backend and no sound server, and
    nothing below needs a device: only what the capture does when an open
    fails. So the module is a stand-in, as in tests/unit/test_capture_rate.py,
    and the tests run in every lane.
    """

    def query_devices(*_args: object) -> dict[str, float]:
        return {"default_samplerate": 16000.0}

    sd = SimpleNamespace(
        PortAudioError=_PortAudioError,
        RawInputStream=open_stream,
        query_devices=query_devices,
    )
    monkeypatch.setitem(sys.modules, "sounddevice", cast("ModuleType", sd))


def _is_speech(frame: bytes) -> bool:
    return frame[:1] == b"\x01"


def _chunker() -> VadChunker:
    return VadChunker(
        sample_rate=16000,
        frame_ms=20,
        silence_ms=100,  # 5 frames
        max_utterance_s=10.0,
        pre_roll_ms=40,  # 2 frames
    )


def test_chunker_emits_on_silence() -> None:
    chunker = _chunker()
    frames = [SILENCE] * 3 + [SPEECH] * 5 + [SILENCE] * 5
    emitted: list[Utterance] = []
    for i, frame in enumerate(frames):
        result = chunker.feed(frame, ts=i * 0.02, is_speech=_is_speech(frame))
        if result is not None:
            emitted.append(result)

    assert len(emitted) == 1
    utt = emitted[0]
    # 1 leading pre-roll silence + 5 speech + 5 trailing silence frames (the
    # speech-onset frame consumes the other pre-roll slot), 2 bytes each.
    assert len(utt.pcm) == (1 + 5 + 5) * 2
    assert utt.pcm.count(SPEECH) == 5
    assert utt.end > utt.start


def test_chunker_force_flush_on_max_length() -> None:
    chunker = VadChunker(frame_ms=20, silence_ms=10_000, max_utterance_s=0.1, pre_roll_ms=0)
    emitted: list[Utterance] = []
    for i in range(10):  # 10 * 20ms = 200ms > 100ms cap
        result = chunker.feed(SPEECH, ts=i * 0.02, is_speech=True)
        if result is not None:
            emitted.append(result)
    assert len(emitted) >= 1


def test_chunker_manual_flush() -> None:
    chunker = _chunker()
    for i in range(4):
        assert chunker.feed(SPEECH, ts=i * 0.02, is_speech=True) is None
    final = chunker.flush()
    assert final is not None
    assert len(final.pcm) > 0
    assert chunker.flush() is None  # nothing left


async def test_capture_recovers_from_stale_portaudio_state(monkeypatch: MonkeyPatch) -> None:
    """A first ``PortAudioError`` on open triggers one reinit-and-retry (see
    ``loreline.audio.portaudio``), which then succeeds without raising."""
    from loreline.audio.capture import SoundDeviceSource  # noqa: PLC0415

    calls = {"open": 0, "reinit": 0}

    class FakeStream:
        def __enter__(self) -> FakeStream:
            return self

        def __exit__(self, *_args: object) -> bool:
            return False

    def fake_open(**_kwargs: object) -> FakeStream:
        calls["open"] += 1
        if calls["open"] == 1:
            msg = "Error opening RawInputStream: Internal PortAudio error [PaErrorCode -9986]"
            raise _PortAudioError(msg)
        return FakeStream()

    def fake_reinitialize() -> None:
        calls["reinit"] += 1

    _fake_sounddevice(monkeypatch, fake_open)
    monkeypatch.setattr("loreline.audio.portaudio.reinitialize", fake_reinitialize)

    source = SoundDeviceSource(device=0, sample_rate=16000)
    source.stop()  # already-stopped: the loop body never runs, only open + teardown
    frames = [frame async for frame in source.frames()]

    assert frames == []
    assert calls == {"open": 2, "reinit": 1}


async def test_capture_propagates_persistent_portaudio_failure(monkeypatch: MonkeyPatch) -> None:
    """A second failure (post-reinit) is a real problem, not staleness - it must
    still propagate instead of being silently swallowed."""
    from loreline.audio.capture import SoundDeviceSource  # noqa: PLC0415

    def always_fails(**_kwargs: object) -> None:
        msg = "device unavailable"
        raise _PortAudioError(msg)

    _fake_sounddevice(monkeypatch, always_fails)
    monkeypatch.setattr("loreline.audio.portaudio.reinitialize", lambda: None)

    source = SoundDeviceSource(device=0, sample_rate=16000)
    with pytest.raises(_PortAudioError):
        async for _ in source.frames():
            pass


def test_rolling_pcm_slices_a_turn_out_of_the_window() -> None:
    """A streaming turn arrives as two offsets, so the audio has to still be held."""
    from loreline.audio.rolling import RollingPcm  # noqa: PLC0415

    buffer = RollingPcm(16000, seconds=1.0)
    # Ten 100 ms frames, each filled with its own byte, from t = 5.0.
    for i in range(10):
        buffer.append(bytes([i, 0]) * 1600, 5.0 + i * 0.1)

    clip, clip_start = buffer.slice(5.2, 5.4)
    assert len(clip) == 1600 * 2 * 2  # 200 ms of s16le
    assert set(clip[::2]) == {2, 3}  # exactly the third and fourth frames
    assert round(clip_start, 6) == 5.2  # all of it was still held


def test_rolling_pcm_returns_what_is_left_of_a_span_that_aged_out() -> None:
    """A short clip diarizes worse than a long one; no clip diarizes not at all."""
    from loreline.audio.rolling import RollingPcm  # noqa: PLC0415

    buffer = RollingPcm(16000, seconds=0.2)  # holds two of the frames below
    for i in range(5):
        buffer.append(bytes([i, 0]) * 1600, 1.0 + i * 0.1)

    clip, clip_start = buffer.slice(1.0, 1.5)
    assert clip[::2] == bytes([3]) * 1600 + bytes([4]) * 1600
    # What came back starts at 1.3, not at the 1.0 that was asked for: a caller
    # shifting the diarizer's 0-based segments by 1.0 would place every label
    # 300 ms early.
    assert round(clip_start, 6) == 1.3
    assert buffer.slice(9.0, 9.5) == (b"", 0.0)  # entirely outside the window
    assert buffer.slice(1.4, 1.4) == (b"", 0.0)  # an empty span is not an error


def test_a_portaudio_that_cannot_start_lists_no_devices(monkeypatch: MonkeyPatch) -> None:
    """Importing sounddevice initializes PortAudio, and that can fail.

    A PortAudio built with the PulseAudio host API raises ``PortAudioError``
    from the import itself where no sound server is running. That is "no audio
    backend" as much as a missing library is, and the picker's route answered
    it with a 500 instead of an empty list.
    """
    import builtins  # noqa: PLC0415

    from loreline.audio.devices import list_input_devices  # noqa: PLC0415

    real_import = builtins.__import__

    def failing_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "sounddevice":
            msg = "Error initializing PortAudio: PulseAudio_Initialize: Can't connect to server"
            raise _PortAudioError(msg)
        return real_import(name, *args, **kwargs)

    monkeypatch.delitem(sys.modules, "sounddevice", raising=False)
    monkeypatch.setattr(builtins, "__import__", failing_import)

    assert list_input_devices() == []
