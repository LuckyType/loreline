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
import wave
from pathlib import Path
from typing import TYPE_CHECKING, Self

from loreline.audio.chunker import Utterance

if TYPE_CHECKING:
    from collections.abc import Sequence
    from types import TracebackType

_SAMPLE_WIDTH = 2  # 16-bit
_CHANNELS = 1
_COPY_CHUNK_FRAMES = 65536


class SessionAudioWriter:
    """Streaming WAV writer that records utterance boundaries.

    Use as a context manager or call :meth:`close` explicitly. ``close`` is
    idempotent and finalises both the WAV header and the index sidecar.
    """

    def __init__(self, wav_path: Path, index_path: Path, *, sample_rate: int) -> None:
        self._wav_path = wav_path
        self._index_path = index_path
        self._sample_rate = sample_rate
        self._index: list[dict[str, float | int]] = []
        self._frames_written = 0
        wav_path.parent.mkdir(parents=True, exist_ok=True)
        self._wav = wave.open(str(wav_path), "wb")  # noqa: SIM115 - closed in close()
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
        is exact (no timestamp correlation needed).
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

    def close(self) -> None:
        """Finalise the WAV header and write the index sidecar (idempotent)."""
        if self._closed:
            return
        self._closed = True
        self._wav.close()
        self._index_path.write_text(
            json.dumps(
                {
                    "sample_rate": self._sample_rate,
                    "channels": _CHANNELS,
                    "utterances": self._index,
                },
                indent=2,
            )
        )

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
            self.index_path(dest_id).write_text(
                json.dumps(
                    {
                        "sample_rate": sample_rate,
                        "channels": _CHANNELS,
                        "utterances": merged_entries,
                    },
                    indent=2,
                )
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
