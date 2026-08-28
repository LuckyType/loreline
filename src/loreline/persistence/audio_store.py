"""Per-session audio store: continuous WAV + an utterance index.

The full continuous mic stream (including silence/gaps) is written to a single
mono 16-bit WAV per session via :meth:`SessionAudioWriter.append_frame`, so the
audio can be re-VAD'd / re-diarized from true source later. A JSON sidecar
records each voiced utterance's offset into that continuous stream (plus original
timestamps), so :meth:`AudioStore.read_utterances` reconstructs the exact
utterances for re-STT without re-running VAD.
"""

from __future__ import annotations

import json
import os
import wave
from pathlib import Path
from typing import TYPE_CHECKING, Self

from loreline.audio.chunker import SpeechDetector, Utterance, VadChunker

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
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


class SessionAudioWriter:
    """Streaming WAV writer that records utterance boundaries.

    Use as a context manager or call :meth:`close` explicitly. The index
    sidecar is re-written (atomically) after every marked utterance and the
    ``wave`` module patches the WAV header on every write, so an unclean death
    (crash, power loss) loses at most the final in-flight utterance - the
    recording on disk stays complete and re-processable without a clean
    ``close``. ``close`` is idempotent and finalises both files.
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
        _write_index_file(self._index_path, sample_rate=self._sample_rate, entries=self._index)

    def close(self) -> None:
        """Finalise the WAV header and write the index sidecar (idempotent)."""
        if self._closed:
            return
        self._closed = True
        self._wav.close()  # patches the header; doesn't close the caller-owned file
        self._file.close()
        _write_index_file(self._index_path, sample_rate=self._sample_rate, entries=self._index)

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

    def wav_path(self, session_id: str) -> Path:
        return self._root / f"{session_id}.wav"

    def index_path(self, session_id: str) -> Path:
        return self._root / f"{session_id}.index.json"

    def exists(self, session_id: str) -> bool:
        return self.wav_path(session_id).exists() and self.index_path(session_id).exists()

    def delete(self, session_id: str) -> None:
        """Remove a session's stored WAV + index sidecar (no-op if absent)."""
        self.wav_path(session_id).unlink(missing_ok=True)
        self.index_path(session_id).unlink(missing_ok=True)

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
            path.stem
            for path in self._root.glob("*.wav")
            if not self.index_path(path.stem).exists()
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
                    index = json.loads(self.index_path(sid).read_text())
                    entries: list[dict[str, float | int]] = index["utterances"]
                    for entry in entries:
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

    def read_utterances(self, session_id: str) -> list[Utterance]:
        """Reconstruct the stored utterances from the WAV + index sidecar."""
        index = json.loads(self.index_path(session_id).read_text())
        entries: list[dict[str, float | int]] = index["utterances"]
        with wave.open(str(self.wav_path(session_id)), "rb") as wav:
            pcm = wav.readframes(wav.getnframes())
        frame_bytes = _SAMPLE_WIDTH * _CHANNELS
        utterances: list[Utterance] = []
        for entry in entries:
            start_byte = int(entry["offset_frames"]) * frame_bytes
            end_byte = start_byte + int(entry["n_frames"]) * frame_bytes
            utterances.append(
                Utterance(
                    pcm=pcm[start_byte:end_byte],
                    start=float(entry["start"]),
                    end=float(entry["end"]),
                )
            )
        return utterances
