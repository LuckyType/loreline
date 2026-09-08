"""Reference self-hosted diarization service (sherpa-onnx).

Runs on a LAN x86 host (off the capture device per D1/D2). Wraps sherpa-onnx
offline speaker diarization behind the HTTP contract expected by Loreline's
``RemoteDiarizer``:

- ``GET  /healthz`` -> ``{"status": "ok", "session_memory": true, "generation":
  "..."}``, or ``503`` with ``{"detail": ...}`` until both models below are
  configured and have loaded successfully
- ``POST /diarize`` (multipart ``file`` = mono WAV, optional ``session_id``) ->
  ``{"segments": [{"start", "end", "speaker"}, ...], "generation": "..."}``, or
  the same ``503`` shape while the models are not ready
- ``DELETE /sessions/{session_id}`` -> ``{"deleted": bool}``, forgetting one
  session's remembered speakers

Two fields in those answers exist for the caller rather than for a human:
``session_memory`` says this build understands ``session_id`` at all, since an
older image accepts the field and ignores it and so reads as a perfectly
healthy diarizer that calls everyone "Speaker 0"; ``generation`` identifies
this process, and changes when it restarts, which is the caller's only way to
notice that a session's numbering has started over (see the note on restarts
under "Session speaker memory").

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
import secrets
import threading
import time
import wave
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import cast

from fastapi import FastAPI, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse

_SEGMENTATION_MODEL = os.environ.get("DIAR_SEGMENTATION_MODEL", "")
_EMBEDDING_MODEL = os.environ.get("DIAR_EMBEDDING_MODEL", "")

# This process's identity, minted once at import and returned with every answer.
# The speaker bank below lives in this process's memory, so a restart forgets
# every session and numbers the next turn from Speaker 0 again - and nothing in
# the labels says so, because "Speaker 0" is exactly what the caller was already
# being told. A value that changes on restart is what lets the caller notice,
# say so once, and leave a human to rename the two halves apart.
GENERATION = secrets.token_hex(8)


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
#
# The bank is this process's memory and nothing else, which is deliberate (one
# container per box, no store to keep in step) and has one visible consequence:
# a restart mid-session forgets every voice and numbers the next turn from
# Speaker 0 again. Every answer therefore carries ``GENERATION``, so a caller
# can see that happen and say so, instead of quietly filing two people under one
# name.


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
# How many voices one session may open before a further one borrows the nearest
# label instead of getting its own. The live path sends no bounds at all - one
# utterance cannot say how many people are at the table - so without a default
# here a session is uncapped, and anything the clustering hands over as a
# cluster of its own can open a speaker that then lives for the rest of the
# evening. A tabletop group is three to six people, eight with guests, so twelve
# is roughly double a large table: it cannot squeeze out a voice that is really
# there, and it still bounds a noisy room to a speaker list a GM can read and
# rename. ``DIAR_MAX_SPEAKERS=0`` lifts the cap for a caller that would rather
# police it itself.
DEFAULT_MAX_SPEAKERS = _env_int("DIAR_MAX_SPEAKERS", 12)

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
    """Cosine similarity of two already-normalized vectors, of equal width.

    ``strict=True`` rather than a silent shortest-of-the-two: two embeddings of
    different width can only come from two different models, and truncating one
    against the other yields a number that looks like a similarity and is not.
    :meth:`SpeakerBank.resolve` already refuses such a vector at the door, so
    this never fires; it is here so that a future caller that skips that door
    gets an error instead of a plausible answer.
    """
    return sum(a * b for a, b in zip(left, right, strict=True))


@dataclass
class _Voice:
    """One remembered speaker: a running mean embedding and its weight."""

    centroid: list[float]
    count: int = 1

    def observe(self, embedding: Sequence[float]) -> None:
        """Fold one more observation of this voice into its centroid."""
        weight = min(self.count, _CENTROID_MEMORY)
        blended = [c * weight + e for c, e in zip(self.centroid, embedding, strict=True)]
        self.centroid = _normalize(blended)
        self.count += 1


class SpeakerBank:
    """The voices one session has heard, as centroids over their embeddings.

    :meth:`resolve` takes one embedding per cluster of one call (not one per
    segment) and answers with a stable speaker id for each. Matching is greedy
    from the best pair down and one-to-one: two clusters that the call itself
    decided were different people never collapse onto the same remembered
    voice.

    Not every embedding is worth the same, and ``reliable`` is how the caller
    says which is which. One pooled from a fifth of a second of audio is a
    guess: it may still recognise a voice this session knows, but it never
    opens a new one, never takes a label the cap forced on it, and never moves
    a centroid. ``_cluster_embeddings`` decides that per cluster.
    """

    def __init__(
        self,
        *,
        threshold: float = DEFAULT_THRESHOLD,
        max_voices: int = DEFAULT_MAX_SPEAKERS,
    ) -> None:
        self._threshold = threshold
        self._max_voices = max_voices
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
        reliable: Sequence[bool] | None = None,
    ) -> list[int | None]:
        """Speaker ids for this call's clusters, in the order given.

        Each cluster takes the most similar remembered voice above the
        threshold, opens a new one while the bank is under its cap, and
        otherwise is forced onto the nearest voice left. A forced match labels
        but does not update that voice, so a bank at its cap cannot be polluted
        by the speaker it had no room for.

        ``reliable`` is one flag per embedding, all true when it is omitted. A
        cluster marked false answers None instead of a speaker whenever nothing
        above the threshold recognised it. Unlabelled costs the caller the
        speaker of a fragment too short to identify anyone by; a new voice would
        have cost it an extra speaker in the session's list for the rest of the
        evening, and a forced one somebody else's name on those words.
        """
        vectors = [_normalize(embedding) for embedding in embeddings]
        self._check_widths(vectors)
        trusted = list(reliable) if reliable is not None else [True] * len(vectors)
        threshold = self._match_threshold(min_speakers)
        assigned = self._match(vectors, threshold)
        # Applied after matching, never during it, so every pair was scored
        # against the centroids this call started from.
        for index, speaker in assigned.items():
            if trusted[index]:
                self._voices[speaker].observe(vectors[index])
        taken = set(assigned.values())
        cap = self._cap(max_speakers)
        ids: list[int | None] = []
        for index, vector in enumerate(vectors):
            speaker = assigned.get(index)
            if speaker is None and trusted[index]:
                speaker = self._open(vector, taken, cap)
            ids.append(speaker)
        return ids

    def _cap(self, max_speakers: int | None) -> int | None:
        """How many voices this session may hold: the call's bound, else the default.

        None is uncapped, which is what ``DIAR_MAX_SPEAKERS=0`` asks for.
        """
        if max_speakers is not None:
            return max(1, max_speakers)
        return self._max_voices if self._max_voices > 0 else None

    def _check_widths(self, vectors: list[list[float]]) -> None:
        """Refuse an embedding this bank's voices cannot be compared with.

        Only the embedding model decides that width, so a mismatch means the
        model changed under a live session, which is a deployment fault rather
        than something one call can recover from. Failing loudly beats a
        truncated dot product that scores like a similarity and puts the wrong
        name on the words.
        """
        if not self._voices:
            return
        width = len(self._voices[0].centroid)
        wrong = next((len(vector) for vector in vectors if len(vector) != width), None)
        if wrong is not None:
            msg = f"embedding width {wrong} does not match this session's {width}"
            raise ValueError(msg)

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

    def _open(self, vector: Sequence[float], taken: set[int], cap: int | None) -> int:
        """A new speaker for ``vector``, or the nearest one the cap leaves free."""
        if cap is None or len(self._voices) < cap:
            self._voices.append(_Voice(centroid=list(vector)))
            speaker = len(self._voices) - 1
            # Marked taken like a matched voice, because it is one from here on:
            # a later cluster of this same call that the cap forces onto an
            # existing speaker must not be forced onto the voice opened here, or
            # two clusters the call itself called different people come back
            # under one label.
            taken.add(speaker)
            return speaker
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
        max_voices: int = DEFAULT_MAX_SPEAKERS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._threshold = threshold
        self._max_voices = max_voices
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
        reliable: Sequence[bool] | None = None,
    ) -> list[int | None]:
        """Speaker ids for one call's clusters, within ``session_id``'s bank."""
        with self._lock:
            self._evict_idle()
            entry = self._entries.pop(session_id, None)
            if entry is None:
                bank = SpeakerBank(threshold=self._threshold, max_voices=self._max_voices)
                entry = _Entry(bank=bank, last_used=0.0)
            entry.last_used = self._clock()
            self._entries[session_id] = entry  # re-inserted last: most recently used
            while len(self._entries) > self._max_sessions:
                del self._entries[next(iter(self._entries))]
            return entry.bank.resolve(
                embeddings,
                min_speakers=min_speakers,
                max_speakers=max_speakers,
                reliable=reliable,
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


# How long a failed model load is remembered before another attempt. A load
# fails for a reason no request can fix - the file is not mounted, the ONNX
# runtime will not open it - so retrying it per call means one doomed ONNX init
# per utterance of every session, on the box least able to spare the CPU, all to
# produce the same 503 each time. Long enough to stop that, short enough that a
# volume which mounts late is picked up without a restart.
_LOAD_RETRY_S = 60.0


class ModelCache:
    """The models this process has loaded, and the ones it has failed to load.

    One entry per key, loaded on first use behind a lock, because a load is
    slow ONNX initialisation and ``/diarize`` runs in Starlette's threadpool,
    where two requests can arrive at once.

    A *failure* is cached too, which is the part worth stating: without it a
    service whose model file is missing runs a fresh, doomed load for every
    request, and the answer is the same 503 either way. It is re-raised as it
    is until :data:`_LOAD_RETRY_S` has passed, after which one request tries
    again - so a volume that mounts late still recovers without a restart.
    """

    def __init__(
        self, *, retry_s: float = _LOAD_RETRY_S, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._retry_s = retry_s
        self._clock = clock
        self._loaded: dict[str, object] = {}
        self._failed: dict[str, tuple[float, Exception]] = {}
        self._lock = threading.Lock()

    def get(self, key: str, load: Callable[[], object]) -> object:
        """The model under ``key``, loading it once; raises what ``load`` raised."""
        cached = self._loaded.get(key)
        if cached is not None:
            return cached
        with self._lock:
            cached = self._loaded.get(key)
            if cached is not None:
                return cached
            failed = self._failed.get(key)
            if failed is not None and self._clock() - failed[0] < self._retry_s:
                raise failed[1]
            try:
                model = load()
            except (RuntimeError, ImportError) as exc:
                self._failed[key] = (self._clock(), exc)
                raise
            self._failed.pop(key, None)
            self._loaded[key] = model
            return model


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


@dataclass
class _ClusterVoice:
    """One call cluster's pooled embedding, and whether it is worth trusting."""

    vector: list[float]
    reliable: bool
    """False when the cluster had nothing longer than :data:`_MIN_EMBED_S`.

    Such a vector comes from a fifth of a second of audio and points more or
    less anywhere, so it may recognise a voice the session already knows but may
    not open a new one: see :meth:`SpeakerBank.resolve`.
    """


def _cluster_embeddings(
    extractor, audio, rate: int, spans: list[tuple[float, float, int]]
) -> dict[int, _ClusterVoice]:
    """One duration-weighted mean embedding per cluster id in ``spans``.

    Longest segments first, and at most :data:`_MAX_EMBED_SEGMENTS` of them per
    cluster: a whole-session call (which is how a re-processing job diarizes)
    can carry thousands of segments, and embedding every one of them would cost
    far more inference than the centroid gains.

    Segments under :data:`_MIN_EMBED_S` hold too little voice to place a
    speaker with. A cluster made only of those is still embedded from its
    longest segment, and marked unreliable: dropping it outright would throw
    away a match against a voice the session knows, and trusting it would let a
    0.2 s fragment mint a speaker that then lives for the rest of the session.

    A cluster whose segments all fell outside the audio has no embedding at all
    and is simply absent here; the caller leaves those segments unlabelled.
    """
    per_cluster: dict[int, list[tuple[float, float, float]]] = {}
    for start, end, cluster in spans:
        per_cluster.setdefault(cluster, []).append((end - start, start, end))
    pooled: dict[int, _ClusterVoice] = {}
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
        pooled[cluster] = _ClusterVoice(
            vector=[
                sum(duration * embedding[i] for duration, embedding in weighted) / total
                for i in range(len(weighted[0][1]))
            ],
            reliable=bool(usable),
        )
    return pooled


def create_app() -> FastAPI:
    """Build the sherpa-onnx diarization service app."""
    app = FastAPI(title="loreline-diarization")
    models = ModelCache()
    banks = SessionBanks()

    def _ensure_pipeline(num_clusters: int) -> tuple[object, object]:
        """Return the cached pipeline for ``num_clusters``, loading it once.

        Shared by ``/healthz`` and ``/diarize`` so both agree on what "ready"
        means: whatever ``_load_pipeline`` raises when the models are not
        configured or fail to load is exactly what fails a health check too,
        instead of ``/healthz`` keeping its own, separate notion of readiness
        that can drift from what a real diarize call actually does.
        """
        loaded = models.get(f"pipeline:{num_clusters}", lambda: _load_pipeline(num_clusters))
        return cast("tuple[object, object]", loaded)

    def _ensure_extractor() -> object:
        """Return the cached embedding extractor, loading it once."""
        return models.get("extractor", _load_extractor)

    @app.get("/healthz")
    def healthz() -> JSONResponse:
        # Plain `def`, not `async def`: the first call loads the models (real
        # file IO plus ONNX init), which is exactly the synchronous, possibly
        # slow work the comment on `/diarize` below already offloads to the
        # threadpool rather than run on the event loop.
        #
        # The extractor is loaded here as well as the pipeline, and it is a
        # second, separate model load reached only by a call that carries a
        # session_id. Left out, a service whose extractor cannot load reports
        # itself healthy while every live turn fails on it - and every call the
        # app makes carries a session id, so "can this serve the calls it will
        # get" is exactly the question this has to answer.
        try:
            _ensure_pipeline(-1)
            _ensure_extractor()
        except (RuntimeError, ImportError) as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        # session_memory says this build understands session_id rather than
        # accepting it and ignoring it, which is what the image before it did
        # and what no status code can distinguish. generation names this
        # process, so a caller can tell a restart from a fresh session.
        return JSONResponse({"status": "ok", "session_memory": True, "generation": GENERATION})

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
        # A cluster missing from `labels` is one this session could not place,
        # and its segments are left out rather than given a number that would
        # name somebody else. The caller's merge already handles words no
        # segment covers, which is exactly what those words become.
        segments = [
            {"start": start, "end": end, "speaker": f"Speaker {labels[cluster]}"}
            for start, end, cluster in spans
            if cluster in labels
        ]
        return JSONResponse({"segments": segments, "sample_rate": rate, "generation": GENERATION})

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
    """Cluster id -> label, resolved against what ``session_id`` has heard before.

    Only the clusters that produced an embedding appear in the answer. One that
    produced none - every segment of it fell outside the audio - is left out,
    and the rest of the call is still resolved against the session.

    Degrading the whole call to this call's own 0..k-1 numbering, which is what
    this used to do, is the one answer that must not be given: the caller reads
    these labels as the session's, so a Bob-only utterance would come back as
    "Speaker 0" while the session's Speaker 0 is Alice, and the transcript would
    put Bob's words under her name.
    """
    pooled = _cluster_embeddings(extractor, audio, rate, spans)
    clusters = [cluster for cluster in _local_labels(spans) if cluster in pooled]
    if not clusters:
        return {}
    speakers = banks.resolve(
        session_id,
        [pooled[cluster].vector for cluster in clusters],
        min_speakers=min_speakers,
        max_speakers=max_speakers,
        reliable=[pooled[cluster].reliable for cluster in clusters],
    )
    return {
        cluster: speaker
        for cluster, speaker in zip(clusters, speakers, strict=True)
        if speaker is not None
    }


app = create_app()
