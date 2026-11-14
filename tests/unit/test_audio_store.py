"""Unit tests for the per-session audio store (continuous WAV + utterance index)."""

from __future__ import annotations

import wave
from pathlib import Path

from loreline.audio.chunker import SpeechDetector, Utterance
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


def test_index_survives_unclean_death(tmp_path: Path) -> None:
    """A crash (no close()) must leave the recording fully adoptable: the
    sidecar is persisted on every marked utterance and ``wave`` patches the WAV
    header on every write, so only the final in-flight utterance can be lost."""
    store = AudioStore(tmp_path)
    writer = store.writer("crash", sample_rate=_SR)
    utt = _pcm(1, 100)
    writer.append_frame(utt)
    writer.mark_utterance(Utterance(pcm=utt, start=0.5, end=1.1))
    # no close() - the process dies here

    assert store.exists("crash") is True
    out = store.read_utterances("crash")
    assert len(out) == 1
    assert out[0].pcm == utt
    assert out[0].start == 0.5
    writer.close()  # test cleanup only


def test_orphaned_wavs_lists_wavs_missing_their_index(tmp_path: Path) -> None:
    store = AudioStore(tmp_path)
    with store.writer("kept", sample_rate=_SR) as writer:
        writer.append_frame(_pcm(1, 10))
    with store.writer("orphan", sample_rate=_SR) as writer:
        writer.append_frame(_pcm(2, 10))
    store.index_path("orphan").unlink()

    assert store.orphaned_wavs() == ["orphan"]
    assert AudioStore(tmp_path / "missing").orphaned_wavs() == []


def _loud(n_samples: int) -> bytes:
    return b"\x10\x27" * n_samples  # constant 10000 - well above the test threshold


def _quiet(n_samples: int) -> bytes:
    return b"\x00\x00" * n_samples


def _energy_detector_factory(_sample_rate: int) -> SpeechDetector:
    def is_speech(frame: bytes) -> bool:
        return any(
            abs(int.from_bytes(frame[i : i + 2], "little", signed=True)) > 1000
            for i in range(0, len(frame), 2)
        )

    return is_speech


def test_rebuild_index_reconstructs_utterances(tmp_path: Path) -> None:
    """Re-running VAD over an orphaned WAV restores an index equivalent to what
    an unbroken session would have written: same spans, ``base_ts``-anchored
    timestamps, loud audio inside each recovered utterance."""
    store = AudioStore(tmp_path)
    with store.writer("s", sample_rate=_SR) as writer:
        writer.append_frame(_quiet(_SR // 2))  # 0.5 s lead-in
        writer.append_frame(_loud(_SR))  # 1.0 s speech
        writer.append_frame(_quiet(_SR))  # 1.0 s gap (> silence_ms, ends utterance 1)
        writer.append_frame(_loud(int(_SR * 0.6)))  # 0.6 s speech
        writer.append_frame(_quiet(_SR))  # 1.0 s tail
    store.index_path("s").unlink()
    assert store.orphaned_wavs() == ["s"]

    count = store.rebuild_index("s", detector_factory=_energy_detector_factory, base_ts=100.0)
    assert count == 2
    assert store.orphaned_wavs() == []

    out = store.read_utterances("s")
    assert len(out) == 2
    # Each span is its burst plus the chunker's 200 ms pre-roll and the 800 ms
    # trailing-silence run that ends an utterance.
    assert int(_SR * 1.9) <= len(out[0].pcm) // 2 <= int(_SR * 2.1)
    assert int(_SR * 1.5) <= len(out[1].pcm) // 2 <= int(_SR * 1.7)
    # base_ts anchors the timeline the way live capture's monotonic stamps do.
    assert 100.0 <= out[0].start <= 100.6
    assert 102.3 <= out[1].start <= 102.7
    # The recovered slices actually contain the loud audio.
    assert max(out[0].pcm) > 0
    assert max(out[1].pcm) > 0


def test_rebuild_index_abort_writes_nothing(tmp_path: Path) -> None:
    store = AudioStore(tmp_path)
    with store.writer("s", sample_rate=_SR) as writer:
        writer.append_frame(_loud(_SR))
    store.index_path("s").unlink()

    result = store.rebuild_index(
        "s", detector_factory=_energy_detector_factory, should_abort=lambda: True
    )
    assert result is None
    assert not store.index_path("s").exists()


def test_a_recording_in_progress_is_indexed_by_an_appended_journal(tmp_path: Path) -> None:
    """Marking an utterance appends one line; the sidecar is written on close.

    The sidecar used to be rewritten whole per utterance, which is the square
    of the session in bytes. What has to stay true is that the index on disk is
    current after every utterance, and that close leaves the one sidecar every
    other reader was written against.
    """
    store = AudioStore(tmp_path)
    writer = store.writer("s", sample_rate=_SR)
    writer.append_frame(_pcm(1, 10))
    assert not store.has_index("s")  # no utterance yet: still a WAV the VAD sweep owns

    for index in range(3):
        writer.append_frame(_pcm(index + 1, 10))
        writer.mark_utterance(
            Utterance(pcm=_pcm(index + 1, 10), start=float(index), end=index + 0.5)
        )
        assert not store.index_path("s").exists()
        assert len(store.journal_path("s").read_text().splitlines()) == index + 2  # + header
        assert [u.start for u in store.read_utterances("s")] == [float(i) for i in range(index + 1)]

    writer.close()

    assert store.index_path("s").exists()
    assert not store.journal_path("s").exists()
    assert len(store.read_utterances("s")) == 3


def test_a_journal_torn_by_a_crash_ends_at_its_last_whole_line(tmp_path: Path) -> None:
    store = AudioStore(tmp_path)
    writer = store.writer("s", sample_rate=_SR)
    for index in range(2):
        writer.append_frame(_pcm(index + 1, 10))
        writer.mark_utterance(
            Utterance(pcm=_pcm(index + 1, 10), start=float(index), end=index + 0.5)
        )
    with store.journal_path("s").open("a") as journal:
        journal.write('{"start": 2.0, "end"')  # the process died mid-append

    assert [u.start for u in store.read_utterances("s")] == [0.0, 1.0]
    assert store.orphaned_wavs() == []

    # The next start turns what survived into an ordinary sidecar.
    assert store.consolidate_journals() == 1
    assert store.index_path("s").exists()
    assert not store.journal_path("s").exists()
    assert [u.start for u in store.read_utterances("s")] == [0.0, 1.0]
    writer.close()  # test cleanup only


def test_consolidating_leaves_the_session_being_recorded_alone(tmp_path: Path) -> None:
    store = AudioStore(tmp_path)
    writer = store.writer("live", sample_rate=_SR)
    writer.append_frame(_pcm(1, 10))
    writer.mark_utterance(Utterance(pcm=_pcm(1, 10), start=0.0, end=0.5))

    assert store.consolidate_journals(skip="live") == 0
    assert store.journal_path("live").exists()
    writer.close()


def test_deleting_a_session_takes_its_journal(tmp_path: Path) -> None:
    store = AudioStore(tmp_path)
    writer = store.writer("s", sample_rate=_SR)
    writer.append_frame(_pcm(1, 10))
    writer.mark_utterance(Utterance(pcm=_pcm(1, 10), start=0.0, end=0.5))

    store.delete("s")

    assert not store.journal_path("s").exists()
    assert not store.exists("s")
    writer.close()
    store.delete("s")


def test_stored_utterances_are_read_one_at_a_time(tmp_path: Path) -> None:
    store = AudioStore(tmp_path)
    with store.writer("s", sample_rate=_SR) as writer:
        writer.append_frame(_pcm(0, 50))
        writer.append_frame(_pcm(1, 100))
        writer.mark_utterance(Utterance(pcm=_pcm(1, 100), start=0.5, end=1.1))
        writer.append_frame(_pcm(2, 80))
        writer.mark_utterance(Utterance(pcm=_pcm(2, 80), start=1.5, end=2.0))

    with store.open_utterances("s") as utterances:
        assert len(utterances) == 2
        feed = iter(utterances)
        assert next(feed).pcm == _pcm(1, 100)
        assert next(feed).pcm == _pcm(2, 80)
        assert next(feed, None) is None
