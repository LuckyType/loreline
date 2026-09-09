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

The models live in a child process (see :class:`WorkerProcess`), and that, not
the semaphore below, is what keeps ``/healthz`` answering while a long
recording is being worked on: sherpa-onnx binds ``OfflineSpeakerDiarization``'s
``process`` without releasing the GIL, so for as long as one inference runs no
other Python thread of the process running it is scheduled - uvicorn's event
loop included. One diarization still runs at a time (see :class:`DiarizeSlot`):
a second one waits, and is answered ``429`` with a ``Retry-After`` if the wait
would be long, which costs nothing in throughput on a CPU service holding one
set of ONNX sessions.

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

import contextlib
import io
import math
import multiprocessing
import os
import secrets
import threading
import time
import wave
from collections.abc import AsyncIterator, Callable, Iterator, Sequence
from dataclasses import dataclass
from multiprocessing.connection import Connection
from multiprocessing.process import BaseProcess
from typing import Protocol, cast

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
#
# "This process" means the one serving HTTP, not the child holding the models
# (:class:`WorkerProcess`), and the split is on purpose. The child answers with
# one pooled embedding per cluster - a few hundred floats - and every decision
# made from those, the matching, the centroids, the TTL, the cap, ``DELETE``,
# stays here in code that no process boundary runs through. So the part of this
# service that is stateful and subtle is the part that did not move, a worker
# that dies costs a model reload rather than a session's speakers, and
# ``GENERATION``, which promises "the bank behind this answer is the one you saw
# last time", still names the process the bank is actually in.


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


# How long a caller waits for the one diarization slot before being turned
# away. Long enough that the live path queues rather than fails - it sends one
# turn at a time, a few seconds of audio each, and several can close together -
# and far shorter than a whole-session re-processing run, so a caller that
# arrives behind one of those is told to come back instead of spending its own
# (much longer, see ``loreline.diarization.remote``) timeout sitting in a line
# it cannot see the front of.
_QUEUE_WAIT_S = 30.0
# What a refused caller is told to come back after, in the Retry-After header.
# The same order as the wait it just spent, because what it is waiting for is
# the diarization that was already running when it arrived.
_RETRY_AFTER_S = 30


class DiarizeSlot:
    """One heavy diarization at a time, with a bounded queue in front of it.

    Deliberate rather than incidental. The service used to serialize by
    accident: the model ran on the request thread, so a second call simply
    waited. What was wanted from that accident is only the serialization - this
    is a CPU service with one set of ONNX sessions, so two inferences at once
    take twice as long each and finish no sooner.

    Read the previous version of this docstring for the mistake worth keeping a
    record of. It claimed that a semaphore of one was also what kept
    ``/healthz`` answering during a run, on the reasoning that both routes are
    plain ``def`` and so both run in Starlette's threadpool, and that a probe
    which does not take the semaphore therefore is not queued behind the audio.
    Measured against the built container, that is false: sherpa-onnx binds
    ``process`` without ``py::call_guard<py::gil_scoped_release>``, so the
    inference holds the GIL from beginning to end and *no* other thread of that
    process runs at all. The probe was not queued behind the semaphore, it was
    never scheduled. Thread-level concurrency cannot fix that, and only
    :class:`WorkerProcess`, which puts the inference somewhere that holding the
    GIL is nobody else's problem, does.

    So this class is now exactly one thing and does not pretend otherwise: the
    bound, and the 429. ``max_workers=1`` in a pool would serialize too, but it
    would do it by growing a queue nobody can see the front of; a caller has to
    be told, and this is where it is told.

    Queue *and* refuse, rather than one or the other. A caller that would be
    served within :data:`_QUEUE_WAIT_S` waits, because the live path sends one
    short turn per call and several turns can close at once, and refusing those
    would drop speaker labels for no reason. A caller that would wait longer is
    refused with 429 and a ``Retry-After``, because the alternative is a
    growing queue of requests whose callers have already timed out and gone
    away, each of which the service would still faithfully compute in full.

    What this cannot do is stop work that has already started; see
    :meth:`WorkerProcess.close` for what a child process does and does not
    change about that. Bounding who may start one is the containment that is
    actually available.

    A waiting caller does occupy one of Starlette's threadpool threads, which
    is why the wait is bounded and not merely long: the queue drains on its own
    within :data:`_QUEUE_WAIT_S` of the arrivals stopping, rather than growing
    for as long as somebody keeps pressing.
    """

    def __init__(self, *, wait_s: float | None = None) -> None:
        self._semaphore = threading.BoundedSemaphore(1)
        self._wait_s = _QUEUE_WAIT_S if wait_s is None else wait_s

    @contextlib.contextmanager
    def hold(self) -> Iterator[None]:
        """Hold the slot for the block, or raise 429 having waited for it."""
        if not self._semaphore.acquire(timeout=self._wait_s):
            raise HTTPException(
                status_code=429,
                detail=(
                    "another diarization is already running on this service, and this "
                    f"request waited {self._wait_s:.0f}s for it - retry once it finishes"
                ),
                headers={"Retry-After": str(_RETRY_AFTER_S)},
            )
        try:
            yield
        finally:
            self._semaphore.release()


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


def _read_wav(data: bytes):
    """Decode a mono 16-bit WAV into the float32 buffer the models take.

    The numpy array itself, never a Python list of samples, and that one word
    is a real bug that was here: ``.tolist()`` on a 37-minute session builds 35
    million Python floats, which costs over a gigabyte of objects and tens of
    seconds inside a single GIL-holding loop - during which this process
    answers nothing at all, ``/healthz`` included, before a frame of audio has
    reached a model. The caller turned the list straight back into an array,
    so the whole detour bought nothing but the stall.

    Unannotated on purpose, like ``_load_pipeline``: numpy is imported inside
    the function so this module still imports where the native wheels are
    absent, which is how the main project's lint and typecheck run it.
    """
    with wave.open(io.BytesIO(data), "rb") as wav:
        rate = wav.getframerate()
        frames = wav.readframes(wav.getnframes())
    import numpy as np  # noqa: PLC0415

    return np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0, rate


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
class ClusterVoice:
    """One call cluster's pooled embedding, and whether it is worth trusting.

    Public, unlike most of what surrounds it, because it is the shape a
    :class:`DiarizeOutcome` carries: this is what the process holding the
    models sends back to the process holding the speaker bank, so it is part of
    a seam rather than an internal detail of the embedding code.
    """

    vector: list[float]
    reliable: bool
    """False when the cluster had nothing longer than :data:`_MIN_EMBED_S`.

    Such a vector comes from a fifth of a second of audio and points more or
    less anywhere, so it may recognise a voice the session already knows but may
    not open a new one: see :meth:`SpeakerBank.resolve`.
    """


def _cluster_embeddings(
    extractor, audio, rate: int, spans: list[tuple[float, float, int]]
) -> dict[int, ClusterVoice]:
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
    pooled: dict[int, ClusterVoice] = {}
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
        pooled[cluster] = ClusterVoice(
            vector=[
                sum(duration * embedding[i] for duration, embedding in weighted) / total
                for i in range(len(weighted[0][1]))
            ],
            reliable=bool(usable),
        )
    return pooled


# ---------------------------------------------------------------------------
# Where the model work runs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DiarizeWork:
    """One call's audio, and the two decisions the models need taken for it."""

    payload: bytes
    """The uploaded WAV, still undecoded: decoding happens where the models are.

    A 36-minute session is about 69 MB as bytes and 138 MB as the float32 array
    it decodes to, so sending the smaller of the two halves what crosses a
    process boundary and leaves the larger entirely on the far side. Measured:
    a 69 MB round trip through a ``multiprocessing`` pipe costs 0.115 s,
    against a diarization of that same clip which the one timing this repo has
    puts at roughly its own length in wall clock (see ``_TIMEOUT_PER_AUDIO_S``
    in ``loreline.diarization.remote``). That is why nothing here reaches for
    shared memory or a temp file: the copy is real, and it is five thousandths
    of a percent of the call it is part of.
    """

    num_clusters: int
    """-1 for automatic clustering, or the exact count to force."""

    embeddings: bool
    """Whether one pooled embedding per cluster is wanted back.

    True exactly when the call carries a ``session_id``, since matching a
    session's bank is the only thing those embeddings are for.
    """


@dataclass(frozen=True)
class DiarizeOutcome:
    """Everything the models can say about one call, and nothing larger.

    Spans, one pooled embedding per cluster, and the sample rate read off the
    WAV. That is a few hundred floats however long the audio behind them was,
    which is the property that lets the speaker bank live on the other side of
    a process boundary from the models: the audio goes one way, and something
    the size of a log line comes back.
    """

    spans: list[tuple[float, float, int]]
    pooled: dict[int, ClusterVoice]
    rate: int


class ModelsUnavailableError(RuntimeError):
    """A model would not load: the 503 half of what can go wrong in a call.

    Named rather than inferred from where it was caught, because the two halves
    now sit on opposite sides of a process boundary and "which line raised it"
    is no longer available to the code choosing a status code. A load failure
    is a 503 that says why; a failure of the work itself is a 500, which is
    what an inference that raised on the request thread always was.

    A ``RuntimeError`` subclass so that code holding an :class:`InlineWorker`
    directly catches it with the same ``except (RuntimeError, ImportError)``
    that has always guarded a load.
    """


class WorkerUnavailableError(Exception):
    """The process holding the models could not be reached for this call.

    Two conditions with one answer, because a caller does the same thing about
    either: the child exited (a fresh one starts for the next call, holding no
    models), or it is busy with a call that is not this one. Neither says
    anything about whether the models are good, which is why
    :class:`Readiness` never files this as a verdict on them.
    """


class DiarizeWorker(Protocol):
    """Whatever holds the ONNX models and puts one call through them.

    Two implementations, differing only in where the work happens:
    :class:`InlineWorker` here, :class:`WorkerProcess` in a child. The service
    is written against this rather than against either, so a test can put
    something cheap where the models would be - or something deliberately
    unco-operative, which is what it takes to write an honest test for the
    defect this seam exists to fix.
    """

    def check_models(self) -> None:
        """Load both models if they are not loaded; raise if either will not.

        :class:`ModelsUnavailableError`, carrying the reason, which is what both
        routes put in their 503.
        """
        ...

    def diarize(self, work: DiarizeWork) -> DiarizeOutcome:
        """Segment, cluster, and where asked embed, one clip."""
        ...

    def close(self) -> None:
        """Release whatever this holds. Called once, when the service stops."""
        ...


class InlineWorker:
    """The models, in whichever process happens to be running this.

    The whole model side of a request and nothing else: the ONNX sessions, the
    WAV decode, the segmentation, the clustering, and the per-cluster
    embedding. It is what :class:`WorkerProcess` runs inside its child, and it
    is equally usable on its own, which is what most of the tests do - so they
    exercise this code rather than a stand-in for it, with no process boundary
    in the way to make them slow or flaky.

    Nothing about a *session* is in here on purpose (see the note under
    "Session speaker memory" at the top of this module): this answers with
    embeddings, and what a session makes of them is decided by the process
    serving HTTP, where the bank has always lived and still does.
    """

    def __init__(self) -> None:
        self._models = ModelCache()

    def check_models(self) -> None:
        """Load the pipeline and the standalone extractor, once each."""
        self._pipeline(-1)
        self._extractor()

    def diarize(self, work: DiarizeWork) -> DiarizeOutcome:
        """Put one clip through the models."""
        pipeline, np = self._pipeline(work.num_clusters)
        samples, rate = _read_wav(work.payload)
        audio = np.asarray(samples, dtype=np.float32)
        result = pipeline.process(audio).sort_by_start_time()
        spans = [(float(seg.start), float(seg.end), int(seg.speaker)) for seg in result]
        pooled = (
            _cluster_embeddings(self._extractor(), audio, rate, spans) if work.embeddings else {}
        )
        return DiarizeOutcome(spans=spans, pooled=pooled, rate=rate)

    def close(self) -> None:
        """Nothing to release: the ONNX sessions go when this process does."""

    def _pipeline(self, num_clusters: int) -> tuple[object, object]:
        loaded = self._load(f"pipeline:{num_clusters}", lambda: _load_pipeline(num_clusters))
        return cast("tuple[object, object]", loaded)

    def _extractor(self) -> object:
        return self._load("extractor", _load_extractor)

    def _load(self, key: str, load: Callable[[], object]) -> object:
        """A cached load, with whatever it failed with renamed to the 503 it becomes."""
        try:
            return self._models.get(key, load)
        except (RuntimeError, ImportError) as exc:
            raise ModelsUnavailableError(str(exc)) from exc


_WorkerBuild = Callable[[], DiarizeWorker]

# The pipe protocol, which is three tuples rather than exceptions on purpose: a
# traceback does not pickle, and a sherpa-onnx exception class need not even
# exist on the other side, while the two things a caller acts on - which half
# failed, and what to show a human - always cross intact.
#   request:  ("check", None) | ("diarize", work), and None to stop
#   answer:   ("ok", value) | ("unloadable", message) | ("raised", type, message)


def _serve(conn: Connection, build: _WorkerBuild) -> None:
    """A child's whole life: build the worker once, then answer one call at a time.

    Module level and named, because ``spawn`` pickles a target by reference:
    this function and ``build`` both have to be importable in a fresh
    interpreter, which is also why ``build`` is a class or a function rather
    than an already-built worker.
    """
    worker = build()
    try:
        while True:
            try:
                request = conn.recv()
            except EOFError:
                return  # the parent went away; there is nobody left to answer
            if request is None:
                return
            conn.send(_answer(worker, request))
    finally:
        worker.close()
        conn.close()


def _answer(worker: DiarizeWorker, request: tuple[str, DiarizeWork | None]) -> tuple[object, ...]:
    """One request run against the models, with every way it can fail made sendable."""
    operation, work = request
    try:
        if operation == "diarize":
            return ("ok", worker.diarize(cast("DiarizeWork", work)))
        worker.check_models()
    except ModelsUnavailableError as exc:
        return ("unloadable", str(exc))
    except Exception as exc:  # the child owes an answer whatever went wrong in it
        return ("raised", type(exc).__name__, str(exc))
    return ("ok", None)


def _unpack(answer: tuple[object, ...]) -> object:
    """A child's answer, as the value it carries or the exception it stands for."""
    if answer[0] == "ok":
        return answer[1]
    if answer[0] == "unloadable":
        raise ModelsUnavailableError(str(answer[1]))
    # A 500, exactly as an inference that raised on the request thread always
    # was. The child's exception class is named in the message because the name
    # is the only part of it that survives the pipe.
    msg = f"the diarization worker raised {answer[1]}: {answer[2]}"
    raise RuntimeError(msg)


# How long a readiness probe waits for its turn on the pipe before answering
# that it does not know. Short on purpose: its only caller is ``/healthz``,
# whose entire job is to answer promptly, and the one situation it covers - a
# probe arriving between a child being replaced and a diarization that outlived
# it finishing - clears on its own.
_READY_WAIT_S = 1.0
# How long the child is given to go after a SIGTERM before it is killed
# outright, and again before we stop waiting at all. Two of these fit inside
# uvicorn's ``--timeout-graceful-shutdown 5`` (see the Dockerfile), which is
# itself inside Docker's ten-second grace period.
_CHILD_EXIT_S = 1.0


class WorkerProcess:
    """The models, in a child process, because a thread was never going to be enough.

    The fix for a measured failure that survived its first fix. sherpa-onnx
    binds ``OfflineSpeakerDiarization.process`` through pybind11 *without*
    ``py::call_guard<py::gil_scoped_release>`` (unlike the embedding extractor
    next to it, which has one), so the call holds the GIL from beginning to
    end. Nothing else in that interpreter is scheduled meanwhile: not
    ``/healthz`` on another threadpool thread, not uvicorn's event loop. A 2 s
    health probe against a service diarizing a 36-minute recording timed out
    every time for the length of the recording, the app showed a healthy
    diarizer as *unreachable* throughout, and ``ReprocessManager.enqueue`` then
    refused the next diarize job with a message telling the operator to start a
    service that was running perfectly well.

    A process cannot hold another process's GIL. So the models sit in one, and
    this one talks to it down a pipe; what crosses is measured rather than
    assumed (see :class:`DiarizeWork`).

    Three things that look like the obvious answer, and why none of them is:

    - ``ProcessPoolExecutor(max_workers=1)``. Its worker is not a daemon, and
      ``concurrent.futures.process``'s ``atexit`` hook joins the executor's
      manager thread, which joins the worker - after sending it a sentinel it
      only reads once its current task is done. Measured: a parent that calls
      ``shutdown(wait=False, cancel_futures=True)`` against a 30 s task still
      takes 30 s to exit, and ``shutdown`` returns in 0.00 s while doing it, so
      nothing at the call site shows the wait. With a diarization in flight
      that is a ``docker stop`` ending in SIGKILL every time, which is the
      exact regression ``--timeout-graceful-shutdown 5`` was added to stop.
      Terminating the worker first means reaching for ``pool._processes``, a
      private attribute that ``shutdown`` then sets to None.
    - An ``initializer=`` that loads the models in the worker. An initializer
      that raises breaks the pool permanently, throwing away
      :class:`ModelCache`'s deliberate one-minute retry - which is what lets a
      volume mounted late be picked up without a restart. The models load
      lazily in the child instead, through that same cache.
    - More uvicorn workers. The speaker bank is process memory, and two of them
      behind one address answer one session with two sets of labels (ADR 0007).
      Half the health probes would land on the busy worker regardless.

    One child, started on first use rather than at import: ``spawn`` re-imports
    this module in the child, and this module builds an app at the bottom of
    it, so a child that started a child would not stop. It is ``daemon=True``,
    so a parent that dies without reaching :meth:`close` still takes it along -
    which also covers the one race :meth:`close` leaves open, where a call that
    was already starting a child finishes doing so just after the close.

    What that start costs, measured here: 0.32 s for the spawn, this module's
    import and a first round trip, then 0.1 ms per round trip after. The ONNX
    load is on top of that and is unchanged - the child loads the models the
    same way, once, through the same :class:`ModelCache`.

    Memory is close to a wash, reasoned rather than measured (the wheels are
    not installed where this is developed). One more interpreter is tens of
    megabytes; against that, this process stops importing numpy and
    onnxruntime, stops holding the two ONNX sessions, and stops holding the
    decoded audio, which for a 36-minute session is 138 MB. Both processes hold
    the uploaded bytes at once for the length of the send, and the child alone
    holds everything after.

    Note about the ``spawn`` import, because a change to the container's ``CMD``
    could disturb it: the child also re-runs the parent's ``__main__``, which is
    uvicorn's console script or ``uvicorn.__main__``. Both guard their body with
    ``if __name__ == "__main__"`` and the child runs them as ``__mp_main__``, so
    all that happens is an import. An entry point without that guard would start
    a second server in the child.

    A child that exits is noticed rather than waited on. This process closes
    its copy of the child's pipe end at start, so a dead child means ``EOF`` on
    the next receive instead of a wait for a writer that will never write; the
    next call starts a fresh one, and the caller is told
    (:class:`WorkerUnavailableError`) so it can forget what it believed about models
    the old child had loaded.
    """

    def __init__(
        self, build: _WorkerBuild = InlineWorker, *, ready_wait_s: float = _READY_WAIT_S
    ) -> None:
        self._build = build
        self._ready_wait_s = ready_wait_s
        # spawn, never fork. By the time a request arrives this process has
        # uvicorn's threadpool in it, and forking a multithreaded process is a
        # deadlock waiting for a lock the parent happened to hold - which
        # Python 3.12 already deprecates and 3.14 stops doing by default. It
        # costs little here: the child has nothing worth inheriting, and the
        # start is measured in the class docstring above.
        self._context = multiprocessing.get_context("spawn")
        self._pipe = threading.Lock()
        self._child: BaseProcess | None = None
        self._conn: Connection | None = None
        self._closed = False

    def check_models(self) -> None:
        """Have the child load both models, waiting only briefly for its ear."""
        self._call(("check", None), wait_s=self._ready_wait_s)

    def diarize(self, work: DiarizeWork) -> DiarizeOutcome:
        """Put one clip through the child's models, however long that takes."""
        return cast("DiarizeOutcome", self._call(("diarize", work), wait_s=None))

    def close(self) -> None:
        """End the child rather than wait for whatever it is inside.

        This is where a process turned out to be an improvement rather than
        only a fix. An inference has no cancellation point in it, so the
        previous arrangement could do nothing but leave the thread running one
        behind as a daemon thread and let the interpreter exit around it. A
        child can simply be sent a signal whose default disposition ends it,
        mid-native-call, at once.

        Not taking the pipe lock is deliberate: the caller here is a shutdown,
        and blocking it behind the diarization it is trying to end would be the
        fault it exists to avoid. An in-flight call sees its pipe close and
        raises :class:`WorkerUnavailableError`, which by then is the truth.

        What this still does not offer is cancelling one request while serving
        others, and nothing asks for it: the HTTP contract has no cancel, and
        killing the child costs every session's models a reload. It is now
        possible, where before it was not, and that is the whole claim.
        """
        self._closed = True
        self._discard()

    def _call(self, request: tuple[str, DiarizeWork | None], *, wait_s: float | None) -> object:
        """Send one request and wait for its answer, holding the pipe throughout.

        One conversation at a time, because there is one pipe: two interleaved
        would hand each caller the other's answer, which is the kind of fault
        that looks like a model behaving strangely.
        """
        if not self._pipe.acquire(timeout=-1 if wait_s is None else wait_s):
            msg = "the process holding the diarization models is busy with another call"
            raise WorkerUnavailableError(msg)
        try:
            conn = self._living_child()
            try:
                conn.send(request)
                answer = cast("tuple[object, ...]", conn.recv())
            except (EOFError, OSError) as exc:
                self._discard()
                msg = "the process holding the diarization models exited before it answered"
                raise WorkerUnavailableError(msg) from exc
        finally:
            self._pipe.release()
        return _unpack(answer)

    def _living_child(self) -> Connection:
        """The pipe to a running child, starting one when there is not one."""
        if self._closed:
            msg = "the diarization service is shutting down"
            raise WorkerUnavailableError(msg)
        child, conn = self._child, self._conn
        if child is not None and conn is not None and child.is_alive():
            return conn
        self._discard()
        parent_end, child_end = self._context.Pipe(duplex=True)
        child = self._context.Process(
            target=_serve, args=(child_end, self._build), name="diarization-models", daemon=True
        )
        child.start()
        # Closed here so this process is not itself a writer on the child's
        # end: left open, a child that died would leave a receive waiting for
        # ever rather than raising EOFError.
        child_end.close()
        self._child, self._conn = child, parent_end
        return parent_end

    def _discard(self) -> None:
        """Forget the current child, ending it first if it is still running."""
        child, conn = self._child, self._conn
        self._child, self._conn = None, None
        if conn is not None:
            with contextlib.suppress(OSError):
                conn.close()
        if child is None:
            return
        if child.is_alive():
            child.terminate()
            child.join(_CHILD_EXIT_S)
        if child.is_alive():
            child.kill()
            child.join(_CHILD_EXIT_S)
        with contextlib.suppress(ValueError):
            child.close()


class Readiness:
    """What the serving process knows about models it can no longer see for itself.

    The models moved into a child (:class:`WorkerProcess`), and asking a child
    anything while it is inside an inference means waiting out the inference,
    which is the exact failure the move exists to remove. So ``/healthz`` has
    to answer from something held on this side, and this is that something.

    Remembering a *yes* with no expiry is faithful rather than convenient: the
    child's :class:`ModelCache` never unloads a model it has loaded, so "both
    of these load" is a fact that, once true, stays true for as long as that
    child lives. :meth:`forget` covers the single way it stops being true,
    which is a child that died and will be replaced by one holding nothing.

    A *no* is remembered for :data:`_LOAD_RETRY_S`, for the reason
    ``ModelCache`` remembers one: a load fails for something no poll can fix,
    and the app polls this endpoint continuously. It is that same window, so a
    volume that mounts late is picked up here when it is picked up there.

    The invariant that makes this safe is worth stating outright, because it is
    what stops a health probe ever reaching a busy child. A diarization only
    starts after a verdict of ready, and ready is sticky, so while one runs
    every probe is answered from here without a word to the child. The only
    calls that do reach it are a cold start, one retry a minute against a
    service that is failing anyway, and the moment just after a child was
    replaced - and that last one is why
    :meth:`WorkerProcess.check_models` waits :data:`_READY_WAIT_S` for its turn
    rather than however long an inference takes.
    """

    def __init__(
        self, *, retry_s: float = _LOAD_RETRY_S, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._retry_s = retry_s
        self._clock = clock
        self._ready = False
        self._failure: tuple[float, Exception] | None = None
        self._lock = threading.Lock()

    def confirm(self, ask: Callable[[], None]) -> None:
        """Return if the models are known good, and otherwise ask, once."""
        if self._ready:
            return
        with self._lock:
            if self._ready:
                return
            failure = self._failure
            if failure is not None and self._clock() - failure[0] < self._retry_s:
                raise failure[1]
            try:
                ask()
            except (RuntimeError, ImportError) as exc:
                self._failure = (self._clock(), exc)
                raise
            self._failure = None
            self._ready = True

    def forget(self) -> None:
        """Unlearn all of it: the process holding those models is gone."""
        with self._lock:
            self._ready = False
            self._failure = None


def create_app(worker: DiarizeWorker | None = None) -> FastAPI:
    """Build the sherpa-onnx diarization service app.

    ``worker`` says where the model work happens and defaults to the child
    process a deployment wants. A test passes an :class:`InlineWorker` to run
    the same code here instead, or something of its own to stand in for a child
    that will not answer.
    """
    models_worker = worker if worker is not None else WorkerProcess()
    readiness = Readiness()
    banks = SessionBanks()
    # Per app rather than per process, so a test building two services gets two
    # of these; a deployment runs one app in one container either way.
    slot = DiarizeSlot()

    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        """End the model worker when the server ends.

        Shutdown only: the child starts itself on first use, which is also what
        lets a test drive this app over ASGI without a lifespan at all. uvicorn
        runs this once its graceful-shutdown budget is spent, so the child is
        killed within about a second of that, comfortably inside Docker's own
        grace period.
        """
        yield
        models_worker.close()

    app = FastAPI(title="loreline-diarization", lifespan=lifespan)

    def _confirm_models() -> None:
        """Readiness as both routes need it, with each failure given its 503.

        Shared by ``/healthz`` and ``/diarize`` so both agree on what "ready"
        means: whatever the models fail to load with is exactly what fails a
        health check too, instead of ``/healthz`` keeping a separate notion of
        readiness that can drift from what a real diarize call does.

        Both models, always, including for a call carrying no ``session_id``
        that will never touch the extractor. ``/healthz`` has graded a service
        with a broken extractor unhealthy since it learned to look, on the
        grounds that every call Loreline makes carries a session id; letting
        ``/diarize`` disagree about the same service would be exactly the drift
        the paragraph above is about.
        """
        try:
            readiness.confirm(models_worker.check_models)
        except WorkerUnavailableError as exc:
            # Not a verdict on the models, so nothing about them is recorded:
            # whatever the old child had loaded went with it.
            readiness.forget()
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except (RuntimeError, ImportError) as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    def _run(work: DiarizeWork) -> DiarizeOutcome:
        """One call through the worker, with a child that is gone turned into a 503."""
        try:
            return models_worker.diarize(work)
        except WorkerUnavailableError as exc:
            readiness.forget()
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except ModelsUnavailableError as exc:
            # The one load that can still happen this side of the readiness
            # check is a pipeline pinned to an exact cluster count, which only
            # a caller sending min == max and no session ever asks for.
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.get("/healthz")
    def healthz() -> JSONResponse:
        # Plain `def`, not `async def`: on a cold service this is the call that
        # loads the models (real file IO plus ONNX init), and waiting for that
        # on the event loop would stall everything else this process is doing.
        #
        # It does not take the diarization slot. More to the point, and this is
        # what the attempt before this one got wrong, it does not speak to the
        # model worker either while that worker is busy: readiness is
        # remembered on this side (see `Readiness`), so a probe arriving during
        # a 36-minute run is answered out of this process's own memory in
        # microseconds. A probe that queues behind the audio reports the
        # service down for precisely as long as it is busiest, and it makes no
        # difference to the app's red badge whether it queued on a semaphore,
        # on a pipe, or on the GIL.
        #
        # Both models are covered, not just the pipeline: the extractor is a
        # second, separate load reached only by a call carrying a session_id,
        # and every call the app makes carries one. Left out, a service whose
        # extractor cannot load reports itself healthy while every live turn
        # fails on it.
        _confirm_models()
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
        """Diarize one clip, one at a time, in a process that is not this one.

        A plain ``def`` route, so Starlette runs the body in its threadpool
        rather than on the loop. That was already true when this service wedged
        for four minutes under a single long diarization, and it is worth being
        exact about what the threadpool does and does not buy, because it was
        over-credited twice. It keeps the loop free of *Python* that takes a
        while. It did not keep it free of decoding the audio while that built a
        Python list of every sample (see ``_read_wav``), it did not bound how
        many inferences ran at once, and it does not keep the loop free of an
        inference at all: the binding holds the GIL for the length of one, so a
        threadpool thread inside it is a stalled server. :class:`DiarizeSlot`
        is the bound; :class:`WorkerProcess` is what puts the GIL-holding call
        somewhere it is nobody else's problem.

        The slot is taken *after* the readiness check, so a misconfigured
        service still answers 503 immediately rather than queueing behind
        somebody else's audio to say the same thing - and that check is
        answered from this process's memory, so it cannot itself queue.

        The labels are worked out inside the slot rather than after it. They
        are microseconds of arithmetic either way, but the slot is what orders
        two calls of one session against each other, and a bank updated in the
        other order would fold this utterance's voices into the wrong
        centroids.
        """
        num_clusters = -1 if session_id else _resolve_num_clusters(min_speakers, max_speakers)
        _confirm_models()
        work = DiarizeWork(
            payload=file.file.read(),
            num_clusters=num_clusters,
            embeddings=session_id is not None,
        )
        with slot.hold():
            outcome = _run(work)
            if session_id:
                labels = _session_labels(banks, session_id, outcome, min_speakers, max_speakers)
            else:
                labels = _local_labels(outcome.spans)
        # A cluster missing from `labels` is one this session could not place,
        # and its segments are left out rather than given a number that would
        # name somebody else. The caller's merge already handles words no
        # segment covers, which is exactly what those words become.
        segments = [
            {"start": start, "end": end, "speaker": f"Speaker {labels[cluster]}"}
            for start, end, cluster in outcome.spans
            if cluster in labels
        ]
        return JSONResponse(
            {"segments": segments, "sample_rate": outcome.rate, "generation": GENERATION}
        )

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
    outcome: DiarizeOutcome,
    min_speakers: int | None,
    max_speakers: int | None,
) -> dict[int, int]:
    """Cluster id -> label, resolved against what ``session_id`` has heard before.

    Takes a :class:`DiarizeOutcome` rather than an extractor and a buffer of
    audio, which is the whole of what moving the models into a child process
    changed on this side: the embedding happened somewhere else, and everything
    from here down - the matching, the centroids, the caps - is the same code
    running in the same place it always did, on a few hundred floats.

    Only the clusters that produced an embedding appear in the answer. One that
    produced none - every segment of it fell outside the audio - is left out,
    and the rest of the call is still resolved against the session.

    Degrading the whole call to this call's own 0..k-1 numbering, which is what
    this used to do, is the one answer that must not be given: the caller reads
    these labels as the session's, so a Bob-only utterance would come back as
    "Speaker 0" while the session's Speaker 0 is Alice, and the transcript would
    put Bob's words under her name.
    """
    pooled, spans = outcome.pooled, outcome.spans
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
