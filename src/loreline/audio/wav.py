"""PCM <-> WAV helpers (stdlib only).

Backends that take a file upload (OpenAI-compatible ``/v1/audio/transcriptions``,
re-processing jobs) need a self-describing container around raw PCM frames.
"""

from __future__ import annotations

import io
import wave


def pcm_to_wav(pcm: bytes, *, sample_rate: int = 16000, channels: int = 1) -> bytes:
    """Wrap 16-bit little-endian PCM in a WAV container.

    Args:
        pcm: Raw signed 16-bit little-endian PCM samples.
        sample_rate: Sample rate in Hz.
        channels: Channel count (1 = mono).
    """
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(2)  # 16-bit
        wav.setframerate(sample_rate)
        wav.writeframes(pcm)
    return buffer.getvalue()
