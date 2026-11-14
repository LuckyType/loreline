"""Per-session audio store: continuous WAV + an utterance index.

The full continuous mic stream (including silence/gaps) is written to a single
mono 16-bit WAV per session via :meth:`SessionAudioWriter.append_frame`, so the
audio can be re-VAD'd / re-diarized from true source later. A JSON sidecar
records each voiced utterance's offset into that continuous stream (plus original
timestamps), so :meth:`AudioStore.open_utterances` reconstructs the exact
utterances for re-STT without re-running VAD. While a session is being recorded
that index is an append-only journal; the sidecar is written when it ends.
"""

from __future__ import annotations

import json
import os
import wave
from pathlib import Path
from typing import TYPE_CHECKING, Self, TextIO

from loreline.audio.chunker import SpeechDetector, Utterance, VadChunker

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence
    from types import TracebackType

_SAMPLE_WIDTH = 2  # 16-bit
_CHANNELS = 1
_COPY_CHUNK_FRAMES = 65536


def _write_index_file(
    path: Path, *, sample_rate: int, entries: Sequence[dict[str, float | int]]
) -> None:
    """Write an index sidecar atomically (write-to-temp + rename).

    The sidecar is rewritten while a session is live, so a crash mid-write must
    never leave a truncated file behind - the whole point of keeping it current
    is that whatever is on disk is always loadable.
    """
    payload = json.dumps(
        {
            "sample_rate": sample_rate,
            "channels": _CHANNELS,
            "utterances": list(entries),
        },
        indent=2,
    )
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(payload)
    os.replace(tmp, path)


def _journal_path(index_path: Path) -> Path:
    """Where a recording in progress keeps its utterance index.

    ``<id>.index.json`` becomes ``<id>.index.jsonl``: one line naming the
    sample rate, then one line per utterance, appended as each one ends.
    """
    return index_path.with_name(index_path.name + "l")


def _read_journal(path: Path) -> tuple[int, list[dict[str, float | int]]]:
    """A journal's sample rate and entries, stopping at the first torn line.

    A process killed mid-append leaves a last line that is not JSON. Everything
    before it was written whole, so that is where the index ends - the same
    "at most the final utterance" loss the rewritten sidecar used to promise.
    """
    sample_rate = 0
    entries: list[dict[str, float | int]] = []
    with path.open(encoding="utf-8") as handle:
        for number, line in enumerate(handle):
            try:
                record = json.loads(line)
            except ValueError:
                break
            if number == 0:
                sample_rate = int(record["sample_rate"])
            else:
                entries.append(record)
    return sample_rate, entries


class SessionAudioWriter:
    """Streaming WAV writer that records utterance boundaries.

    Use as a context manager or call :meth:`close` explicitly. Each marked
    utterance is appended to a journal beside the WAV and the ``wave`` module
    patches the WAV header on every write, so an unclean death (crash, power
    loss) loses at most the final in-flight utterance - the recording on disk
    stays complete and re-processable without a clean ``close``. ``close`` is
    idempotent, writes the index sidecar and retires the journal.

    A journal rather than the sidecar rewritten each time, which is what this
    did before: the sidecar holds every utterance so far, so rewriting it per
    utterance wrote the square of the session - several hundred megabytes over
    a four-hour evening, about as much as the audio itself, onto what is
    usually an SD card. An append costs one line whatever the session's length.
    Every reader goes through :meth:`AudioStore.load_index`, which reads
    whichever of the two is there, so a recording still in progress and one
    whose process died are both readable exactly as before.
    """

    def __init__(self, wav_path: Path, index_path: Path, *, sample_rate: int) -> None:
        self._wav_path = wav_path
        self._index_path = index_path
        self._sample_rate = sample_rate
        self._index: list[dict[str, float | int]] = []
        self._frames_written = 0
        wav_path.parent.mkdir(parents=True, exist_ok=True)
        # Own the file handle (rather than letting wave open the path) so
        # mark_utterance can flush buffered PCM to disk - ``wave`` exposes no
        # flush of its own, and its buffer only reaches disk as a side effect
        # of the header patch's seek, which skips the very first write.
        self._file = wav_path.open("wb")
        self._wav = wave.open(self._file, "wb")  # noqa: SIM115 - closed in close()
        self._wav.setnchannels(_CHANNELS)
        self._wav.setsampwidth(_SAMPLE_WIDTH)
        self._wav.setframerate(sample_rate)
        self._closed = False
        self._journal_path = _journal_path(index_path)
        self._journal: TextIO | None = None

    def append_frame(self, frame: bytes) -> None:
        """Append one raw capture frame to the continuous session WAV."""
        self._wav.writeframes(frame)
        self._frames_written += len(frame) // (_SAMPLE_WIDTH * _CHANNELS)

    def mark_utterance(self, utterance: Utterance) -> None:
        """Record a voiced utterance's span in the continuous stream.

        Must be called right after the utterance's final frame was appended: the
        utterance is the most recent ``n_frames`` samples written, so its offset
        is exact (no timestamp correlation needed). Persists the updated index
        sidecar before returning (disk I/O - call off the event loop).
        """
        n_frames = len(utterance.pcm) // (_SAMPLE_WIDTH * _CHANNELS)
        self._index.append(
            {
                "start": utterance.start,
                "end": utterance.end,
                "offset_frames": max(0, self._frames_written - n_frames),
                "n_frames": n_frames,
            }
        )
        # WAV first, sidecar second: the persisted index only ever references
        # audio that has already reached the on-disk WAV.
        self._file.flush()
        if self._journal is None:
            # Opened on the first utterance, not at construction, so a session
            # that never heard one still has no index at all - which is what
            # marks its WAV as one the startup sweep should run VAD over.
            self._journal = self._journal_path.open("w", encoding="utf-8")
            header = {"sample_rate": self._sample_rate, "channels": _CHANNELS}
            self._journal.write(json.dumps(header) + "\n")
        self._journal.write(json.dumps(self._index[-1]) + "\n")
        self._journal.flush()

    def close(self) -> None:
        """Finalise the WAV header and write the index sidecar (idempotent)."""
        if self._closed:
            return
        self._closed = True
        self._wav.close()  # patches the header; doesn't close the caller-owned file
        self._file.close()
        if self._journal is not None:
            self._journal.close()
        _write_index_file(self._index_path, sample_rate=self._sample_rate, entries=self._index)
        # Only once the sidecar is in place: until then the journal is the index.
        self._journal_path.unlink(missing_ok=True)

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


class AudioStore:
    """Resolve per-session audio paths and read/write session audio."""

    def __init__(self, root: Path) -> None:
        self._root = root

    @property
    def root(self) -> Path:
        """Directory the session WAVs and index sidecars are written to.

        Exposed so a live session can watch free space on the filesystem its
        recording actually lands on, which is not necessarily the one holding
        the rest of the data dir.
        """
        return self._root

    def wav_path(self, session_id: str) -> Path:
        return self._root / f"{session_id}.wav"

    def index_path(self, session_id: str) -> Path:
        return self._root / f"{session_id}.index.json"

    def journal_path(self, session_id: str) -> Path:
        return _journal_path(self.index_path(session_id))

    def has_index(self, session_id: str) -> bool:
        """Whether this session's utterances are indexed, by sidecar or by journal."""
        return self.index_path(session_id).exists() or self.journal_path(session_id).exists()

    def exists(self, session_id: str) -> bool:
        return self.wav_path(session_id).exists() and self.has_index(session_id)

    def delete(self, session_id: str) -> None:
        """Remove a session's stored WAV + index sidecar (no-op if absent)."""
        self.wav_path(session_id).unlink(missing_ok=True)
        self.index_path(session_id).unlink(missing_ok=True)
        self.journal_path(session_id).unlink(missing_ok=True)

    def load_index(self, session_id: str) -> list[dict[str, float | int]]:
        """A session's utterance entries, from the sidecar or else the journal.

        The sidecar wins when both exist: it is written last and whole, so a
        journal still beside it is one ``close`` did not get to delete.
        """
        index_path = self.index_path(session_id)
        if index_path.exists():
            index = json.loads(index_path.read_text())
            entries: list[dict[str, float | int]] = index["utterances"]
            return entries
        return _read_journal(self.journal_path(session_id))[1]

    def consolidate_journals(self, *, skip: str | None = None) -> int:
        """Turn journals a dead process left behind into sidecars; return how many.

        A journal is complete on its own, so this is tidying rather than
        recovery: it leaves every finished recording with the one file the
        rest of the store was written against. ``skip`` is the session being
        recorded right now, whose journal is still being appended to.
        """
        if not self._root.is_dir():
            return 0
        done = 0
        for journal in self._root.glob("*.index.jsonl"):
            session_id = journal.name.removesuffix(".index.jsonl")
            if session_id == skip:
                continue
            if not self.index_path(session_id).exists():
                sample_rate, entries = _read_journal(journal)
                if sample_rate <= 0:
                    continue  # header never landed; the VAD sweep rebuilds it
                _write_index_file(
                    self.index_path(session_id), sample_rate=sample_rate, entries=entries
                )
                done += 1
            journal.unlink(missing_ok=True)
        return done

    def writer(self, session_id: str, *, sample_rate: int) -> SessionAudioWriter:
        return SessionAudioWriter(
            self.wav_path(session_id),
            self.index_path(session_id),
            sample_rate=sample_rate,
        )

    def duration_s(self, session_id: str) -> float:
        """Length of the stored continuous WAV in seconds."""
        with wave.open(str(self.wav_path(session_id)), "rb") as wav:
            rate = wav.getframerate()
            return wav.getnframes() / rate if rate else 0.0

    def orphaned_wavs(self) -> list[str]:
        """Session ids that have a stored WAV but no index sidecar.

        These are recordings whose writer never got to persist its index (a
        crash before the first utterance, or audio written by a pre-sidecar
        version of the app) - complete audio that re-transcription can't see
        until :meth:`rebuild_index` restores the sidecar.
        """
        if not self._root.is_dir():
            return []
        return sorted(
            path.stem for path in self._root.glob("*.wav") if not self.has_index(path.stem)
        )

    def rebuild_index(
        self,
        session_id: str,
        *,
        detector_factory: Callable[[int], SpeechDetector],
        base_ts: float = 0.0,
        frame_ms: int = 20,
        should_abort: Callable[[], bool] | None = None,
    ) -> int | None:
        """Reconstruct a missing index sidecar by re-running VAD over the WAV.

        Streams the continuous recording through a fresh detector + chunker with
        the same shape the live capture uses, so the rebuilt utterance spans
        match what an unbroken session would have stored. ``base_ts`` (the
        session's ``started_mono``) stands in for the original capture
        timestamps: audio position zero is session start, which is the same
        convention the live path's monotonic stamps encode. Blocking and
        CPU-heavy (VAD inference) - run off the event loop. Returns the
        utterance count, or None when ``should_abort`` cut it short (no sidecar
        is written then).
        """
        with wave.open(str(self.wav_path(session_id)), "rb") as wav:
            sample_rate = wav.getframerate()
            detector = detector_factory(sample_rate)
            chunker = VadChunker(sample_rate=sample_rate, frame_ms=frame_ms)
            frame_samples = max(1, sample_rate * frame_ms // 1000)
            entries: list[dict[str, float | int]] = []
            frames_read = 0

            def mark(utterance: Utterance) -> None:
                n_frames = len(utterance.pcm) // (_SAMPLE_WIDTH * _CHANNELS)
                entries.append(
                    {
                        "start": utterance.start,
                        "end": utterance.end,
                        "offset_frames": max(0, frames_read - n_frames),
                        "n_frames": n_frames,
                    }
                )

            while frame := wav.readframes(frame_samples):
                if should_abort is not None and should_abort():
                    return None
                frames_read += len(frame) // (_SAMPLE_WIDTH * _CHANNELS)
                ts = base_ts + frames_read / sample_rate
                utterance = chunker.feed(frame, ts=ts, is_speech=detector(frame))
                if utterance is not None:
                    mark(utterance)
            final = chunker.flush()
            if final is not None:
                mark(final)

        _write_index_file(self.index_path(session_id), sample_rate=sample_rate, entries=entries)
        return len(entries)

    def merge(self, source_ids: Sequence[str], dest_id: str) -> None:
        """Concatenate the sources' WAVs + utterance indexes into ``dest_id``.

        Parts are appended in the given order. Utterance timestamps in the
        merged index are derived from audio position (frame offsets), so the
        merged WAV, its index, and a back-to-back-merged transcript line up
        regardless of the sources' original capture clocks. Every source must
        have stored audio at one shared sample rate; raises ``ValueError``
        otherwise (and never leaves partial output behind).
        """
        rates: list[int] = []
        for sid in source_ids:
            if not self.exists(sid):
                msg = f"session {sid!r} has no stored audio"
                raise ValueError(msg)
            with wave.open(str(self.wav_path(sid)), "rb") as wav:
                rates.append(wav.getframerate())
        if len(set(rates)) != 1:
            msg = f"sources mix sample rates {sorted(set(rates))}"
            raise ValueError(msg)
        sample_rate = rates[0]

        merged_entries: list[dict[str, float | int]] = []
        frames_total = 0
        try:
            with wave.open(str(self.wav_path(dest_id)), "wb") as out:
                out.setnchannels(_CHANNELS)
                out.setsampwidth(_SAMPLE_WIDTH)
                out.setframerate(sample_rate)
                for sid in source_ids:
                    with wave.open(str(self.wav_path(sid)), "rb") as src:
                        part_frames = src.getnframes()
                        while chunk := src.readframes(_COPY_CHUNK_FRAMES):
                            out.writeframes(chunk)
                    for entry in self.load_index(sid):
                        offset_frames = frames_total + int(entry["offset_frames"])
                        n_frames = int(entry["n_frames"])
                        merged_entries.append(
                            {
                                "start": offset_frames / sample_rate,
                                "end": (offset_frames + n_frames) / sample_rate,
                                "offset_frames": offset_frames,
                                "n_frames": n_frames,
                            }
                        )
                    frames_total += part_frames
            _write_index_file(
                self.index_path(dest_id), sample_rate=sample_rate, entries=merged_entries
            )
        except Exception:
            self.delete(dest_id)
            raise

    def read_wav(self, session_id: str) -> tuple[bytes, int]:
        """Return the full continuous session WAV bytes + its sample rate."""
        path = self.wav_path(session_id)
        data = path.read_bytes()
        with wave.open(str(path), "rb") as wav:
            sample_rate = wav.getframerate()
        return data, sample_rate

    def open_utterances(self, session_id: str) -> StoredUtterances:
        """Open a session's stored utterances for reading one at a time.

        Raises here, not on the first read, when the audio or its index is
        missing, so a caller learns that before it has started anything.
        """
        return StoredUtterances(self.wav_path(session_id), self.load_index(session_id))

    def read_utterances(self, session_id: str) -> list[Utterance]:
        """Reconstruct every stored utterance from the WAV + index, in memory.

        Fine for a short recording and for tests. A whole evening is several
        hundred megabytes this way; anything that walks a session utterance by
        utterance uses :meth:`open_utterances` instead.
        """
        with self.open_utterances(session_id) as utterances:
            return list(utterances)


class StoredUtterances:
    """A session's utterances, each read from the WAV as it is asked for.

    Reading the recording whole and slicing it held the entire session in
    memory, and the slices beside it: close to a gigabyte for four hours, on a
    device that may have two. An utterance is at most half a minute, so reading
    them one at a time keeps a re-transcription's footprint at one of those
    whatever the session's length.

    Iterable once, in index order. Reads block (they are file I/O), so iterate
    off the event loop.
    """

    def __init__(self, wav_path: Path, entries: list[dict[str, float | int]]) -> None:
        self._entries = entries
        self._wav = wave.open(str(wav_path), "rb")  # noqa: SIM115 - closed in close()

    def __len__(self) -> int:
        return len(self._entries)

    def __iter__(self) -> Iterator[Utterance]:
        for entry in self._entries:
            self._wav.setpos(min(int(entry["offset_frames"]), self._wav.getnframes()))
            yield Utterance(
                pcm=self._wav.readframes(int(entry["n_frames"])),
                start=float(entry["start"]),
                end=float(entry["end"]),
            )

    def close(self) -> None:
        self._wav.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()
