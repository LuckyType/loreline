"""Reference self-hosted diarization service (sherpa-onnx).

Runs on a LAN x86 host (off the capture device per D1/D2). Wraps sherpa-onnx
offline speaker diarization behind the HTTP contract expected by Loreline's
``RemoteDiarizer``:

- ``GET  /healthz`` -> ``{"status": "ok"}``, or ``503`` with ``{"detail": ...}``
  until both models below are configured and have loaded successfully
- ``POST /diarize`` (multipart ``file`` = mono WAV, optional ``session_id``) ->
  ``{"segments": [{"start", "end", "speaker"}, ...]}``, or the same ``503``
  shape while the models are not ready
- ``DELETE /sessions/{session_id}`` -> ``{"deleted": bool}``, forgetting one
  session's remembered speakers

The service is stateless per call unless the caller passes ``session_id``, in
which case it remembers that session's voices and returns labels that mean the
same person from one call to the next (see "Session speaker memory" below).
That is what makes the labels usable at all for a caller that diarizes one
utterance at a time, which is how Loreline's live capture calls this.

Models are configured via environment variables (see README). The sherpa-onnx
import is deferred so the module imports cleanly where the native wheel is
absent (e.g. lint/typecheck in the main project CI).
"""

from __future__ import annotations

import io
import math
import os
import threading
import time
import wave
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from fastapi import FastAPI, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse

_SEGMENTATION_MODEL = os.environ.get("DIAR_SEGMENTATION_MODEL", "")
_EMBEDDING_MODEL = os.environ.get("DIAR_EMBEDDING_MODEL", "")


# ---------------------------------------------------------------------------
# Session speaker memory
# ---------------------------------------------------------------------------
# Why this exists: Loreline calls /diarize once per utterance, and clustering
# each utterance on its own gives labels that mean nothing across calls - every
# single-speaker utterance comes back as "Speaker 0" whoever spoke, so the
# session's rename map ("Speaker 0" -> "Alice") renames the whole table to
# Alice. A caller that passes ``session_id`` gets a bank of speaker embeddings
# kept per session instead: this call's clusters are matched against the voices
# already heard, so "Speaker 1" is the same person in the first utterance and
# in the hundredth. Without ``session_id`` nothing is remembered and the
# per-call behavior is exactly what it was.


def _env_float(name: str, default: float) -> float:
    """Read a float from the environment, falling back on anything unparseable."""
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    """Read an int from the environment, falling back on anything unparseable."""
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


# Cosine similarity above which a cluster is the same voice as a remembered one.
# Measured on a two-narrator LibriVox clip through the models this service is
# deployed with: same speaker 0.63 to 0.88 (median 0.77), different speakers
# -0.00 to 0.22 (median 0.14). 0.5 sits in the middle of that gap, and is also
# what the within-call FastClustering uses as its distance threshold, so a
# call's own clusters and the bank draw the line in the same place.
DEFAULT_THRESHOLD = _env_float("DIAR_SPEAKER_THRESHOLD", 0.5)
# A bank is a few hundred floats, so eviction is about not keeping one entry per
# session forever rather than about size. An hour idle outlasts any gap within a
# live session and still clears a session whose end was never reported.
DEFAULT_TTL_S = _env_float("DIAR_SESSION_TTL_S", 3600.0)
DEFAULT_MAX_SESSIONS = _env_int("DIAR_MAX_SESSIONS", 32)

# Extra similarity demanded before an existing speaker is reused while the bank
# holds fewer voices than ``min_speakers``. "There are at least three people
# here" is a reason to be reluctant to collapse two of them into one; the margin
# stays below the measured same-speaker floor, so it splits a voice rarely.
_FLOOR_MARGIN = 0.1
_MAX_THRESHOLD = 0.9
# How many observations a centroid averages before it stops moving much. Capped
# so a voice can still drift (a chair scrapes back, a headset comes off) instead
# of freezing on the session's first minute.
_CENTROID_MEMORY = 20
# Below this a segment is too short to embed reliably, so it only contributes to
# its cluster's embedding when the cluster has nothing longer.
_MIN_EMBED_S = 0.5
# How many segments of one cluster are embedded to build its centroid for a
# call. Bounds the added inference on a whole-session call, where a cluster can
# hold thousands of segments and the longest handful already place the voice.
_MAX_EMBED_SEGMENTS = 8
# How much of one segment is embedded, from its middle. Embedding cost is linear
# in audio length while the embedding itself stops improving after a few
# seconds, so this holds what session memory adds to a call roughly flat
# instead of letting it follow the length of whatever was said.
_MAX_EMBED_S = 4.0


def _normalize(vector: Sequence[float]) -> list[float]:
    """Unit-length copy of ``vector``; an all-zero vector is returned as it is."""
    norm = math.sqrt(sum(value * value for value in vector))
    if norm == 0.0:
        return list(vector)
    return [value / norm for value in vector]


def _cosine(left: Sequence[float], right: Sequence[float]) -> float:
    """Cosine similarity of two already-normalized vectors."""
    return sum(a * b for a, b in zip(left, right, strict=False))


@dataclass
class _Voice:
    """One remembered speaker: a running mean embedding and its weight."""

    centroid: list[float]
    count: int = 1

    def observe(self, embedding: Sequence[float]) -> None:
        """Fold one more observation of this voice into its centroid."""
        weight = min(self.count, _CENTROID_MEMORY)
        blended = [c * weight + e for c, e in zip(self.centroid, embedding, strict=False)]
        self.centroid = _normalize(blended)
        self.count += 1


class SpeakerBank:
    """The voices one session has heard, as centroids over their embeddings.

    :meth:`resolve` takes one embedding per cluster of one call (not one per
    segment) and answers with a stable speaker id for each. Matching is greedy
    from the best pair down and one-to-one: two clusters that the call itself
    decided were different people never collapse onto the same remembered
    voice.
    """

    def __init__(self, *, threshold: float = DEFAULT_THRESHOLD) -> None:
        self._threshold = threshold
        self._voices: list[_Voice] = []

    @property
    def speakers(self) -> int:
        """How many distinct voices this session has heard so far."""
        return len(self._voices)

    def resolve(
        self,
        embeddings: Sequence[Sequence[float]],
        *,
        min_speakers: int | None = None,
        max_speakers: int | None = None,
    ) -> list[int]:
        """Speaker ids for this call's clusters, in the order given.

        Each cluster takes the most similar remembered voice above the
        threshold, opens a new one while under ``max_speakers``, and otherwise
        is forced onto the nearest voice left. A forced match labels but does
        not update that voice, so a bank at its cap cannot be polluted by the
        speaker it had no room for.
        """
        vectors = [_normalize(embedding) for embedding in embeddings]
        threshold = self._match_threshold(min_speakers)
        assigned = self._match(vectors, threshold)
        # Applied after matching, never during it, so every pair was scored
        # against the centroids this call started from.
        for index, speaker in assigned.items():
            self._voices[speaker].observe(vectors[index])
        taken = set(assigned.values())
        ids: list[int] = []
        for index, vector in enumerate(vectors):
            speaker = assigned.get(index)
            ids.append(speaker if speaker is not None else self._open(vector, taken, max_speakers))
        return ids

    def _match_threshold(self, min_speakers: int | None) -> float:
        if min_speakers is not None and len(self._voices) < min_speakers:
            return min(self._threshold + _FLOOR_MARGIN, _MAX_THRESHOLD)
        return self._threshold

    def _match(self, vectors: list[list[float]], threshold: float) -> dict[int, int]:
        """Cluster index -> remembered speaker, best pairs first, one to one."""
        pairs = sorted(
            (
                (_cosine(vector, voice.centroid), index, speaker)
                for index, vector in enumerate(vectors)
                for speaker, voice in enumerate(self._voices)
            ),
            key=lambda pair: pair[0],
            reverse=True,
        )
        assigned: dict[int, int] = {}
        taken: set[int] = set()
        for similarity, index, speaker in pairs:
            if similarity < threshold:
                break
            if index in assigned or speaker in taken:
                continue
            assigned[index] = speaker
            taken.add(speaker)
        return assigned

    def _open(self, vector: Sequence[float], taken: set[int], max_speakers: int | None) -> int:
        """A new speaker for ``vector``, or the nearest one the cap leaves free."""
        if max_speakers is None or len(self._voices) < max(1, max_speakers):
            self._voices.append(_Voice(centroid=list(vector)))
            return len(self._voices) - 1
        free = [s for s in range(len(self._voices)) if s not in taken]
        candidates = free or list(range(len(self._voices)))
        speaker = max(candidates, key=lambda s: _cosine(vector, self._voices[s].centroid))
        taken.add(speaker)
        return speaker


@dataclass
class _Entry:
    """One session's bank and when it was last used."""

    bank: SpeakerBank
    last_used: float


class SessionBanks:
    """Every live session's :class:`SpeakerBank`, with a bounded lifetime.

    Two things bound the memory, because "the session ended" is a message that
    can go missing: an idle TTL, swept on every call, and a cap on how many
    sessions are kept at once, which evicts the least recently used. A session
    is also dropped explicitly by ``DELETE /sessions/{id}``, which is what
    Loreline sends when a capture stops.

    The lock covers the whole match-and-update, which is plain Python
    arithmetic over a handful of vectors: ``/diarize`` is a sync route running
    in Starlette's threadpool, so two utterances of one session can otherwise
    update the same centroid at once.
    """

    def __init__(
        self,
        *,
        threshold: float = DEFAULT_THRESHOLD,
        ttl_s: float = DEFAULT_TTL_S,
        max_sessions: int = DEFAULT_MAX_SESSIONS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._threshold = threshold
        self._ttl_s = ttl_s
        self._max_sessions = max(1, max_sessions)
        self._clock = clock
        self._entries: dict[str, _Entry] = {}
        self._lock = threading.Lock()

    def __len__(self) -> int:
        return len(self._entries)

    def resolve(
        self,
        session_id: str,
        embeddings: Sequence[Sequence[float]],
        *,
        min_speakers: int | None = None,
        max_speakers: int | None = None,
    ) -> list[int]:
        """Speaker ids for one call's clusters, within ``session_id``'s bank."""
        with self._lock:
            self._evict_idle()
            entry = self._entries.pop(session_id, None)
            if entry is None:
                entry = _Entry(bank=SpeakerBank(threshold=self._threshold), last_used=0.0)
            entry.last_used = self._clock()
            self._entries[session_id] = entry  # re-inserted last: most recently used
            while len(self._entries) > self._max_sessions:
                del self._entries[next(iter(self._entries))]
            return entry.bank.resolve(
                embeddings, min_speakers=min_speakers, max_speakers=max_speakers
            )

    def delete(self, session_id: str) -> bool:
        """Forget one session's speakers; False when it was not remembered."""
        with self._lock:
            return self._entries.pop(session_id, None) is not None

    def _evict_idle(self) -> None:
        cutoff = self._clock() - self._ttl_s
        for session_id in [s for s, entry in self._entries.items() if entry.last_used < cutoff]:
            del self._entries[session_id]


# ---------------------------------------------------------------------------
# Models and audio
# ---------------------------------------------------------------------------


def _load_pipeline(num_clusters: int = -1):
    """Build a diarization pipeline; ``num_clusters`` -1 = auto (threshold-based)."""
    import numpy as np  # noqa: PLC0415
    import sherpa_onnx  # noqa: PLC0415

    if not _SEGMENTATION_MODEL or not _EMBEDDING_MODEL:
        msg = "DIAR_SEGMENTATION_MODEL and DIAR_EMBEDDING_MODEL must be set"
        raise RuntimeError(msg)

    config = sherpa_onnx.OfflineSpeakerDiarizationConfig(
        segmentation=sherpa_onnx.OfflineSpeakerSegmentationModelConfig(
            pyannote=sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(
                model=_SEGMENTATION_MODEL
            ),
        ),
        embedding=sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=_EMBEDDING_MODEL),
        clustering=sherpa_onnx.FastClusteringConfig(num_clusters=num_clusters, threshold=0.5),
    )
    return sherpa_onnx.OfflineSpeakerDiarization(config), np


def _load_extractor():
    """Build the standalone embedding extractor the session bank matches on.

    A second load of the same embedding model the pipeline already holds:
    sherpa-onnx's diarization API returns clustered segments and never the
    embeddings behind them, so there is nothing to reuse. It costs one more ONNX
    session in RAM and is only loaded once a caller actually asks for session
    memory.
    """
    import sherpa_onnx  # noqa: PLC0415

    if not _EMBEDDING_MODEL:
        msg = "DIAR_EMBEDDING_MODEL must be set"
        raise RuntimeError(msg)
    return sherpa_onnx.SpeakerEmbeddingExtractor(
        sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=_EMBEDDING_MODEL)
    )


def _resolve_num_clusters(min_speakers: int | None, max_speakers: int | None) -> int:
    """Map Loreline's speaker bounds to sherpa-onnx's exact-cluster count.

    FastClusteringConfig supports either auto clustering (``num_clusters=-1``)
    or an exact count, so only an exact bound (``min == max``) can be honored.

    Not used for a call that carries a ``session_id``: there the bounds describe
    the session, not the few seconds in this request, and one utterance holding
    fewer speakers than the table does is the normal case rather than an error.
    Such a call clusters automatically and the bank enforces ``max_speakers``.
    """
    if min_speakers is not None and max_speakers is not None and min_speakers == max_speakers:
        return max(1, min_speakers)
    return -1


def _read_wav(data: bytes) -> tuple[list[float], int]:
    with wave.open(io.BytesIO(data), "rb") as wav:
        rate = wav.getframerate()
        frames = wav.readframes(wav.getnframes())
    import numpy as np  # noqa: PLC0415

    samples = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
    return samples.tolist(), rate


def _embed(extractor, audio, rate: int, start: float, end: float) -> list[float] | None:
    """The unit-length speaker embedding of ``audio[start:end]``, None if empty.

    A long segment is embedded from its middle :data:`_MAX_EMBED_S` seconds
    only: the ends of a turn are where a breath or the next person starting is,
    and the middle is enough voice to place a speaker with.
    """
    if end - start > _MAX_EMBED_S:
        middle = (start + end) / 2
        start, end = middle - _MAX_EMBED_S / 2, middle + _MAX_EMBED_S / 2
    chunk = audio[int(start * rate) : int(end * rate)]
    if len(chunk) == 0:
        return None
    stream = extractor.create_stream()
    stream.accept_waveform(sample_rate=rate, waveform=chunk)
    stream.input_finished()
    return _normalize(list(extractor.compute(stream)))


def _cluster_embeddings(
    extractor, audio, rate: int, spans: list[tuple[float, float, int]]
) -> dict[int, list[float]]:
    """One duration-weighted mean embedding per cluster id in ``spans``.

    Longest segments first, and at most :data:`_MAX_EMBED_SEGMENTS` of them per
    cluster: a whole-session call (which is how a re-processing job diarizes)
    can carry thousands of segments, and embedding every one of them would cost
    far more inference than the centroid gains. Segments under
    :data:`_MIN_EMBED_S` hold too little voice to place a speaker with, so a
    cluster falls back to its longest segment only when it has nothing better.
    """
    per_cluster: dict[int, list[tuple[float, float, float]]] = {}
    for start, end, cluster in spans:
        per_cluster.setdefault(cluster, []).append((end - start, start, end))
    pooled: dict[int, list[float]] = {}
    for cluster, segments in per_cluster.items():
        segments.sort(reverse=True)
        usable = [s for s in segments if s[0] >= _MIN_EMBED_S][:_MAX_EMBED_SEGMENTS]
        weighted = [
            (duration, embedding)
            for duration, start, end in (usable or segments[:1])
            if (embedding := _embed(extractor, audio, rate, start, end)) is not None
        ]
        if not weighted:
            continue
        total = sum(duration for duration, _ in weighted)
        pooled[cluster] = [
            sum(duration * embedding[i] for duration, embedding in weighted) / total
            for i in range(len(weighted[0][1]))
        ]
    return pooled


def create_app() -> FastAPI:
    """Build the sherpa-onnx diarization service app."""
    app = FastAPI(title="loreline-diarization")
    state: dict[str, object] = {}
    lock = threading.Lock()
    banks = SessionBanks()

    def _ensure_pipeline(num_clusters: int) -> tuple[object, object]:
        """Return the cached pipeline for ``num_clusters``, loading it once.

        Shared by ``/healthz`` and ``/diarize`` so both agree on what "ready"
        means: whatever ``_load_pipeline`` raises when the models are not
        configured or fail to load is exactly what fails a health check too,
        instead of ``/healthz`` keeping its own, separate notion of readiness
        that can drift from what a real diarize call actually does.
        """
        pipelines: dict[int, tuple[object, object]] = state.setdefault("pipelines", {})  # type: ignore[assignment]
        if num_clusters in pipelines:
            return pipelines[num_clusters]
        with lock:
            if num_clusters not in pipelines:
                pipelines[num_clusters] = _load_pipeline(num_clusters)
            return pipelines[num_clusters]

    def _ensure_extractor() -> object:
        """Return the cached embedding extractor, loading it once."""
        extractor = state.get("extractor")
        if extractor is not None:
            return extractor
        with lock:
            if "extractor" not in state:
                state["extractor"] = _load_extractor()
            return state["extractor"]

    @app.get("/healthz")
    def healthz() -> JSONResponse:
        # Plain `def`, not `async def`: the first call loads the pipeline
        # (real file IO plus ONNX init), which is exactly the synchronous,
        # possibly slow work the comment on `/diarize` below already offloads
        # to the threadpool rather than run on the event loop.
        try:
            _ensure_pipeline(-1)
        except (RuntimeError, ImportError) as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return JSONResponse({"status": "ok"})

    @app.post("/diarize")
    def diarize(
        file: UploadFile,
        sample_rate: int = Form(16000),
        min_speakers: int | None = Form(None),
        max_speakers: int | None = Form(None),
        session_id: str | None = Form(None),
    ) -> JSONResponse:
        # A plain `def` route runs in Starlette's threadpool instead of the
        # event loop: pipeline.process() below is synchronous ONNX inference
        # that can take real wall-clock time, and this service has no other
        # concurrent work worth protecting the loop for, so offloading it is
        # strictly better than blocking every other in-flight request on it.
        num_clusters = -1 if session_id else _resolve_num_clusters(min_speakers, max_speakers)
        try:
            pipeline, np = _ensure_pipeline(num_clusters)
            extractor = _ensure_extractor() if session_id else None
        except (RuntimeError, ImportError) as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

        samples, rate = _read_wav(file.file.read())
        audio = np.array(samples, dtype=np.float32)
        result = pipeline.process(audio).sort_by_start_time()
        spans = [(float(seg.start), float(seg.end), int(seg.speaker)) for seg in result]
        if session_id and extractor is not None:
            labels = _session_labels(
                banks, session_id, extractor, audio, rate, spans, min_speakers, max_speakers
            )
        else:
            labels = _local_labels(spans)
        segments = [
            {"start": start, "end": end, "speaker": f"Speaker {labels[cluster]}"}
            for start, end, cluster in spans
        ]
        return JSONResponse({"segments": segments, "sample_rate": rate})

    @app.delete("/sessions/{session_id}")
    def forget_session(session_id: str) -> JSONResponse:
        """Drop one session's remembered speakers.

        Answers 200 either way rather than 404 for an unknown id: the caller
        sends this when a capture stops, whether or not that session ever
        diarized anything, and an error there would only be noise it has to
        swallow.
        """
        return JSONResponse({"deleted": banks.delete(session_id)})

    return app


def _local_labels(spans: list[tuple[float, float, int]]) -> dict[int, int]:
    """Cluster id -> label for one call on its own.

    Raw cluster ids can be sparse (unused clusters leave gaps); renumber them by
    first appearance so the transcript shows Speaker 0..k-1.
    """
    remap: dict[int, int] = {}
    for _start, _end, cluster in spans:
        remap.setdefault(cluster, len(remap))
    return remap


def _session_labels(
    banks: SessionBanks,
    session_id: str,
    extractor: object,
    audio: object,
    rate: int,
    spans: list[tuple[float, float, int]],
    min_speakers: int | None,
    max_speakers: int | None,
) -> dict[int, int]:
    """Cluster id -> label, resolved against what ``session_id`` has heard before."""
    local = _local_labels(spans)
    pooled = _cluster_embeddings(extractor, audio, rate, spans)
    if len(pooled) != len(local):
        # A cluster whose every segment fell outside the audio has no voice to
        # match on. Rather than label the rest of the call against the session
        # and that one against nothing, the whole call degrades to the
        # stateless labels, which is what a caller without a session gets.
        return local
    clusters = list(local)
    speakers = banks.resolve(
        session_id,
        [pooled[cluster] for cluster in clusters],
        min_speakers=min_speakers,
        max_speakers=max_speakers,
    )
    return dict(zip(clusters, speakers, strict=True))


app = create_app()
