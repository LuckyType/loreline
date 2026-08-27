"""Unit tests for the per-session audio store (continuous WAV + utterance index)."""

from __future__ import annotations

import wave
from pathlib import Path

from loreline.audio.chunker import Utterance
from loreline.persistence.audio_store import AudioStore

_SR = 16000


def _pcm(byte: int, n_samples: int) -> bytes:
    return bytes([byte, 0]) * n_samples


def test_continuous_recording_and_utterance_slices(tmp_path: Path) -> None:
    store = AudioStore(tmp_path)
    sid = "abc"
    assert store.exists(sid) is False

    silence_a = _pcm(0, 50)
    utt1 = _pcm(1, 100)
    gap = _pcm(0, 30)
    utt2 = _pcm(2, 80)
    with store.writer(sid, sample_rate=_SR) as writer:
        writer.append_frame(silence_a)
        writer.append_frame(utt1)
        writer.mark_utterance(Utterance(pcm=utt1, start=0.5, end=1.1))
        writer.append_frame(gap)
        writer.append_frame(utt2)
        writer.mark_utterance(Utterance(pcm=utt2, start=1.5, end=2.0))

    assert store.exists(sid) is True

    # The continuous WAV holds everything, including the silence/gaps.
    with wave.open(str(store.wav_path(sid)), "rb") as wav:
        assert wav.getnframes() == 50 + 100 + 30 + 80

    # read_utterances slices the exact voiced spans back out of the continuous stream.
    out = store.read_utterances(sid)
    assert len(out) == 2
    assert out[0].pcm == utt1
    assert out[1].pcm == utt2
    assert out[0].start == 0.5
    assert out[1].end == 2.0


def test_close_is_idempotent(tmp_path: Path) -> None:
    store = AudioStore(tmp_path)
    writer = store.writer("x", sample_rate=_SR)
    writer.append_frame(_pcm(3, 10))
    writer.mark_utterance(Utterance(pcm=_pcm(3, 10), start=0.0, end=0.1))
    writer.close()
    writer.close()  # must not raise
    assert store.exists("x") is True
