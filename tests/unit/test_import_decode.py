"""Tests for decoding an uploaded recording into capture audio."""

from __future__ import annotations

import asyncio
import shutil
import struct
import subprocess
import wave
from pathlib import Path

import pytest

from loreline.audio.decode import (
    CAPTURE_SAMPLE_RATE,
    DecodeError,
    FfmpegMissingError,
    RecordingTooLongError,
    decode_recording,
    ffmpeg_timeout_s,
    pcm16_wav_shape,
)

_HAS_FFMPEG = shutil.which("ffmpeg") is not None
_EIGHT_HOURS = 8 * 3600.0


def _no_binary(_name: str) -> str | None:
    """Stand in for ``shutil.which`` on a box with no ffmpeg."""
    return None


def _tone(frames: int, *, channels: int = 1) -> bytes:
    """Interleaved s16le samples that are not silence (so a VAD can hear them)."""
    return b"".join(
        struct.pack("<" + "h" * channels, *([(i % 4000) - 2000] * channels)) for i in range(frames)
    )


def write_wav(
    path: Path, *, seconds: float, rate: int = CAPTURE_SAMPLE_RATE, channels: int = 1
) -> Path:
    """A PCM16 WAV of the given length, rate and channel count."""
    frames = int(seconds * rate)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(_tone(frames, channels=channels))
    return path


def _shape(path: Path) -> tuple[int, int, int]:
    with wave.open(str(path), "rb") as wav:
        return wav.getnchannels(), wav.getframerate(), wav.getnframes()


async def test_a_capture_shaped_wav_is_rewritten_without_ffmpeg(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole point of the stdlib path: a mono 16 kHz WAV needs no ffmpeg.

    ffmpeg is hidden for the length of the test so a box that happens to have
    it cannot be what makes this pass.
    """
    monkeypatch.setattr("shutil.which", _no_binary)
    src = write_wav(tmp_path / "in.wav", seconds=1.5)
    dest = tmp_path / "out.wav"

    decoded = await decode_recording(
        src, dest, size_bytes=src.stat().st_size, max_seconds=_EIGHT_HOURS
    )

    assert decoded.decoder == "wav"
    assert abs(decoded.duration_s - 1.5) < 0.01
    assert _shape(dest) == (1, CAPTURE_SAMPLE_RATE, int(1.5 * CAPTURE_SAMPLE_RATE))


async def test_a_wav_at_another_rate_is_resampled_to_the_capture_rate(tmp_path: Path) -> None:
    """A phone's 44.1 kHz WAV lands at 16 kHz, the rate everything else reads."""
    pytest.importorskip("soxr")  # the streaming resampler, from the audio extra
    src = write_wav(tmp_path / "in.wav", seconds=1.0, rate=44100)
    dest = tmp_path / "out.wav"

    decoded = await decode_recording(
        src, dest, size_bytes=src.stat().st_size, max_seconds=_EIGHT_HOURS
    )

    assert decoded.decoder == "wav"
    channels, rate, _frames = _shape(dest)
    assert (channels, rate) == (1, CAPTURE_SAMPLE_RATE)
    # soxr holds a block's tail back, so the length is close rather than exact.
    assert abs(decoded.duration_s - 1.0) < 0.05


async def test_a_stereo_wav_is_mixed_down_to_mono(tmp_path: Path) -> None:
    """Two channels are two microphones as often as one signal twice."""
    pytest.importorskip("numpy")  # the downmix, from the audio extra
    src = write_wav(tmp_path / "in.wav", seconds=0.5, channels=2)
    dest = tmp_path / "out.wav"

    decoded = await decode_recording(
        src, dest, size_bytes=src.stat().st_size, max_seconds=_EIGHT_HOURS
    )

    assert decoded.decoder == "wav"
    assert _shape(dest) == (1, CAPTURE_SAMPLE_RATE, int(0.5 * CAPTURE_SAMPLE_RATE))


async def test_a_recording_past_the_hour_limit_is_refused_before_it_is_decoded(
    tmp_path: Path,
) -> None:
    """The header says how long it runs, so nothing is written to find out."""
    src = write_wav(tmp_path / "in.wav", seconds=2.0)
    dest = tmp_path / "out.wav"

    with pytest.raises(RecordingTooLongError) as exc:
        await decode_recording(src, dest, size_bytes=src.stat().st_size, max_seconds=1.0)

    assert "limit" in str(exc.value)
    assert not dest.exists()


async def test_a_wav_with_no_audio_in_it_is_refused(tmp_path: Path) -> None:
    """A bare header would otherwise become a session that can never hold a
    transcript, which is a worse answer than a refusal."""
    src = write_wav(tmp_path / "in.wav", seconds=0.0)
    dest = tmp_path / "out.wav"

    with pytest.raises(DecodeError):
        await decode_recording(src, dest, size_bytes=src.stat().st_size, max_seconds=_EIGHT_HOURS)

    assert not dest.exists()


async def test_a_format_needing_ffmpeg_says_so_when_ffmpeg_is_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Naming ffmpeg and naming what works without it: the failure is about
    the box, and the same upload succeeds unchanged once it is installed."""
    monkeypatch.setattr("shutil.which", _no_binary)
    src = tmp_path / "session.m4a"
    src.write_bytes(b"not audio at all, and certainly not a PCM WAV")
    dest = tmp_path / "out.wav"

    with pytest.raises(FfmpegMissingError) as exc:
        await decode_recording(src, dest, size_bytes=src.stat().st_size, max_seconds=_EIGHT_HOURS)

    assert "ffmpeg" in str(exc.value)
    assert "WAV" in str(exc.value)
    assert not dest.exists()


@pytest.mark.skipif(not _HAS_FFMPEG, reason="ffmpeg is not installed")
async def test_a_compressed_recording_goes_through_ffmpeg(tmp_path: Path) -> None:
    """The path an actual phone recording takes: encoded in, capture audio out."""
    wav = write_wav(tmp_path / "in.wav", seconds=1.0, rate=44100, channels=2)
    src = tmp_path / "session.ogg"
    await asyncio.to_thread(
        subprocess.run,
        ["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-i", str(wav), str(src)],
        check=True,
    )
    dest = tmp_path / "out.wav"

    decoded = await decode_recording(
        src, dest, size_bytes=src.stat().st_size, max_seconds=_EIGHT_HOURS
    )

    assert decoded.decoder == "ffmpeg"
    assert abs(decoded.duration_s - 1.0) < 0.2
    channels, rate, _frames = _shape(dest)
    assert (channels, rate) == (1, CAPTURE_SAMPLE_RATE)


@pytest.mark.skipif(not _HAS_FFMPEG, reason="ffmpeg is not installed")
async def test_a_file_that_is_not_audio_leaves_nothing_behind(tmp_path: Path) -> None:
    """A failed decode leaves no half-written WAV for the next caller."""
    src = tmp_path / "notes.txt"
    src.write_bytes(b"the party went to the vault" * 100)
    dest = tmp_path / "out.wav"

    with pytest.raises(DecodeError):
        await decode_recording(src, dest, size_bytes=src.stat().st_size, max_seconds=_EIGHT_HOURS)

    assert not dest.exists()


def test_pcm16_wav_shape_answers_none_for_anything_it_cannot_rewrite(tmp_path: Path) -> None:
    """Which is how the decoder is chosen: None means "ffmpeg's problem"."""
    good = write_wav(tmp_path / "good.wav", seconds=0.25, rate=8000, channels=2)
    assert pcm16_wav_shape(good) == (2, 8000, 2000)

    junk = tmp_path / "junk.bin"
    junk.write_bytes(b"RIFFnope")
    assert pcm16_wav_shape(junk) is None

    eight_bit = tmp_path / "eight.wav"
    with wave.open(str(eight_bit), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(1)  # not 16-bit: ffmpeg's to widen
        wav.setframerate(CAPTURE_SAMPLE_RATE)
        wav.writeframes(b"\x80" * 1000)
    assert pcm16_wav_shape(eight_bit) is None


def test_the_ffmpeg_timeout_scales_with_the_input_but_has_a_floor() -> None:
    """A one-second clip still gets a minute; a gigabyte gets far more."""
    assert ffmpeg_timeout_s(0) == 60.0
    assert ffmpeg_timeout_s(1024 * 1024 * 1024) > 1800.0
