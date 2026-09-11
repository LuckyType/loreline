"""Decode an uploaded recording into the shape a live capture produces.

An import has to become the same artifact a capture leaves behind - one
continuous mono 16 kHz 16-bit PCM WAV - because everything downstream reads
that and only that: the VAD that rebuilds the utterance index, the re-
transcription that replays it, the diarizer that clusters it, the player in
the browser. See ``docs/adr/0008``.

Two decoders, and which one runs is decided by the file rather than by the
caller. A file the ``wave`` module can already read as 16-bit PCM is rewritten
here, block by block, with no native dependency beyond what a resample needs -
so a WAV import works on a box with no ffmpeg at all. Everything else (m4a,
mp3, ogg, opus, webm, flac, a compressed or float WAV) goes through ffmpeg,
which is one subprocess and no format table of our own to keep current.
"""

from __future__ import annotations

import asyncio
import contextlib
import shutil
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from loreline.audio.resample import Pcm16Stream, downmix_pcm16

# The shape SessionAudioWriter produces, and the only shape this module emits.
CAPTURE_SAMPLE_RATE = 16000
_CHANNELS = 1
_SAMPLE_WIDTH = 2  # 16-bit

# Source frames per read. Large enough that a multi-hour file is not read a
# kilobyte at a time, small enough that neither the block nor its resampled
# copy is ever a meaningful fraction of memory (~2 MB of int16 per read).
_BLOCK_FRAMES = 1 << 20

# What a caller may offer, as the accept list the dialog uses and the sentence
# a refusal prints. Not a gate: ffmpeg decides what it can decode, and this is
# only what the UI promises.
IMPORTABLE_SUFFIXES = (".wav", ".m4a", ".mp3", ".ogg", ".opus", ".webm", ".flac", ".aac", ".mp4")

# How long ffmpeg is given, scaled to the input: decoding runs many times
# faster than real time on every format here, so this is an upper bound on a
# hung process rather than a budget anything is expected to approach.
_FFMPEG_SECONDS_PER_MB = 2.0
_FFMPEG_MIN_TIMEOUT_S = 60.0

# How much of ffmpeg's complaint reaches the GM. Its stderr can run to pages;
# the last line is the one that says what was wrong with the file.
_STDERR_TAIL = 400


class DecodeError(ValueError):
    """Raised when an uploaded recording cannot become session audio."""


class FfmpegMissingError(DecodeError):
    """Raised when a format needs ffmpeg and ffmpeg is not installed.

    Its own class because it is the one decode failure that is about the box
    rather than about the file: the same upload succeeds unchanged once ffmpeg
    is there, so the message names it and names what works without it.
    """


class RecordingTooLongError(DecodeError):
    """Raised when a decoded recording runs past the configured ceiling."""


@dataclass(frozen=True, slots=True)
class Decoded:
    """What a decode produced: how long it runs, and what decoded it."""

    duration_s: float
    decoder: Literal["wav", "ffmpeg"]


def pcm16_wav_shape(path: Path) -> tuple[int, int, int] | None:
    """``(channels, sample_rate, frames)`` for a 16-bit PCM WAV, else None.

    None means "not a file the stdlib can rewrite", which covers a compressed
    WAV, a float WAV, a WAVE_FORMAT_EXTENSIBLE header and anything that is not
    a WAV at all. Every one of those is ffmpeg's to handle, so they get the
    same answer here.
    """
    try:
        with wave.open(str(path), "rb") as wav:
            if wav.getsampwidth() != _SAMPLE_WIDTH or wav.getcomptype() != "NONE":
                return None
            return wav.getnchannels(), wav.getframerate(), wav.getnframes()
    except (wave.Error, OSError, EOFError):
        return None


def decode_pcm16_wav(
    src: Path, dest: Path, *, channels: int, sample_rate: int, frames: int, max_seconds: float
) -> Decoded:
    """Rewrite a 16-bit PCM WAV as capture audio. Blocking - run off the loop.

    Streamed block by block rather than read whole: at 1 GB of upload the
    decoded copy is still hundreds of MB, and holding either end in memory is
    the difference between importing a four-hour session and dying on it.
    ``Pcm16Stream`` is what makes the streaming safe, since it keeps its
    filter state across blocks; at 16 kHz it is a pass-through that touches no
    native code at all.
    """
    duration_s = frames / sample_rate if sample_rate else 0.0
    _refuse_a_recording_that_is_too_long(duration_s, max_seconds)
    try:
        stream = Pcm16Stream(sample_rate, CAPTURE_SAMPLE_RATE)
        with wave.open(str(src), "rb") as inp, wave.open(str(dest), "wb") as out:
            out.setnchannels(_CHANNELS)
            out.setsampwidth(_SAMPLE_WIDTH)
            out.setframerate(CAPTURE_SAMPLE_RATE)
            while block := inp.readframes(_BLOCK_FRAMES):
                out.writeframes(stream.process(downmix_pcm16(block, channels)))
    except RuntimeError as exc:
        # A WAV this box cannot reshape on its own: not mono, or not at the
        # capture rate, with the native helper for that missing. ffmpeg does
        # both, so the answer is the same one a compressed file gets.
        msg = f"{exc} Install ffmpeg, or upload a 16 kHz mono 16-bit PCM WAV, which needs neither."
        raise DecodeError(msg) from exc
    return Decoded(duration_s=_stored_duration(dest), decoder="wav")


def ffmpeg_timeout_s(size_bytes: int) -> float:
    """How long ffmpeg gets for an input of this size."""
    return max(_FFMPEG_MIN_TIMEOUT_S, size_bytes / (1024 * 1024) * _FFMPEG_SECONDS_PER_MB)


async def decode_with_ffmpeg(
    src: Path, dest: Path, *, size_bytes: int, max_seconds: float
) -> Decoded:
    """Decode anything ffmpeg can read into capture audio.

    ``-vn`` because a phone recording is often a video file with the evening's
    audio in it, and ``-nostdin`` because a subprocess that inherits stdin can
    block forever waiting on a prompt nobody will answer.
    """
    binary = shutil.which("ffmpeg")
    if binary is None:
        msg = (
            "this file needs ffmpeg to decode and ffmpeg is not installed on the server. "
            "Install it, or upload a 16-bit PCM WAV, which is decoded without it."
        )
        raise FfmpegMissingError(msg)
    timeout_s = ffmpeg_timeout_s(size_bytes)
    proc = await asyncio.create_subprocess_exec(
        binary,
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(src),
        "-vn",
        "-ac",
        str(_CHANNELS),
        "-ar",
        str(CAPTURE_SAMPLE_RATE),
        "-acodec",
        "pcm_s16le",
        "-f",
        "wav",
        str(dest),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except TimeoutError as exc:
        proc.kill()
        with contextlib.suppress(Exception):
            await proc.wait()
        msg = f"decoding gave up after {round(timeout_s)}s - the file may be corrupt"
        raise DecodeError(msg) from exc
    if proc.returncode != 0:
        detail = stderr.decode("utf-8", "replace").strip().replace("\n", " ")[-_STDERR_TAIL:]
        msg = f"ffmpeg could not decode this file: {detail or 'no audio stream found'}"
        raise DecodeError(msg)
    duration_s = _stored_duration(dest)
    _refuse_a_recording_that_is_too_long(duration_s, max_seconds)
    return Decoded(duration_s=duration_s, decoder="ffmpeg")


def discard(path: Path) -> None:
    """Remove a file if it is there (a decode that failed, a temp upload)."""
    path.unlink(missing_ok=True)


async def decode_recording(
    src: Path, dest: Path, *, size_bytes: int, max_seconds: float
) -> Decoded:
    """Decode an uploaded recording into capture audio at ``dest``.

    The file decides the decoder, not the caller and not the file name: an
    ``.m4a`` that is really a WAV takes the stdlib path, and a ``.wav`` that is
    really mu-law takes ffmpeg's. Nothing is left at ``dest`` when this raises.
    """
    shape = await asyncio.to_thread(pcm16_wav_shape, src)
    try:
        if shape is not None:
            channels, sample_rate, frames = shape
            # Blocking file I/O plus a resample over the whole recording.
            return await asyncio.to_thread(
                decode_pcm16_wav,
                src,
                dest,
                channels=channels,
                sample_rate=sample_rate,
                frames=frames,
                max_seconds=max_seconds,
            )
        return await decode_with_ffmpeg(src, dest, size_bytes=size_bytes, max_seconds=max_seconds)
    except BaseException:
        # Includes a cancellation: a client that disconnects mid-decode must
        # not leave a half-written WAV behind for the next caller to trip over.
        discard(dest)
        raise


def _stored_duration(path: Path) -> float:
    """Length of a written WAV in seconds, read from its header."""
    with wave.open(str(path), "rb") as wav:
        rate = wav.getframerate()
        return wav.getnframes() / rate if rate else 0.0


def _refuse_a_recording_that_is_too_long(duration_s: float, max_seconds: float) -> None:
    """Refuse an empty recording, and one past the configured ceiling.

    Empty is refused here rather than allowed through as a zero-length session:
    a file with no decodable audio produces a bare WAV header, an empty
    utterance index and a session that can never hold a transcript, which is a
    worse answer than a refusal naming the file.
    """
    if duration_s <= 0:
        msg = "this file holds no audio at all"
        raise DecodeError(msg)
    if duration_s > max_seconds:
        hours = duration_s / 3600
        msg = (
            f"this recording runs {hours:.1f} hours, past the "
            f"{max_seconds / 3600:.1f} hour import limit"
        )
        raise RecordingTooLongError(msg)
