"""Unit tests for AssemblyAI audio re-chunking (50-1000 ms per message)."""

from __future__ import annotations

from loreline.stt.backends.assemblyai import (
    _audio_chunks,  # pyright: ignore[reportPrivateUsage]
)

_SAMPLE_RATE = 16000
_BYTES_PER_MS = _SAMPLE_RATE * 2 // 1000


def _ms(chunk: bytes) -> float:
    return len(chunk) / _BYTES_PER_MS


def test_chunks_stay_within_the_duration_window() -> None:
    pcm = b"\x01\x00" * (_SAMPLE_RATE * 5)  # 5 s
    chunks = _audio_chunks(pcm, _SAMPLE_RATE)
    assert all(50 <= _ms(chunk) <= 1000 for chunk in chunks)
    assert b"".join(chunks) == pcm  # nothing lost or reordered


def test_sub_minimum_tail_is_folded_into_the_last_chunk() -> None:
    pcm = b"\x01\x00" * int(_SAMPLE_RATE * 0.810)  # 810 ms: 800 + a 10 ms tail
    chunks = _audio_chunks(pcm, _SAMPLE_RATE)
    assert len(chunks) == 1
    assert _ms(chunks[0]) == 810


def test_tiny_utterance_is_padded_to_the_minimum() -> None:
    pcm = b"\x01\x00" * int(_SAMPLE_RATE * 0.020)  # 20 ms, below the 50 ms floor
    chunks = _audio_chunks(pcm, _SAMPLE_RATE)
    assert len(chunks) == 1
    assert _ms(chunks[0]) == 50
    assert chunks[0].startswith(pcm)  # padded with trailing silence, audio kept
