"""Unit tests for PCM/WAV helpers."""

from __future__ import annotations

import io
import wave

from loreline.audio import pcm_to_wav


def test_pcm_to_wav_roundtrip() -> None:
    pcm = b"\x01\x00" * 1600  # 1600 samples = 100ms @ 16kHz
    data = pcm_to_wav(pcm, sample_rate=16000)

    with wave.open(io.BytesIO(data), "rb") as wav:
        assert wav.getnchannels() == 1
        assert wav.getsampwidth() == 2
        assert wav.getframerate() == 16000
        assert wav.getnframes() == 1600
        assert wav.readframes(1600) == pcm
