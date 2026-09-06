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

import httpx
import pytest

import services.diarization.app as diarization_app


def _transport() -> httpx.ASGITransport:
    """A transport onto a fresh service app, so no test shares another's cache."""
    return httpx.ASGITransport(app=diarization_app.create_app())


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
    """Once ready, /healthz reports it, and does not reload on every poll."""
    calls: list[int] = []

    def fake_load_pipeline(num_clusters: int = -1) -> tuple[object, object]:
        calls.append(num_clusters)
        return object(), object()

    monkeypatch.setattr(diarization_app, "_load_pipeline", fake_load_pipeline)

    async with httpx.AsyncClient(transport=_transport(), base_url="http://diar") as client:
        first = await client.get("/healthz")
        second = await client.get("/healthz")

    assert first.status_code == 200
    assert first.json() == {"status": "ok"}
    assert second.status_code == 200
    assert calls == [-1]  # loaded once; the second poll reused the cached pipeline


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


def _fake_read_wav(data: bytes) -> tuple[list[float], int]:
    """One second of audio per voice named in the body: ``b"1,2"``."""
    samples: list[float] = []
    for voice in data.decode().split(","):
        samples.extend([float(voice)] * _RATE)
    return samples, _RATE


class _FakeNumpy:
    float32 = "float32"

    @staticmethod
    def array(values: list[float], dtype: object = None) -> list[float]:
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
    """Cuts the audio into runs of one voice, one cluster id per distinct voice."""

    def process(self, audio: list[float]) -> _FakeResult:
        result = _FakeResult()
        clusters: dict[float, int] = {}
        start = 0
        for index in range(1, len(audio) + 1):
            if index < len(audio) and audio[index] == audio[start]:
                continue
            voice = audio[start]
            cluster = clusters.setdefault(voice, len(clusters))
            result.append(_FakeSegment(start / _RATE, index / _RATE, cluster))
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
