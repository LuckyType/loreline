"""Tests for the audio chunker and capture recovery.

Requires the ``audio`` extra (sounddevice) for the capture-recovery test;
skipped otherwise (see the base CI job, which runs without it).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from loreline.audio import Utterance, VadChunker

if TYPE_CHECKING:
    from pytest import MonkeyPatch

SPEECH = b"\x01\x01"
SILENCE = b"\x00\x00"


def _require_sounddevice() -> Any:
    """Return the ``sounddevice`` module, or skip if it can't be used here.

    Not ``pytest.importorskip``: that only catches ``ImportError``, but
    sounddevice raises ``OSError("PortAudio library not found")`` at import
    time when the native library is absent - its wheel doesn't bundle
    PortAudio on Linux. So on a host without libportaudio2 (a bare CI runner,
    for one) importorskip lets the OSError through and the test *fails*
    instead of skipping.
    """
    try:
        # sounddevice ships only with the `audio` extra, which the base CI job
        # deliberately installs without, so an unresolved import here is
        # expected rather than a mistake. (Don't start this comment with
        # "pyright:" - that's reserved for directives and errors out.)
        import sounddevice  # noqa: PLC0415 # pyright: ignore[reportMissingImports]
    except (ImportError, OSError) as exc:  # pragma: no cover - env-dependent
        pytest.skip(f"sounddevice unavailable: {exc}")
    return sounddevice


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
    sd = _require_sounddevice()

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
            raise sd.PortAudioError(msg)
        return FakeStream()

    def fake_reinitialize() -> None:
        calls["reinit"] += 1

    monkeypatch.setattr(sd, "RawInputStream", fake_open)
    monkeypatch.setattr("loreline.audio.portaudio.reinitialize", fake_reinitialize)

    source = SoundDeviceSource(device=0, sample_rate=16000)
    source.stop()  # already-stopped: the loop body never runs, only open + teardown
    frames = [frame async for frame in source.frames()]

    assert frames == []
    assert calls == {"open": 2, "reinit": 1}


async def test_capture_propagates_persistent_portaudio_failure(monkeypatch: MonkeyPatch) -> None:
    """A second failure (post-reinit) is a real problem, not staleness - it must
    still propagate instead of being silently swallowed."""
    sd = _require_sounddevice()

    from loreline.audio.capture import SoundDeviceSource  # noqa: PLC0415

    def always_fails(**_kwargs: object) -> None:
        msg = "device unavailable"
        raise sd.PortAudioError(msg)

    monkeypatch.setattr(sd, "RawInputStream", always_fails)
    monkeypatch.setattr("loreline.audio.portaudio.reinitialize", lambda: None)

    source = SoundDeviceSource(device=0, sample_rate=16000)
    with pytest.raises(sd.PortAudioError):
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
