"""Regression tests for the Silero VAD frame-size fix.

Silero requires a fixed 512-sample (@16k) analysis window and raises on anything
else. Capture feeds 320-sample (20 ms) frames, which previously crashed the
router on the first frame so real sessions captured but transcribed nothing. The
adapter now buffers internally; these tests drive the real model with off-window
frame sizes and assert it never raises.

Requires the ``audio`` extra (numpy/torch/silero-vad); skipped otherwise.
"""

from __future__ import annotations

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("torch")
pytest.importorskip("silero_vad")

from loreline.audio.vad import SileroVad  # noqa: E402


def _frame(n_samples: int, amplitude: int = 0) -> bytes:
    return np.full(n_samples, amplitude, dtype=np.int16).tobytes()


def test_silero_accepts_320_sample_frames() -> None:
    vad = SileroVad(sample_rate=16000)
    for _ in range(20):  # 320 != 512; must buffer, not raise
        assert isinstance(vad.is_speech(_frame(320)), bool)


def test_silero_handles_varied_frame_sizes() -> None:
    vad = SileroVad(sample_rate=16000)
    for n in (160, 320, 480, 512, 1024):
        assert isinstance(vad.is_speech(_frame(n)), bool)


def test_silero_empty_frame_is_safe() -> None:
    vad = SileroVad(sample_rate=16000)
    assert vad.is_speech(b"") is False
