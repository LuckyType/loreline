"""Tests for audio input-level metering."""

from __future__ import annotations

import array

from loreline.audio.level import levels


def _pcm(samples: list[int]) -> bytes:
    return array.array("h", samples).tobytes()


def test_silence_is_zero() -> None:
    assert levels(_pcm([0] * 100)) == (0.0, 0.0)


def test_empty_is_zero() -> None:
    assert levels(b"") == (0.0, 0.0)


def test_full_scale_peak() -> None:
    peak, rms = levels(_pcm([32767, -32768, 0, 0]))
    assert 0.99 <= peak <= 1.0
    assert rms > 0.0


def test_peak_at_least_rms() -> None:
    peak, rms = levels(_pcm([10000, -5000, 2000, -8000]))
    assert peak >= rms > 0.0


def test_odd_trailing_byte_ignored() -> None:
    data = _pcm([16384, -16384]) + b"\x01"  # 2 samples + a dangling byte
    peak, _ = levels(data)
    assert 0.49 <= peak <= 0.51
