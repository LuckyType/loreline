"""Tests for services/diarization/app.py's own /healthz readiness gate.

This is the small, separate sherpa-onnx-backed diarization service bundled in
this repo and run as its own Docker container (see ``services/diarization``),
not the mock at ``mocks/diarization.py`` that ``test_diarization.py`` in this
same directory uses to exercise ``RemoteDiarizer`` end to end.

sherpa-onnx is a native wheel and, like numpy, is never installed for these
tests (kept out of base CI on purpose, see ``services/diarization/app.py``'s
own module docstring), so the "not ready" cases below run the service's real
``_load_pipeline`` and let its own import failure prove the 503, while the
"loaded" case stubs it to check the readiness gate itself rather than
sherpa-onnx's model loading.
"""

from __future__ import annotations

import asyncio
import contextlib
import multiprocessing
import os
import threading
import time
from collections.abc import Generator

import httpx
import pytest

import services.diarization.app as diarization_app


def _transport(worker: diarization_app.DiarizeWorker | None = None) -> httpx.ASGITransport:
    """A transport onto a fresh service app, so no test shares another's cache.

    An ``InlineWorker`` unless a test says otherwise, and that is the same
    class the service runs inside its child process - just running here, where
    a monkeypatched ``_load_pipeline`` can reach it and where no test pays for
    a ``spawn``. The process boundary has tests of its own further down; what
    these want is the service's own logic, not a second interpreter.
    """
    return httpx.ASGITransport(
        app=diarization_app.create_app(worker or diarization_app.InlineWorker())
    )


async def test_healthz_503_before_models_are_ready() -> None:
    """The bug this guards: /healthz used to answer 200 in this exact state.

    No monkeypatching: DIAR_SEGMENTATION_MODEL/DIAR_EMBEDDING_MODEL are unset
    in the test process and sherpa-onnx is not installed, so this drives the
    service's real ``_load_pipeline`` failure, the same one a misconfigured
    deployment hits.
    """
    async with httpx.AsyncClient(transport=_transport(), base_url="http://diar") as client:
        response = await client.get("/healthz")

    assert response.status_code == 503
    assert "detail" in response.json()


async def test_healthz_matches_diarize_failure_shape() -> None:
    """/healthz's 503 must read exactly like /diarize's documented one.

    Before this fix only /diarize could ever answer 503; now both endpoints
    share the same readiness check, so a failure looks identical either way.
    """
    async with httpx.AsyncClient(transport=_transport(), base_url="http://diar") as client:
        healthz_response = await client.get("/healthz")
        diarize_response = await client.post(
            "/diarize", files={"file": ("audio.wav", b"", "audio/wav")}
        )

    assert healthz_response.status_code == 503
    assert diarize_response.status_code == 503
    assert healthz_response.json() == diarize_response.json()


async def test_healthz_503_reports_the_unconfigured_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The exact real-world incident: models never configured on a live box.

    Stubs ``_load_pipeline`` to raise precisely what it raises for unset env
    vars, independent of whether sherpa-onnx happens to be installed, so this
    documents and locks in the specific failure this bug report described.
    """

    def unconfigured(num_clusters: int = -1) -> tuple[object, object]:
        _ = num_clusters
        msg = "DIAR_SEGMENTATION_MODEL and DIAR_EMBEDDING_MODEL must be set"
        raise RuntimeError(msg)

    monkeypatch.setattr(diarization_app, "_load_pipeline", unconfigured)

    async with httpx.AsyncClient(transport=_transport(), base_url="http://diar") as client:
        response = await client.get("/healthz")

    assert response.status_code == 503
    assert response.json() == {
        "detail": "DIAR_SEGMENTATION_MODEL and DIAR_EMBEDDING_MODEL must be set"
    }


async def test_healthz_ok_once_models_load_and_caches_the_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Once ready, /healthz reports it, and does not reload on every poll.

    Both models: the embedding extractor is a second, separate load that only a
    call carrying a session_id reaches, and every call Loreline makes carries
    one, so health has to cover it too.
    """
    calls: list[int] = []
    extractors: list[int] = []

    def fake_load_pipeline(num_clusters: int = -1) -> tuple[object, object]:
        calls.append(num_clusters)
        return object(), object()

    def fake_load_extractor() -> object:
        extractors.append(1)
        return object()

    monkeypatch.setattr(diarization_app, "_load_pipeline", fake_load_pipeline)
    monkeypatch.setattr(diarization_app, "_load_extractor", fake_load_extractor)

    async with httpx.AsyncClient(transport=_transport(), base_url="http://diar") as client:
        first = await client.get("/healthz")
        second = await client.get("/healthz")

    assert first.status_code == 200
    assert first.json()["status"] == "ok"
    assert first.json()["session_memory"] is True
    assert second.status_code == 200
    assert calls == [-1]  # loaded once; the second poll reused the cached pipeline
    assert extractors == [1]


async def test_healthz_503_when_only_the_embedding_extractor_is_broken(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A working pipeline and a broken extractor used to read as healthy.

    That is the shape a real deployment fails in: the segmentation model is
    mounted and the embedding one is not, so /healthz answered 200 while every
    live turn - all of which carry a session id - failed on the second model.
    """

    def fake_load_extractor() -> object:
        msg = "DIAR_EMBEDDING_MODEL must be set"
        raise RuntimeError(msg)

    monkeypatch.setattr(
        diarization_app, "_load_pipeline", lambda num_clusters=-1: (object(), object())
    )
    monkeypatch.setattr(diarization_app, "_load_extractor", fake_load_extractor)

    async with httpx.AsyncClient(transport=_transport(), base_url="http://diar") as client:
        response = await client.get("/healthz")

    assert response.status_code == 503
    assert response.json() == {"detail": "DIAR_EMBEDDING_MODEL must be set"}


async def test_a_failed_load_is_not_attempted_again_by_every_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A doomed ONNX init per poll costs real CPU to answer the same 503."""
    attempts: list[int] = []

    def fake_load_extractor() -> object:
        attempts.append(1)
        msg = "DIAR_EMBEDDING_MODEL must be set"
        raise RuntimeError(msg)

    monkeypatch.setattr(
        diarization_app, "_load_pipeline", lambda num_clusters=-1: (object(), object())
    )
    monkeypatch.setattr(diarization_app, "_load_extractor", fake_load_extractor)

    async with httpx.AsyncClient(transport=_transport(), base_url="http://diar") as client:
        first = await client.get("/healthz")
        second = await client.get("/healthz")
        third = await client.get("/healthz")

    assert [first.status_code, second.status_code, third.status_code] == [503, 503, 503]
    assert first.json() == third.json()  # the same failure, reported the same way
    assert attempts == [1]


def test_a_failed_load_is_retried_once_its_window_has_passed() -> None:
    """Bounded, not abandoned: a volume that mounts late recovers on its own.

    Driven directly rather than through a request, because the point is the
    clock: the service's own cache is built with the real one.
    """
    now = [1000.0]
    attempts: list[int] = []

    def failing() -> object:
        attempts.append(1)
        msg = "not mounted yet"
        raise RuntimeError(msg)

    cache = diarization_app.ModelCache(retry_s=60.0, clock=lambda: now[0])
    for _ in range(3):
        with pytest.raises(RuntimeError):
            cache.get("model", failing)
    assert attempts == [1]  # two of the three were answered from the cached failure

    now[0] += 61.0
    with pytest.raises(RuntimeError):
        cache.get("model", failing)
    assert attempts == [1, 1]

    now[0] += 61.0
    assert cache.get("model", lambda: "loaded") == "loaded"  # a fixed mount is picked up


# ---------------------------------------------------------------------------
# Session memory, over the HTTP contract
# ---------------------------------------------------------------------------
# Neither sherpa-onnx nor numpy is installed for these tests, so the models are
# stubbed by a fake that is self-consistent rather than canned: the request body
# names one synthetic voice per second ("1,2,1"), the fake pipeline segments the
# audio into runs of one voice and clusters those runs, and the fake extractor
# embeds a span as the one-hot vector of the voice in it. What is left running
# is the service's own code: the form field, the pooling per cluster, the bank
# and the labels that come back.

_RATE = 16000
# The voice the fake pipeline reports outside the audio it was given, which is
# how a cluster with no embedding at all arises for real: the segmentation
# model places a turn the caller's audio does not cover, and the service's own
# ``_embed`` answers None for it. What the service does with the *rest* of such
# a call is what those tests are about.
_UNPLACEABLE = 9.0


def _fake_read_wav(data: bytes) -> tuple[list[float], int]:
    """One second of audio per voice named in the body: ``b"1,2"``.

    A voice can give its own length instead: ``b"1@0.2,2"`` is a fifth of a
    second of voice 1 followed by a second of voice 2, which is how a cluster
    with nothing long enough to embed reliably is written here.
    """
    samples: list[float] = []
    for token in data.decode().split(","):
        voice, _, seconds = token.partition("@")
        samples.extend([float(voice)] * int(float(seconds or 1.0) * _RATE))
    return samples, _RATE


class _FakeNumpy:
    float32 = "float32"

    @staticmethod
    def asarray(values: list[float], dtype: object = None) -> list[float]:
        """What the service calls on an already-decoded buffer.

        ``asarray`` rather than ``array``: the real ``_read_wav`` hands back a
        float32 array already, and copying a whole session of audio a second
        time is exactly the kind of waste that made this service unreachable
        while it worked (see its ``_read_wav`` docstring).
        """
        _ = dtype
        return list(values)


class _FakeSegment:
    def __init__(self, start: float, end: float, speaker: int) -> None:
        self.start = start
        self.end = end
        self.speaker = speaker


class _FakeResult(list[_FakeSegment]):
    def sort_by_start_time(self) -> _FakeResult:
        return self


class _FakePipeline:
    """Cuts the audio into runs of one voice, one cluster id per distinct voice.

    Voice :data:`_UNPLACEABLE` is the one exception: its run is reported past
    the end of the audio, so the service embeds an empty slice for it and gets
    nothing back, exactly as it does for a real segment that falls outside what
    the caller sent.
    """

    def process(self, audio: list[float]) -> _FakeResult:
        result = _FakeResult()
        clusters: dict[float, int] = {}
        start = 0
        for index in range(1, len(audio) + 1):
            if index < len(audio) and audio[index] == audio[start]:
                continue
            voice = audio[start]
            cluster = clusters.setdefault(voice, len(clusters))
            offset = len(audio) / _RATE if voice == _UNPLACEABLE else 0.0
            result.append(_FakeSegment(start / _RATE + offset, index / _RATE + offset, cluster))
            start = index
        return result


class _FakeStream:
    def __init__(self) -> None:
        self.chunk: list[float] = []

    def accept_waveform(self, *, sample_rate: int, waveform: list[float]) -> None:
        _ = sample_rate
        self.chunk = waveform

    def input_finished(self) -> None:
        return None


class _FakeExtractor:
    def create_stream(self) -> _FakeStream:
        return _FakeStream()

    def compute(self, stream: _FakeStream) -> list[float]:
        voice = int(stream.chunk[0])
        return [1.0 if index == voice else 0.0 for index in range(8)]


def _stub_models(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(diarization_app, "_read_wav", _fake_read_wav)
    monkeypatch.setattr(
        diarization_app, "_load_pipeline", lambda num_clusters=-1: (_FakePipeline(), _FakeNumpy())
    )
    monkeypatch.setattr(diarization_app, "_load_extractor", _FakeExtractor)


async def _speakers(client: httpx.AsyncClient, voices: str, **form: str) -> list[str]:
    response = await client.post(
        "/diarize",
        data={"sample_rate": str(_RATE), **form},
        files={"file": ("audio.wav", voices.encode(), "audio/wav")},
    )
    assert response.status_code == 200
    return [segment["speaker"] for segment in response.json()["segments"]]


async def test_a_session_keeps_one_label_per_voice_across_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The defect this fixes, at the level the service can see it.

    Three utterances, two voices, one at a time: without a session every one of
    them is "Speaker 0" (see the test below), which is what made a whole
    table's transcript read as one person.
    """
    _stub_models(monkeypatch)
    async with httpx.AsyncClient(transport=_transport(), base_url="http://diar") as client:
        assert await _speakers(client, "1", session_id="s1") == ["Speaker 0"]
        assert await _speakers(client, "2", session_id="s1") == ["Speaker 1"]
        assert await _speakers(client, "1", session_id="s1") == ["Speaker 0"]
        # Both voices in one utterance, and both keep the labels they had.
        assert await _speakers(client, "2,1", session_id="s1") == ["Speaker 1", "Speaker 0"]


async def test_without_a_session_labels_stay_per_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """The old behavior, unchanged for a caller that sends no session."""
    _stub_models(monkeypatch)
    async with httpx.AsyncClient(transport=_transport(), base_url="http://diar") as client:
        assert await _speakers(client, "1") == ["Speaker 0"]
        assert await _speakers(client, "2") == ["Speaker 0"]
        assert await _speakers(client, "2,1") == ["Speaker 0", "Speaker 1"]


async def test_sessions_are_independent(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_models(monkeypatch)
    async with httpx.AsyncClient(transport=_transport(), base_url="http://diar") as client:
        assert await _speakers(client, "1", session_id="s1") == ["Speaker 0"]
        assert await _speakers(client, "2", session_id="s2") == ["Speaker 0"]
        assert await _speakers(client, "2", session_id="s1") == ["Speaker 1"]


async def test_deleting_a_session_forgets_its_speakers(monkeypatch: pytest.MonkeyPatch) -> None:
    """What a capture's end sends, and what an unknown id answers."""
    _stub_models(monkeypatch)
    async with httpx.AsyncClient(transport=_transport(), base_url="http://diar") as client:
        await _speakers(client, "1", session_id="s1")
        assert await _speakers(client, "2", session_id="s1") == ["Speaker 1"]

        deleted = await client.delete("/sessions/s1")
        assert deleted.status_code == 200
        assert deleted.json() == {"deleted": True}
        unknown = await client.delete("/sessions/never-seen")
        assert unknown.status_code == 200
        assert unknown.json() == {"deleted": False}

        assert await _speakers(client, "2", session_id="s1") == ["Speaker 0"]


async def test_max_speakers_caps_a_session(monkeypatch: pytest.MonkeyPatch) -> None:
    """The bound applies to the session, not to the seconds in one request."""
    _stub_models(monkeypatch)
    async with httpx.AsyncClient(transport=_transport(), base_url="http://diar") as client:
        form = {"max_speakers": "2"}
        assert await _speakers(client, "1", session_id="s1", **form) == ["Speaker 0"]
        assert await _speakers(client, "2", session_id="s1", **form) == ["Speaker 1"]
        assert await _speakers(client, "3", session_id="s1", **form) == ["Speaker 0"]


async def test_a_session_call_clusters_automatically(monkeypatch: pytest.MonkeyPatch) -> None:
    """An exact bound describes the session, so it must not force a per-call count.

    One utterance holding fewer voices than the table is the normal case, and
    sherpa-onnx asked for exactly two clusters would split the one voice in it.
    """
    loaded: list[int] = []

    def record(num_clusters: int = -1) -> tuple[object, object]:
        loaded.append(num_clusters)
        return _FakePipeline(), _FakeNumpy()

    _stub_models(monkeypatch)
    monkeypatch.setattr(diarization_app, "_load_pipeline", record)
    async with httpx.AsyncClient(transport=_transport(), base_url="http://diar") as client:
        form = {"min_speakers": "2", "max_speakers": "2"}
        assert await _speakers(client, "1", session_id="s1", **form) == ["Speaker 0"]
        assert await _speakers(client, "1,2", **form) == ["Speaker 0", "Speaker 1"]

    assert loaded == [-1, 2]  # the session call clusters automatically, the stateless one does not


async def test_a_fragment_too_short_to_embed_never_opens_a_speaker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """It may recognise a voice the session knows; it may not mint one.

    The live path sends neither bound, so nothing but this stops a fifth of a
    second of chair-scrape from becoming a speaker that lives for the rest of
    the evening and turns up in the rename list as a person nobody remembers.
    """
    _stub_models(monkeypatch)
    async with httpx.AsyncClient(transport=_transport(), base_url="http://diar") as client:
        assert await _speakers(client, "1", session_id="s1") == ["Speaker 0"]
        # A fifth of a second of the voice it already knows: matched, not minted.
        assert await _speakers(client, "1@0.2", session_id="s1") == ["Speaker 0"]
        # A fifth of a second of somebody else: no label at all, and no new voice.
        assert await _speakers(client, "2@0.2", session_id="s1") == []
        # Which the next full second of that voice proves: it is Speaker 1, so
        # nothing was opened for it in between.
        assert await _speakers(client, "2", session_id="s1") == ["Speaker 1"]


async def test_a_cluster_with_no_embedding_does_not_renumber_the_rest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bug: one unplaceable cluster sent the whole call back to 0..k-1.

    A session that knows two voices then answered "Speaker 0" for a call
    holding only its second one, and the transcript filed Bob's words under
    Alice's name - the exact failure session memory exists to prevent.
    """
    _stub_models(monkeypatch)
    async with httpx.AsyncClient(transport=_transport(), base_url="http://diar") as client:
        assert await _speakers(client, "1", session_id="s1") == ["Speaker 0"]
        assert await _speakers(client, "2", session_id="s1") == ["Speaker 1"]
        # Voice 9 is the cluster placed outside the audio, so it has no
        # embedding at all; voice 2 is the one the session already calls
        # Speaker 1, and it stays Speaker 1.
        assert await _speakers(client, "9,2", session_id="s1") == ["Speaker 1"]


async def test_a_stateless_call_still_numbers_from_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    """A caller with no session is unaffected by any of the above."""
    _stub_models(monkeypatch)
    async with httpx.AsyncClient(transport=_transport(), base_url="http://diar") as client:
        assert await _speakers(client, "9,2") == ["Speaker 0", "Speaker 1"]


async def test_every_answer_carries_this_process_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The only way a caller can see that a restart renumbered its session.

    The bank is process memory, so the turn after a restart is Speaker 0 again
    and one label ends up naming two people. Nothing in the labels says so -
    "Speaker 0" is what a healthy service answers too - hence a value that
    changes with the process.
    """
    _stub_models(monkeypatch)
    async with httpx.AsyncClient(transport=_transport(), base_url="http://diar") as client:
        health = await client.get("/healthz")
        diarized = await client.post(
            "/diarize",
            data={"sample_rate": str(_RATE), "session_id": "s1"},
            files={"file": ("audio.wav", b"1", "audio/wav")},
        )

    assert diarization_app.GENERATION  # a value, not an empty string
    assert health.json()["generation"] == diarization_app.GENERATION
    assert diarized.json()["generation"] == diarization_app.GENERATION
    # The capability flag rides on the same answer: an older image accepts the
    # session_id form field and ignores it, which no status code can show.
    assert health.json()["session_memory"] is True


# ---------------------------------------------------------------------------
# One diarization at a time, and a service that keeps answering during one
# ---------------------------------------------------------------------------
# The incident these guard: a diarization the caller had already given up on
# went on running, and while it did the service answered nothing at all - the
# 2 s health probe included, so the app's badge went red and every further
# press queued more work behind the pile. The model is faked with something
# that simply stalls, since what is under test is the service's own
# concurrency, not sherpa-onnx.

# A stalled call gives up on its own rather than hanging the suite, so a
# regression fails the test instead of the run.
_STALL_LIMIT_S = 5.0


class _StallingPipeline:
    """Stands in for minutes of ONNX inference, and counts its own overlap.

    ``entered`` fires as soon as a call is inside ``process``; ``release`` lets
    every waiting call finish. ``peak`` is how many were inside at once, which
    is the property :class:`~services.diarization.app.DiarizeSlot` exists to
    hold at one.
    """

    def __init__(self, entered: threading.Event, release: threading.Event) -> None:
        self._entered = entered
        self._release = release
        self._lock = threading.Lock()
        self._inside = 0
        self.peak = 0
        self.calls = 0

    def process(self, audio: list[float]) -> _FakeResult:
        with self._lock:
            self._inside += 1
            self.calls += 1
            self.peak = max(self.peak, self._inside)
        self._entered.set()
        self._release.wait(_STALL_LIMIT_S)
        with self._lock:
            self._inside -= 1
        return _FakePipeline().process(audio)


def _stall_the_model(
    monkeypatch: pytest.MonkeyPatch, entered: threading.Event, release: threading.Event
) -> _StallingPipeline:
    """Stub the models, with a pipeline that stays inside ``process``."""
    _stub_models(monkeypatch)
    pipeline = _StallingPipeline(entered, release)
    monkeypatch.setattr(
        diarization_app, "_load_pipeline", lambda num_clusters=-1: (pipeline, _FakeNumpy())
    )
    return pipeline


def _post_diarize(client: httpx.AsyncClient) -> asyncio.Task[httpx.Response]:
    """Start a diarize request without waiting for it."""
    return asyncio.create_task(
        client.post(
            "/diarize",
            data={"sample_rate": str(_RATE)},
            files={"file": ("audio.wav", b"1,2", "audio/wav")},
        )
    )


class _ExclusiveWorker:
    """A worker that, like the real one, can attend to exactly one thing at a time.

    This class is the whole point of rewriting the test below, and the reason
    the version before it was green while the deployment was red. That one
    stalled the model on a ``threading.Event``, which releases the GIL, so
    every other thread of the process carried on and ``/healthz`` answered -
    demonstrating a thing the real service could not do. sherpa-onnx's
    ``process`` is bound without ``py::call_guard<py::gil_scoped_release>`` and
    holds the GIL for its whole run, so while one is in flight *nothing* else
    in that interpreter is scheduled.

    Rather than reproduce that (a test that burns CPU to prove a point is a
    slow test that fails on a loaded box), this states the same constraint at
    the seam: ``diarize`` holds a lock for its whole run, and ``check_models``
    refuses to wait for that lock, counting the intrusion and failing loudly.
    A ``/healthz`` that needs to ask the worker anything therefore fails this
    test outright, instead of passing because a fake was more accommodating
    than the thing it stands in for.
    """

    def __init__(self, entered: threading.Event, release: threading.Event) -> None:
        self._entered = entered
        self._release = release
        self._busy = threading.Lock()
        self.intrusions = 0
        self.checks = 0

    def check_models(self) -> None:
        if not self._busy.acquire(blocking=False):
            self.intrusions += 1
            msg = "the worker was asked about its models while it was diarizing"
            raise AssertionError(msg)
        try:
            self.checks += 1
        finally:
            self._busy.release()

    def diarize(self, work: diarization_app.DiarizeWork) -> diarization_app.DiarizeOutcome:
        _ = work
        with self._busy:
            self._entered.set()
            self._release.wait(_STALL_LIMIT_S)
            return diarization_app.DiarizeOutcome(spans=[(0.0, 1.0, 0)], pooled={}, rate=_RATE)

    def close(self) -> None:
        return None


async def test_healthz_answers_while_a_diarization_is_running() -> None:
    """The measured failure: a busy service read as a service that is gone.

    The app probes this endpoint with a 2 s deadline while the UI polls, so a
    health check that waits on the diarization reports the diarizer unreachable
    for exactly as long as it is working - which is what happened on the box,
    for the whole of a 36-minute recording, while the container was up and busy
    throughout. The deadline here is that same 2 s, so this fails by timing out
    rather than by being slow.

    Polled several times, because "the app's badge went red, poll after poll"
    is what was actually reported, and once is not that. The worker is asked
    exactly once in the whole test, on the cold probe before any audio arrives:
    every answer after that comes out of this process's own memory, which is
    the only way one can be given while the models are unreachable by anyone.
    """
    entered, release = threading.Event(), threading.Event()
    worker = _ExclusiveWorker(entered, release)

    async with httpx.AsyncClient(transport=_transport(worker), base_url="http://diar") as client:
        assert (await client.get("/healthz")).status_code == 200

        diarizing = _post_diarize(client)
        assert await asyncio.to_thread(entered.wait, _STALL_LIMIT_S)

        for _ in range(3):
            health = await asyncio.wait_for(client.get("/healthz"), timeout=2.0)
            assert health.status_code == 200
            assert health.json()["status"] == "ok"

        release.set()
        assert (await diarizing).status_code == 200

    assert worker.intrusions == 0
    assert worker.checks == 1


async def test_healthz_still_answers_while_the_model_thread_stalls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same promise, one layer down, against a model that merely takes a while.

    Kept alongside the test above rather than replaced by it: this one drives
    the real ``InlineWorker`` and the real slot, so it covers the path an
    in-process deployment takes, while that one covers the constraint the real
    binding imposes. Neither subsumes the other.
    """
    entered, release = threading.Event(), threading.Event()
    _stall_the_model(monkeypatch, entered, release)

    async with httpx.AsyncClient(transport=_transport(), base_url="http://diar") as client:
        diarizing = _post_diarize(client)
        assert await asyncio.to_thread(entered.wait, _STALL_LIMIT_S)

        health = await asyncio.wait_for(client.get("/healthz"), timeout=2.0)
        assert health.status_code == 200
        assert health.json()["status"] == "ok"

        release.set()
        assert (await diarizing).status_code == 200


async def test_two_diarizations_are_served_one_after_the_other(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second caller queues, and is served, rather than racing the first.

    Queueing is the right answer for the live path: it sends one short turn per
    call and several turns can close at once, and two of them inside the model
    together would take twice as long each and finish no sooner.
    """
    entered, release = threading.Event(), threading.Event()
    pipeline = _stall_the_model(monkeypatch, entered, release)

    async with httpx.AsyncClient(transport=_transport(), base_url="http://diar") as client:
        first = _post_diarize(client)
        assert await asyncio.to_thread(entered.wait, _STALL_LIMIT_S)
        second = _post_diarize(client)
        # Long enough for a second call to reach the model if nothing stopped
        # it; the assertion below is what says nothing did.
        await asyncio.sleep(0.05)
        release.set()

        assert (await first).status_code == 200
        assert (await second).status_code == 200

    assert pipeline.calls == 2
    assert pipeline.peak == 1


async def test_a_caller_that_would_wait_too_long_is_refused_with_a_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bounded queueing, because an unbounded one is the wedge itself.

    A caller that waits out its own timeout in the queue leaves work behind
    that the service still computes in full for nobody. 429 with a Retry-After
    says so in the one place the caller can act on it.
    """
    monkeypatch.setattr(diarization_app, "_QUEUE_WAIT_S", 0.05)
    entered, release = threading.Event(), threading.Event()
    pipeline = _stall_the_model(monkeypatch, entered, release)

    async with httpx.AsyncClient(transport=_transport(), base_url="http://diar") as client:
        first = _post_diarize(client)
        assert await asyncio.to_thread(entered.wait, _STALL_LIMIT_S)

        refused = await client.post(
            "/diarize",
            data={"sample_rate": str(_RATE)},
            files={"file": ("audio.wav", b"1,2", "audio/wav")},
        )
        assert refused.status_code == 429
        assert "another diarization" in refused.json()["detail"]
        assert refused.headers["retry-after"] == "30"

        release.set()
        assert (await first).status_code == 200

    assert pipeline.calls == 1  # the refused call never reached the model


# ---------------------------------------------------------------------------
# The process boundary itself
# ---------------------------------------------------------------------------
# Every test above runs the worker inline, which is where the service's own
# logic is worth testing and where a monkeypatched loader can reach. These few
# start the real child: spawn, a fresh interpreter, a pipe, and the dataclasses
# the service sends over it. No ONNX file anywhere - what is under test is the
# boundary, and a model would only make it slow.


class SlowFakeWorker:
    """What the child process runs below: no models, and slow to order.

    Public and module level because ``spawn`` pickles a class by reference and
    imports it in a fresh interpreter, which is the whole reason
    ``WorkerProcess`` takes what to build rather than hard-coding it.

    Configured through the environment rather than through arguments, since a
    class pickled by name carries no state along with it and the child inherits
    this process's environment when it is spawned.
    """

    def check_models(self) -> None:
        """Load nothing, or refuse the way an unmounted model file refuses."""
        unloadable = os.environ.get("DIAR_TEST_UNLOADABLE")
        if unloadable:
            raise diarization_app.ModelsUnavailableError(unloadable)

    def diarize(self, work: diarization_app.DiarizeWork) -> diarization_app.DiarizeOutcome:
        """Reflect the request back inside the shapes the service reads.

        None of this is a diarization. What is under test is that the request
        crossed whole and the answer came back the same way, the pooled
        embedding included - that last being the one whose loss would be silent
        rather than loud, since the speaker bank is fed from it.
        """
        if os.environ.get("DIAR_TEST_EXIT"):
            os._exit(9)  # a child dying mid-call, the way an OOM kill ends one
        time.sleep(float(os.environ.get("DIAR_TEST_STALL_S", "0") or 0))
        return diarization_app.DiarizeOutcome(
            spans=[(0.0, float(len(work.payload)), 0)],
            pooled={0: diarization_app.ClusterVoice(vector=[1.0], reliable=work.embeddings)},
            rate=_RATE,
        )

    def close(self) -> None:
        return None


@contextlib.contextmanager
def _child_worker(
    monkeypatch: pytest.MonkeyPatch, **environment: str
) -> Generator[diarization_app.WorkerProcess]:
    """A real ``WorkerProcess`` running :class:`SlowFakeWorker`, closed afterwards.

    Closed on the way out whatever happened, because a leaked child outlives
    the test that made it and the next one then measures two.
    """
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    worker = diarization_app.WorkerProcess(SlowFakeWorker)
    try:
        yield worker
    finally:
        worker.close()


async def test_a_child_process_serves_a_whole_call_over_its_pipe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The request and its answer cross a real process boundary intact.

    Including ``ClusterVoice``: the child embeds and this process matches, so
    a session's speaker is decided here, from something the child sent, in code
    that no boundary runs through. The generation in the answer is this
    process's, which is the same fact stated from the other end - the bank the
    caller is being told about is the one in the process it is talking to.
    """
    with _child_worker(monkeypatch) as worker:
        async with httpx.AsyncClient(
            transport=_transport(worker), base_url="http://diar"
        ) as client:
            assert (await client.get("/healthz")).status_code == 200
            response = await client.post(
                "/diarize",
                data={"sample_rate": str(_RATE), "session_id": "s1"},
                files={"file": ("audio.wav", b"1,2,1", "audio/wav")},
            )

    assert response.status_code == 200
    body = response.json()
    # Five bytes of payload reached the child and the span it built from them
    # came back, labelled with a speaker this process opened for it.
    assert body["segments"] == [{"start": 0.0, "end": 5.0, "speaker": "Speaker 0"}]
    assert body["sample_rate"] == _RATE
    assert body["generation"] == diarization_app.GENERATION


async def test_healthz_answers_while_the_child_process_is_working(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole change, end to end, against a child that genuinely cannot answer.

    The child is busy for longer than the app's probe deadline and its pipe is
    silent for all of it, which is exactly what an inference does to it. This
    process answers anyway, in milliseconds, because it is not the one doing
    the work and readiness is not a question it has to forward.
    """
    with _child_worker(monkeypatch, DIAR_TEST_STALL_S="1.5") as worker:
        async with httpx.AsyncClient(
            transport=_transport(worker), base_url="http://diar"
        ) as client:
            assert (await client.get("/healthz")).status_code == 200
            diarizing = _post_diarize(client)
            await asyncio.sleep(0.2)  # long enough for the child to be inside the call
            assert not diarizing.done()

            for _ in range(3):
                health = await asyncio.wait_for(client.get("/healthz"), timeout=2.0)
                assert health.status_code == 200
                assert health.json()["status"] == "ok"

            assert (await diarizing).status_code == 200


async def test_a_load_failure_in_the_child_is_the_same_503_out_here(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A model that will not load says so through the pipe, not through a traceback.

    A traceback does not pickle and a sherpa-onnx exception class need not
    exist on both sides, so the child sends which half failed and what to put
    in front of a human. This is the half that is a 503, and the sentence on
    the settings page is the one the child wrote.
    """
    unmounted = "/models/seg.onnx is not mounted"
    with _child_worker(monkeypatch, DIAR_TEST_UNLOADABLE=unmounted) as worker:
        async with httpx.AsyncClient(
            transport=_transport(worker), base_url="http://diar"
        ) as client:
            health = await client.get("/healthz")
            diarize = await client.post(
                "/diarize", files={"file": ("audio.wav", b"1", "audio/wav")}
            )

    assert health.status_code == 503
    assert health.json() == {"detail": unmounted}
    assert diarize.status_code == 503
    assert diarize.json() == health.json()  # two routes, one verdict


async def test_a_child_that_exits_is_replaced_and_the_caller_is_told(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dead child costs one request and a model reload, not the service.

    The models used to be in the process serving HTTP, so anything that killed
    them killed the server, and the restart also forgot every session's
    speakers. Now this process survives, and the two things it must not do are
    hang waiting for an answer that is not coming, and go on believing the dead
    child's models were loaded - which is why the deadlines here are real
    assertions and why the call after it is served rather than refused.
    """
    with _child_worker(monkeypatch, DIAR_TEST_EXIT="1") as worker:
        async with httpx.AsyncClient(
            transport=_transport(worker), base_url="http://diar"
        ) as client:
            assert (await client.get("/healthz")).status_code == 200

            lost = await asyncio.wait_for(_post_diarize(client), timeout=_STALL_LIMIT_S)
            assert lost.status_code == 503
            assert "exited" in lost.json()["detail"]

            monkeypatch.delenv("DIAR_TEST_EXIT")
            served = await asyncio.wait_for(_post_diarize(client), timeout=_STALL_LIMIT_S)
            assert served.status_code == 200


def test_closing_does_not_wait_for_the_child_to_finish(monkeypatch: pytest.MonkeyPatch) -> None:
    """``docker stop`` has ten seconds; a diarization can have thirty minutes.

    The container was measured being SIGKILLed over exactly this, which is why
    uvicorn is given ``--timeout-graceful-shutdown 5``. Moving the models into
    a child must not put that back - a ``ProcessPoolExecutor`` would have, by
    joining its worker at interpreter exit however long the worker had left.
    So: a child told to be busy for a minute, and a close that has to return in
    a fraction of it with nothing of ours still running.
    """
    told: list[str] = []

    def diarize_in_the_background(worker: diarization_app.WorkerProcess) -> None:
        work = diarization_app.DiarizeWork(payload=b"1", num_clusters=-1, embeddings=False)
        try:
            worker.diarize(work)
        except diarization_app.WorkerUnavailableError:
            told.append("the worker went away")

    with _child_worker(monkeypatch, DIAR_TEST_STALL_S="60") as worker:
        worker.check_models()  # the child is up and idle
        caller = threading.Thread(target=diarize_in_the_background, args=(worker,), daemon=True)
        caller.start()
        time.sleep(0.3)  # long enough for the child to be inside the call

        started = time.monotonic()
        worker.close()
        took = time.monotonic() - started

    caller.join(_STALL_LIMIT_S)
    assert took < 3.0, f"close() waited {took:.1f}s on a child with 60s of work in it"
    assert told == ["the worker went away"]
    assert not [p for p in multiprocessing.active_children() if p.name == "diarization-models"]


class _FlakyWorker:
    """An inline worker whose process is pretended to die once, mid-session.

    Standing in for what a real child does when it is OOM-killed: the call in
    flight is lost and the next one is served by a fresh child holding no
    models. The point of the test below is what is *not* lost with it.
    """

    def __init__(self, inner: diarization_app.DiarizeWorker) -> None:
        self._inner = inner
        self.die_on_the_next_call = False

    def check_models(self) -> None:
        self._inner.check_models()

    def diarize(self, work: diarization_app.DiarizeWork) -> diarization_app.DiarizeOutcome:
        if self.die_on_the_next_call:
            self.die_on_the_next_call = False
            msg = "the process holding the diarization models exited before it answered"
            raise diarization_app.WorkerUnavailableError(msg)
        return self._inner.diarize(work)

    def close(self) -> None:
        self._inner.close()


async def test_a_worker_that_dies_does_not_renumber_a_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Where the speaker bank lives, said as a behaviour rather than as a comment.

    The models are in a child and the bank is not, so losing the child costs a
    model reload and nothing else. Had the bank gone with them, the next turn
    would start again at Speaker 0 and one label would end up naming two
    people - the failure ``generation`` exists to report, and one a caller
    could not even see here, because the process ``generation`` names never
    restarted.
    """
    _stub_models(monkeypatch)
    worker = _FlakyWorker(diarization_app.InlineWorker())
    async with httpx.AsyncClient(transport=_transport(worker), base_url="http://diar") as client:
        assert await _speakers(client, "1", session_id="s1") == ["Speaker 0"]
        assert await _speakers(client, "2", session_id="s1") == ["Speaker 1"]
        before = (await client.get("/healthz")).json()["generation"]

        worker.die_on_the_next_call = True
        lost = await client.post(
            "/diarize",
            data={"sample_rate": str(_RATE), "session_id": "s1"},
            files={"file": ("audio.wav", b"1", "audio/wav")},
        )
        assert lost.status_code == 503

        assert await _speakers(client, "1", session_id="s1") == ["Speaker 0"]
        assert await _speakers(client, "2", session_id="s1") == ["Speaker 1"]
        assert (await client.get("/healthz")).json()["generation"] == before
