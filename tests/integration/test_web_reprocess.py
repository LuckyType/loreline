"""Integration tests for export endpoints + the re-processing pipeline."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import NamedTuple

import httpx
import pytest_asyncio
from asgi_lifespan import LifespanManager
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from test_web_session import (  # type: ignore[import-not-found]
    FakeBackend,
    FakeDiarizer,
    FakeSource,
    GlossaryRecordingBackend,
    OutOfCreditBackend,
    capture_factory,
    fake_diarizers,
)

from loreline.audio.chunker import SpeechDetector, Utterance
from loreline.diarization.base import DiarizationProvider
from loreline.health import HealthReport, HealthStatus, raise_for_vendor_status
from loreline.models import (
    DiarizationConfig,
    DiarizationMode,
    JobStatus,
    ProviderConfig,
    SpeakerSegment,
    TranscriptEvent,
)
from loreline.reprocess.jobs import stored_audio_backend
from loreline.secrets import SecretStore
from loreline.settings import Settings
from loreline.stt import create_backend
from loreline.web.app import create_app
from loreline.web.schemas import ReprocessRequest

# Any model id: the fake backend never looks at it, but the API requires one -
# a provider row carries no model, so the request is where it is decided.
_MODEL = "fake-model"


async def _reachable_diarizer(endpoint: str) -> HealthReport:
    """Stand in for the probe ``enqueue`` runs before it accepts a diarize job.

    Every diarize test here names ``http://diar``, where nothing is listening:
    the real probe would refuse the job on the spot and no diarizer double
    under test would ever be called. What that refusal does when it *is* the
    subject has its own test at the bottom of this file.
    """
    _ = endpoint
    return HealthReport(HealthStatus.HEALTHY)


@pytest_asyncio.fixture
async def client(tmp_path: Path) -> AsyncIterator[AsyncClient]:
    settings = Settings(data_dir=tmp_path / "data", auth_password="", jwt_secret="t")
    app = create_app(
        settings,
        capture_factory=capture_factory,  # type: ignore[arg-type]
        backend_factory=FakeBackend,  # type: ignore[arg-type]
        diarizer_factory=fake_diarizers,
        diarizer_probe=_reachable_diarizer,
    )
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac


async def _provider(client: AsyncClient) -> str:
    resp = await client.post(
        "/api/providers",
        json={"name": "Fake", "kind": "openai_compat"},
    )
    return resp.json()["id"]


async def _run_session(client: AsyncClient, pid: str) -> str:
    start = await client.post("/api/session/start", json={"primary_provider": pid, "model": _MODEL})
    session_id: str = start.json()["id"]
    await client.post("/api/session/stop")
    return session_id


async def test_export_formats(client: AsyncClient) -> None:
    pid = await _provider(client)
    sid = await _run_session(client, pid)

    txt = await client.get(f"/api/session/{sid}/export", params={"fmt": "txt"})
    assert txt.status_code == 200
    assert "hello world" in txt.text
    assert "attachment" in txt.headers["content-disposition"]

    srt = await client.get(f"/api/session/{sid}/export", params={"fmt": "srt"})
    assert srt.status_code == 200
    assert "-->" in srt.text

    bad = await client.get(f"/api/session/{sid}/export", params={"fmt": "nope"})
    assert bad.status_code == 404


class ModelNamingBackend(FakeBackend):
    """FakeBackend whose text names the model that produced it.

    Which is the only way to tell two versions of one session apart end to
    end: the live capture and every re-transcription otherwise say the same
    thing, so an export returning the wrong one would look right.
    """

    async def transcribe(
        self,
        utterance: Utterance,
        *,
        session_id: str,
        glossary: object = None,
    ) -> TranscriptEvent | None:
        _ = glossary
        return TranscriptEvent(
            session_id=session_id,
            source=self.config.id,
            text=f"said by {self.model}",
            start_ts=utterance.start,
            end_ts=utterance.end,
            is_final=True,
        )


async def test_export_returns_the_version_it_was_asked_for(tmp_path: Path) -> None:
    """Export selects a transcript version, and refuses one that does not exist.

    The route had no version parameter at all and rendered the original with
    the version hard-coded, so re-transcribing a session with a better model
    and pressing Export on that version handed back the live capture under the
    new version's name, with nothing saying so.
    """
    settings = Settings(data_dir=tmp_path / "data", auth_password="", jwt_secret="t")
    app = create_app(
        settings,
        capture_factory=capture_factory,  # type: ignore[arg-type]
        backend_factory=ModelNamingBackend,  # type: ignore[arg-type]
        diarizer_factory=fake_diarizers,
    )
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            pid = await _provider(client)
            start = await client.post(
                "/api/session/start", json={"primary_provider": pid, "model": "live-model"}
            )
            sid = start.json()["id"]
            await client.post("/api/session/stop")

            enqueue = await client.post(
                "/api/reprocess",
                json={"session_id": sid, "provider_id": pid, "model": "nova-9000"},
            )
            version = enqueue.json()["id"]
            ctx = app.state.ctx  # pyright: ignore[reportAny]
            await ctx.reprocess.wait(version)

            named = await client.get(
                f"/api/session/{sid}/export", params={"fmt": "txt", "version": version}
            )
            assert named.status_code == 200
            assert "said by nova-9000" in named.text
            assert "live-model" not in named.text

            # No version named is still the live capture, so an old bookmark
            # keeps meaning what it meant.
            default = await client.get(f"/api/session/{sid}/export", params={"fmt": "txt"})
            assert "said by live-model" in default.text
            assert "nova-9000" not in default.text
            explicit = await client.get(
                f"/api/session/{sid}/export", params={"fmt": "txt", "version": "original"}
            )
            assert explicit.text == default.text

            # A version nobody produced is a 404, not the original wearing its
            # name.
            missing = await client.get(
                f"/api/session/{sid}/export", params={"fmt": "txt", "version": "nope"}
            )
            assert missing.status_code == 404
            assert "nope" in missing.json()["detail"]


async def test_audio_download(client: AsyncClient) -> None:
    pid = await _provider(client)
    sid = await _run_session(client, pid)
    resp = await client.get(f"/api/session/{sid}/audio")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "audio/wav"
    assert resp.content[:4] == b"RIFF"


async def test_reprocess_transcribe_names_the_model_it_runs(tmp_path: Path) -> None:
    """A "transcribe" job names its model, same as starting a live session.

    Required rather than optional: the provider row holds no model, so a job
    with none would have nothing to run - and the row's `model` column would go
    back to claiming null while a constant inside a connector decided."""
    settings = Settings(data_dir=tmp_path / "data", auth_password="", jwt_secret="t")
    captured: dict[str, str | None] = {}

    def factory(config: ProviderConfig, secrets: SecretStore, model: str | None) -> FakeBackend:
        captured["model"] = model
        return FakeBackend(config, secrets, model)

    app = create_app(
        settings,
        capture_factory=capture_factory,  # type: ignore[arg-type]
        backend_factory=factory,  # type: ignore[arg-type]
        diarizer_factory=fake_diarizers,
        diarizer_probe=_reachable_diarizer,
    )
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            pid = await _provider(client)
            sid = await _run_session(client, pid)

            enqueue = await client.post(
                "/api/reprocess",
                json={"session_id": sid, "provider_id": pid, "model": "nova-9000"},
            )
            assert enqueue.status_code == 202
            job_id = enqueue.json()["id"]

            job: dict[str, object] = {}
            for _ in range(50):
                job = (await client.get(f"/api/reprocess/{job_id}")).json()
                if job["status"] in {"done", "error"}:
                    break
                await asyncio.sleep(0.02)
            assert job["status"] == "done"
            assert captured["model"] == "nova-9000"
            # And the job row records the model that actually ran.
            assert job["model"] == "nova-9000"


async def test_a_reprocess_job_builds_its_connector_for_stored_audio(tmp_path: Path) -> None:
    """The app wires both managers from one argument, so the default each falls
    back to is the whole difference between them.

    A re-processing job replays a recording and prefers a model's batch
    transport (see loreline.reprocess.jobs.stored_audio_backend); a live
    session takes the model's own preference, which for nova-3 and
    universal-3-5-pro is the streaming socket. Every other test in this file
    injects a fake factory and would never notice the two swapping over.
    """
    settings = Settings(data_dir=tmp_path / "data", auth_password="", jwt_secret="t")
    app = create_app(
        settings,
        capture_factory=capture_factory,  # type: ignore[arg-type]
        diarizer_factory=fake_diarizers,
        diarizer_probe=_reachable_diarizer,
    )
    async with LifespanManager(app):
        ctx = app.state.ctx  # pyright: ignore[reportAny]
        assert ctx.reprocess._backend_factory is stored_audio_backend
        assert ctx.manager._backend_factory is create_backend


async def test_reprocess_applies_the_glossary_unless_switched_off(tmp_path: Path) -> None:
    """`use_glossary` defaults to on, matching what re-processing did before the
    option existed; off hands the backend no glossary at all. Either way the
    choice is recorded on the job row, like `model`, so a stored version can be
    read as glossary-biased or not."""
    settings = Settings(data_dir=tmp_path / "data", auth_password="", jwt_secret="t")
    seen: list[object] = []

    def factory(
        config: ProviderConfig, secrets: SecretStore, model: str | None
    ) -> GlossaryRecordingBackend:
        return GlossaryRecordingBackend(config, secrets, model, seen)

    app = create_app(
        settings,
        capture_factory=capture_factory,  # type: ignore[arg-type]
        backend_factory=factory,  # type: ignore[arg-type]
        diarizer_factory=fake_diarizers,
        diarizer_probe=_reachable_diarizer,
    )
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            pid = await _provider(client)
            sid = await _run_session(client, pid)
            await client.put("/api/glossary", json={"terms": ["Drakonia"]})
            ctx = app.state.ctx  # pyright: ignore[reportAny]

            seen.clear()
            enqueue = await client.post(
                "/api/reprocess", json={"session_id": sid, "provider_id": pid, "model": _MODEL}
            )
            await ctx.reprocess.wait(enqueue.json()["id"])
            job = (await client.get(f"/api/reprocess/{enqueue.json()['id']}")).json()
            assert job["status"] == "done"
            assert job["use_glossary"] is True
            assert seen and [getattr(g, "terms", None) for g in seen] == [["Drakonia"]] * len(seen)

            seen.clear()
            enqueue = await client.post(
                "/api/reprocess",
                json={
                    "session_id": sid,
                    "provider_id": pid,
                    "model": _MODEL,
                    "use_glossary": False,
                },
            )
            await ctx.reprocess.wait(enqueue.json()["id"])
            job = (await client.get(f"/api/reprocess/{enqueue.json()['id']}")).json()
            assert job["status"] == "done"
            assert job["use_glossary"] is False
            assert seen and all(g is None for g in seen)


async def test_reprocess_job(tmp_path: Path) -> None:
    # Needs the raw repo (not just the HTTP client) to check the alternate
    # transcript is persisted, so build the app directly like
    # test_diarize_session_relabels_globally does below.
    settings = Settings(data_dir=tmp_path / "data", auth_password="", jwt_secret="t")
    app = create_app(
        settings,
        capture_factory=capture_factory,  # type: ignore[arg-type]
        backend_factory=FakeBackend,  # type: ignore[arg-type]
        diarizer_factory=fake_diarizers,
        diarizer_probe=_reachable_diarizer,
    )
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            pid = await _provider(client)
            sid = await _run_session(client, pid)

            enqueue = await client.post(
                "/api/reprocess", json={"session_id": sid, "provider_id": pid, "model": _MODEL}
            )
            assert enqueue.status_code == 202
            job_id = enqueue.json()["id"]

            job: dict[str, object] = {}
            for _ in range(50):
                job = (await client.get(f"/api/reprocess/{job_id}")).json()
                if job["status"] in {"done", "error"}:
                    break
                await asyncio.sleep(0.02)
            assert job["status"] == "done"
            assert int(job["segments_added"]) >= 1  # type: ignore[arg-type]

            # The job records the model the request named, which is the model
            # that ran: there is no other source for one, and no connector
            # constant left to quietly substitute a different one.
            assert job["model"] == _MODEL

            jobs = await client.get("/api/reprocess", params={"session_id": sid})
            assert len(jobs.json()) == 1

            # The re-transcription is persisted as its own version, tagged by
            # the JOB id (so runs never overwrite each other)...
            ctx = app.state.ctx  # pyright: ignore[reportAny]
            raw_sources = {e.source for e in await ctx.transcripts.for_session(sid)}
            assert f"reprocess:{job_id}" in raw_sources

            # ...retrievable via the per-version transcript endpoint...
            version = await client.get(f"/api/session/{sid}/transcript", params={"version": job_id})
            assert version.status_code == 200
            assert len(version.json()) >= 1

            # ...but stays out of the canonical session view, which would
            # otherwise show every segment twice.
            detail = (await client.get(f"/api/session/{sid}")).json()
            sources = {seg["source"] for seg in detail["transcript"]}
            assert not any(s.startswith("reprocess:") for s in sources)

            # A second run with the same provider is a NEW version; both are kept.
            second = await client.post(
                "/api/reprocess", json={"session_id": sid, "provider_id": pid, "model": _MODEL}
            )
            second_id = second.json()["id"]
            for _ in range(50):
                if (await client.get(f"/api/reprocess/{second_id}")).json()["status"] == "done":
                    break
                await asyncio.sleep(0.02)
            raw_sources = {e.source for e in await ctx.transcripts.for_session(sid)}
            assert {f"reprocess:{job_id}", f"reprocess:{second_id}"} <= raw_sources


async def test_reprocess_accepts_a_kind_barred_from_live_capture(client: AsyncClient) -> None:
    """Re-processing replays stored audio, so the live-capture exclusion must
    not reach it: OpenRouter transcription has no streaming mode and is
    rejected for a live session, but re-processing a recording is exactly what
    it is for (see loreline.capabilities.supports_live_capture). The provider
    pickers mirror this split, so a regression here would silently strand every
    batch provider on the session page."""
    live_pid = await _provider(client)
    sid = await _run_session(client, live_pid)
    batch = await client.post(
        "/api/providers",
        json={"name": "OpenRouter", "kind": "openrouter"},
    )
    batch_pid = batch.json()["id"]

    start = await client.post(
        "/api/session/start", json={"primary_provider": batch_pid, "model": _MODEL}
    )
    assert start.status_code == 400
    assert "live" in start.json()["detail"]

    enqueue = await client.post(
        "/api/reprocess", json={"session_id": sid, "provider_id": batch_pid, "model": _MODEL}
    )
    assert enqueue.status_code == 202
    job_id = enqueue.json()["id"]
    job: dict[str, object] = {}
    for _ in range(50):
        job = (await client.get(f"/api/reprocess/{job_id}")).json()
        if job["status"] in {"done", "error"}:
            break
        await asyncio.sleep(0.02)
    assert job["status"] == "done"
    assert int(job["segments_added"]) >= 1  # type: ignore[arg-type]


async def test_reprocess_unknown_session(client: AsyncClient) -> None:
    pid = await _provider(client)
    resp = await client.post(
        "/api/reprocess", json={"session_id": "missing", "provider_id": pid, "model": _MODEL}
    )
    assert resp.status_code == 404


class _WholeSessionDiarizer:
    """Diarizer returning one segment that covers the whole session."""

    async def diarize(
        self,
        wav: bytes,
        *,
        sample_rate: int = 16000,
        min_speakers: int | None = None,
        max_speakers: int | None = None,
        session_id: str | None = None,
    ) -> list[SpeakerSegment]:
        _ = (wav, sample_rate, min_speakers, max_speakers, session_id)
        return [SpeakerSegment(start=-1e12, end=1e12, speaker="Speaker A")]

    async def aclose(self) -> None:
        return None


async def _whole_session_diarizers(_config: DiarizationConfig) -> DiarizationProvider:
    return _WholeSessionDiarizer()


async def test_diarize_session_relabels_globally(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "data", auth_password="", jwt_secret="t")
    app = create_app(
        settings,
        capture_factory=capture_factory,  # type: ignore[arg-type]
        backend_factory=FakeBackend,  # type: ignore[arg-type]
        diarizer_factory=_whole_session_diarizers,
        diarizer_probe=_reachable_diarizer,
    )
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            pid = await _provider(client)
            sid = await _run_session(client, pid)

            enqueue = await client.post(
                "/api/reprocess",
                json={
                    "session_id": sid,
                    "operation": "diarize",
                    "diarization": {"mode": "remote", "endpoint": "http://diar"},
                },
            )
            assert enqueue.status_code == 202
            job_id = enqueue.json()["id"]

            job: dict[str, object] = {}
            for _ in range(50):
                job = (await client.get(f"/api/reprocess/{job_id}")).json()
                if job["status"] in {"done", "error"}:
                    break
                await asyncio.sleep(0.02)
            assert job["status"] == "done"
            assert int(job["segments_added"]) >= 1  # type: ignore[arg-type]

            # The original version's diarized copy supersedes it in the
            # canonical view, tagged with the version it relabels.
            detail = (await client.get(f"/api/session/{sid}")).json()
            diarized = [s for s in detail["transcript"] if s["source"] == "diarize:original"]
            assert diarized
            assert all(s["speaker"] == "Speaker A" for s in diarized)

            # Diarizing a re-transcribed version relabels ONLY that version.
            rp = await client.post(
                "/api/reprocess", json={"session_id": sid, "provider_id": pid, "model": _MODEL}
            )
            rp_id = rp.json()["id"]
            for _ in range(50):
                if (await client.get(f"/api/reprocess/{rp_id}")).json()["status"] == "done":
                    break
                await asyncio.sleep(0.02)
            enqueue = await client.post(
                "/api/reprocess",
                json={
                    "session_id": sid,
                    "operation": "diarize",
                    "target": rp_id,
                    "diarization": {"mode": "remote", "endpoint": "http://diar"},
                },
            )
            assert enqueue.status_code == 202
            diar_id = enqueue.json()["id"]
            assert enqueue.json()["target"] == rp_id
            for _ in range(50):
                if (await client.get(f"/api/reprocess/{diar_id}")).json()["status"] == "done":
                    break
                await asyncio.sleep(0.02)
            version = await client.get(f"/api/session/{sid}/transcript", params={"version": rp_id})
            assert all(s["source"] == f"diarize:{rp_id}" for s in version.json())
            assert all(s["speaker"] == "Speaker A" for s in version.json())

            # A diarize job aimed at a nonexistent version is a 404.
            missing = await client.post(
                "/api/reprocess",
                json={
                    "session_id": sid,
                    "operation": "diarize",
                    "target": "nope",
                    "diarization": {"mode": "remote", "endpoint": "http://diar"},
                },
            )
            assert missing.status_code == 404


async def _wait_done(client: AsyncClient, job_id: str) -> dict[str, object]:
    """Poll a job until it leaves queued/running, and return the finished row.

    "Cancelled" is one of the ways to leave: a stopped run is over, it just
    ended on a decision rather than on the end of the recording. Callers assert
    which of the three they expected.
    """
    job: dict[str, object] = {}
    for _ in range(50):
        job = (await client.get(f"/api/reprocess/{job_id}")).json()
        if job["status"] in {"done", "error", "cancelled"}:
            return job
        await asyncio.sleep(0.02)
    return job


class _FailingDiarizer:
    """Diarizer standing in for a service that is reachable but misbehaves."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    async def diarize(
        self,
        wav: bytes,
        *,
        sample_rate: int = 16000,
        min_speakers: int | None = None,
        max_speakers: int | None = None,
        session_id: str | None = None,
    ) -> list[SpeakerSegment]:
        _ = (wav, sample_rate, min_speakers, max_speakers, session_id)
        raise self._exc

    async def aclose(self) -> None:
        return None


def _diarizer_503() -> httpx.HTTPStatusError:
    """Shaped exactly as ``RemoteDiarizer.diarize()`` raises it: the service
    is up but cannot serve, e.g. a 503 while its models load."""
    request = httpx.Request("POST", "http://diar/diarize")
    response = httpx.Response(503, text="models not loaded", request=request)
    try:
        raise_for_vendor_status(response)
    except httpx.HTTPStatusError as exc:
        return exc
    raise AssertionError("expected raise_for_vendor_status to raise")  # pragma: no cover


async def _diarize_job(client: AsyncClient, sid: str) -> dict[str, object]:
    enqueue = await client.post(
        "/api/reprocess",
        json={
            "session_id": sid,
            "operation": "diarize",
            "diarization": {"mode": "remote", "endpoint": "http://diar"},
        },
    )
    assert enqueue.status_code == 202
    return await _wait_done(client, enqueue.json()["id"])


async def test_diarize_job_translates_a_diarizer_error_status_for_the_gm(tmp_path: Path) -> None:
    """A diarizer that is running but cannot serve (e.g. a 503 while its
    models load) used to leave the job's error as the raw exception, including
    an httpx-generated link to Mozilla's HTTP status docs - developer-facing
    text on a GM-facing page. It now reads as a plain sentence; the vendor's
    detail is not lost, it lands in the job's traceback log instead of the row.
    """
    error = _diarizer_503()

    async def failing_diarizers(_config: DiarizationConfig) -> DiarizationProvider:
        return _FailingDiarizer(error)

    settings = Settings(data_dir=tmp_path / "data", auth_password="", jwt_secret="t")
    app = create_app(
        settings,
        capture_factory=capture_factory,  # type: ignore[arg-type]
        backend_factory=FakeBackend,  # type: ignore[arg-type]
        diarizer_factory=failing_diarizers,
        diarizer_probe=_reachable_diarizer,
    )
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            pid = await _provider(client)
            sid = await _run_session(client, pid)
            job = await _diarize_job(client, sid)

    assert job["status"] == "error"
    assert job["error"] == (
        "The diarization service answered but could not process the audio "
        "(is it configured correctly?)"
    )
    assert "developer.mozilla.org" not in str(job["error"])
    assert "models not loaded" not in str(job["error"])  # kept in the log, not the job row


async def test_diarize_job_leaves_an_unreachable_diarizer_error_as_is(tmp_path: Path) -> None:
    """A diarizer nothing is listening at is a different failure from one that
    answered badly: the raw message already reads fine ("connection refused"),
    so it is left alone rather than replaced with a sentence that would claim
    the service answered when nothing did."""

    async def unreachable_diarizers(_config: DiarizationConfig) -> DiarizationProvider:
        return _FailingDiarizer(httpx.ConnectError("Connection refused"))

    settings = Settings(data_dir=tmp_path / "data", auth_password="", jwt_secret="t")
    app = create_app(
        settings,
        capture_factory=capture_factory,  # type: ignore[arg-type]
        backend_factory=FakeBackend,  # type: ignore[arg-type]
        diarizer_factory=unreachable_diarizers,
        diarizer_probe=_reachable_diarizer,
    )
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            pid = await _provider(client)
            sid = await _run_session(client, pid)
            job = await _diarize_job(client, sid)

    assert job["status"] == "error"
    assert "Connection refused" in str(job["error"])


async def test_a_timed_out_diarization_says_so_instead_of_saying_nothing(
    tmp_path: Path,
) -> None:
    """The failure that was invisible end to end: an error with no words in it.

    ``str(httpx.ReadTimeout())`` is the empty string, and an ``UNREACHABLE``
    verdict used to be passed through as ``str(exc)``, so a diarization that
    ran out of time stored ``error=""``. The session page reads a job as failed
    only if it carries a message, so the column went back to "-" and a run that
    had taken two minutes was indistinguishable from a button never pressed.
    """

    async def timing_out_diarizers(_config: DiarizationConfig) -> DiarizationProvider:
        return _FailingDiarizer(httpx.ReadTimeout(""))

    settings = Settings(data_dir=tmp_path / "data", auth_password="", jwt_secret="t")
    app = create_app(
        settings,
        capture_factory=capture_factory,  # type: ignore[arg-type]
        backend_factory=FakeBackend,  # type: ignore[arg-type]
        diarizer_factory=timing_out_diarizers,
        diarizer_probe=_reachable_diarizer,
    )
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            pid = await _provider(client)
            sid = await _run_session(client, pid)
            job = await _diarize_job(client, sid)

    assert job["status"] == "error"
    error = str(job["error"])
    assert error.strip()  # the whole bug: this used to be ""
    assert "http://diar" in error  # which service, not just "something"
    assert "did not answer in time" in error
    assert "ReadTimeout" in error  # and which failure, for whoever reads a report


async def test_diarizing_against_a_service_that_is_not_answering_is_refused(
    tmp_path: Path,
) -> None:
    """A press against nothing at all is answered, not queued.

    Refusing costs one probe, the same one the health badge uses, and says
    which endpoint did not answer and what the probe saw.

    What it must not say is the part asserted at the bottom. The first version
    of this message told the operator to start the service, and while the
    diarizer could not answer during a run - it held the GIL from the first
    frame to the last - that sentence was shown about a service that was up and
    working, which is the least useful thing to tell somebody staring at a
    container they can see running. The service was fixed; this checks that the
    message no longer instructs from a verdict that cannot support one.
    """
    probed: list[str] = []

    async def unreachable(endpoint: str) -> HealthReport:
        probed.append(endpoint)
        return HealthReport(HealthStatus.UNREACHABLE, "no answer within 2s")

    settings = Settings(data_dir=tmp_path / "data", auth_password="", jwt_secret="t")
    app = create_app(
        settings,
        capture_factory=capture_factory,  # type: ignore[arg-type]
        backend_factory=FakeBackend,  # type: ignore[arg-type]
        diarizer_factory=_whole_session_diarizers,
        diarizer_probe=unreachable,
    )
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            pid = await _provider(client)
            sid = await _run_session(client, pid)

            refused = await client.post(
                "/api/reprocess",
                json={
                    "session_id": sid,
                    "operation": "diarize",
                    "diarization": {"mode": "remote", "endpoint": "http://diar"},
                },
            )
            assert refused.status_code == 503
            detail = refused.json()["detail"]
            assert "http://diar" in detail  # which service, not just "something"
            assert "no answer within 2s" in detail  # and what the probe actually saw
            # It offers causes rather than picking one, because two seconds of
            # silence cannot separate a stopped service from a mistyped address
            # from a network that will not carry the request.
            assert "may be stopped" in detail
            assert "only busy still answers" in detail
            assert "start it" not in detail
            # No job row either: a refused press leaves nothing behind to
            # explain later, which is the difference from a job that fails.
            assert (await client.get("/api/reprocess", params={"session_id": sid})).json() == []

            # A re-transcription is still allowed against the same dead
            # diarizer: its value is the transcript, and the router survives a
            # diarizer that is not there.
            allowed = await client.post(
                "/api/reprocess",
                json={
                    "session_id": sid,
                    "provider_id": pid,
                    "model": _MODEL,
                    "diarization": {"mode": "remote", "endpoint": "http://diar"},
                },
            )
            assert allowed.status_code == 202

    assert probed == ["http://diar"]


async def test_a_diarization_left_blank_runs_at_the_stored_default_endpoint(
    tmp_path: Path,
) -> None:
    """The fallback the diarize dialog promises, honoured end to end.

    The dialog's endpoint field says a blank value falls back to the server's
    configured one, and sends null to mean so; nothing resolved it, and the
    factory refused the config, so the job failed for a GM who had done what
    the copy invited. Now the stored default is read when the job is enqueued:
    the probe checks that endpoint, the diarizer is built for it, and the row
    records it, so the version list names where the run actually went rather
    than "endpoint: null". A transcribe job that asks for remote diarization
    the same way gets the same endpoint.
    """
    probed: list[str] = []
    built: list[DiarizationConfig] = []

    async def reachable(endpoint: str) -> HealthReport:
        probed.append(endpoint)
        return HealthReport(HealthStatus.HEALTHY)

    async def diarizers(config: DiarizationConfig) -> DiarizationProvider:
        built.append(config)
        return _WholeSessionDiarizer()

    settings = Settings(data_dir=tmp_path / "data", auth_password="", jwt_secret="t")
    app = create_app(
        settings,
        capture_factory=capture_factory,  # type: ignore[arg-type]
        backend_factory=FakeBackend,  # type: ignore[arg-type]
        diarizer_factory=diarizers,
        diarizer_probe=reachable,
    )
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            pid = await _provider(client)
            sid = await _run_session(client, pid)
            saved = await client.put(
                "/api/system/defaults",
                json={"diar_mode": "remote", "diar_endpoint": "http://stored:8001"},
            )
            assert saved.status_code == 200
            built.clear()

            # Null, the dialog's spelling of "blank", and whitespace, a
            # client's: neither is an address.
            for endpoint in (None, "   "):
                enqueue = await client.post(
                    "/api/reprocess",
                    json={
                        "session_id": sid,
                        "operation": "diarize",
                        "diarization": {"mode": "remote", "endpoint": endpoint},
                    },
                )
                assert enqueue.status_code == 202, enqueue.text
                assert enqueue.json()["diarization"]["endpoint"] == "http://stored:8001"
                job = await _wait_done(client, enqueue.json()["id"])
                assert job["status"] == "done"
                assert int(job["segments_added"]) >= 1  # type: ignore[arg-type]

            transcribe = await client.post(
                "/api/reprocess",
                json={
                    "session_id": sid,
                    "provider_id": pid,
                    "model": _MODEL,
                    "diarization": {"mode": "remote"},
                },
            )
            assert transcribe.status_code == 202, transcribe.text
            assert transcribe.json()["diarization"]["endpoint"] == "http://stored:8001"
            assert (await _wait_done(client, transcribe.json()["id"]))["status"] == "done"

    # Probed once per diarize job, at the resolved address; a transcribe job
    # is not probed (see test_diarizing_against_a_service_that_is_not_answering_is_refused).
    assert probed == ["http://stored:8001", "http://stored:8001"]
    # And every diarizer built for those jobs was built for that address.
    remote = [c.endpoint for c in built if c.mode is DiarizationMode.REMOTE]
    assert remote == ["http://stored:8001"] * 3


async def test_a_remote_diarization_with_no_endpoint_anywhere_is_refused(
    client: AsyncClient,
) -> None:
    """Blank means "the default", and with no default there is nothing to run
    against, so the press is answered with a 400 that says where to put one.

    A 400 and not a 503: the request is what has to change. No job row is
    left behind either, for the same reason the unreachable case leaves none.
    A transcribe job asking for remote diarization the same way is refused
    too: it builds its diarizer before the router runs, so it would fail on
    its first line rather than survive without labels, and a live capture
    asked for the same config is refused at the start button.
    """
    pid = await _provider(client)
    sid = await _run_session(client, pid)

    refused = await client.post(
        "/api/reprocess",
        json={"session_id": sid, "operation": "diarize", "diarization": {"mode": "remote"}},
    )
    assert refused.status_code == 400
    detail = refused.json()["detail"]
    assert "none is configured" in detail
    assert "Settings" in detail  # where the default lives

    transcribe = await client.post(
        "/api/reprocess",
        json={
            "session_id": sid,
            "provider_id": pid,
            "model": _MODEL,
            "diarization": {"mode": "remote"},
        },
    )
    assert transcribe.status_code == 400
    assert (await client.get("/api/reprocess", params={"session_id": sid})).json() == []

    # The other modes have no address to resolve, and are untouched.
    plain = await client.post(
        "/api/reprocess", json={"session_id": sid, "provider_id": pid, "model": _MODEL}
    )
    assert plain.status_code == 202


class _TrackingDiarizer:
    """Diarizer standing in for the remote service's per-session bank memory.

    Records the ``session_id`` every ``diarize()`` call carries into ``seen``,
    and mirrors ``RemoteDiarizer.aclose``: on close it "forgets" (into
    ``forgotten``) every id this instance has itself sent, and only those.
    ``seen``/``forgotten`` are shared across every diarizer the factory below
    builds, so a test can tell whether a transcribe job's own instance ever
    touches an id another caller (a live capture, another job) would use.
    """

    def __init__(self, seen: list[str | None], forgotten: list[str]) -> None:
        self._seen = seen
        self._forgotten = forgotten
        self._sessions: set[str] = set()

    async def diarize(
        self,
        wav: bytes,
        *,
        sample_rate: int = 16000,
        min_speakers: int | None = None,
        max_speakers: int | None = None,
        session_id: str | None = None,
    ) -> list[SpeakerSegment]:
        _ = (wav, sample_rate, min_speakers, max_speakers)
        self._seen.append(session_id)
        if session_id is not None:
            self._sessions.add(session_id)
        return []

    async def aclose(self) -> None:
        self._forgotten.extend(sorted(self._sessions))
        self._sessions.clear()


async def test_transcribe_job_diarizes_under_its_own_bank_and_forgets_only_that(
    tmp_path: Path,
) -> None:
    """A transcribe job must not diarize under the live session's own bank.

    ``RouterConfig.session_id`` (see ``loreline.stt.router``) has to stay the
    live session's own id - it is also what every produced ``TranscriptEvent``
    is filed under - but the remote diarizer keys its speaker bank on that
    same string over the wire. A transcribe job's own ``RemoteDiarizer``
    instance sending it would land on the SAME remote bank a live capture of
    that session uses, and the job's ``aclose`` - which deletes whatever it
    sent - would wipe the live capture's voices out from under it, renumbering
    it from Speaker 0. The job must diarize, and delete, under a bank id of
    its own instead (see ``loreline.reprocess.jobs._JobBankDiarizer``).
    """
    settings = Settings(data_dir=tmp_path / "data", auth_password="", jwt_secret="t")
    seen: list[str | None] = []
    forgotten: list[str] = []

    async def tracking_diarizers(_config: DiarizationConfig) -> DiarizationProvider:
        return _TrackingDiarizer(seen, forgotten)

    app = create_app(
        settings,
        capture_factory=capture_factory,  # type: ignore[arg-type]
        backend_factory=FakeBackend,  # type: ignore[arg-type]
        diarizer_factory=tracking_diarizers,
        diarizer_probe=_reachable_diarizer,
    )
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            pid = await _provider(client)
            sid = await _run_session(client, pid)

            enqueue = await client.post(
                "/api/reprocess",
                json={
                    "session_id": sid,
                    "provider_id": pid,
                    "model": _MODEL,
                    "diarization": {"mode": "remote", "endpoint": "http://diar"},
                },
            )
            assert enqueue.status_code == 202
            job_id = enqueue.json()["id"]
            job = await _wait_done(client, job_id)
            assert job["status"] == "done"

    expected_bank = f"{sid}:job:{job_id}"
    assert seen  # the job actually diarized at least one utterance
    assert sid not in seen  # never sent under the live session's own bare id
    assert seen == [expected_bank] * len(seen)
    assert forgotten == [expected_bank]  # only the job's own bank is deleted


async def test_delete_transcript_version(tmp_path: Path) -> None:
    """Deleting a version removes exactly its rows.

    Its segments go, so does the diarized copy that superseded them and the job
    rows that produced both - a `diarize:<version>` copy of rows that no longer
    exist is reachable by no reader. Every other version, and the original,
    is left alone.
    """
    settings = Settings(data_dir=tmp_path / "data", auth_password="", jwt_secret="t")
    app = create_app(
        settings,
        capture_factory=capture_factory,  # type: ignore[arg-type]
        backend_factory=FakeBackend,  # type: ignore[arg-type]
        diarizer_factory=_whole_session_diarizers,
        diarizer_probe=_reachable_diarizer,
    )
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            pid = await _provider(client)
            sid = await _run_session(client, pid)
            ctx = app.state.ctx  # pyright: ignore[reportAny]

            first = (
                await client.post(
                    "/api/reprocess", json={"session_id": sid, "provider_id": pid, "model": _MODEL}
                )
            ).json()["id"]
            await _wait_done(client, first)
            second = (
                await client.post(
                    "/api/reprocess", json={"session_id": sid, "provider_id": pid, "model": _MODEL}
                )
            ).json()["id"]
            await _wait_done(client, second)
            diar = (
                await client.post(
                    "/api/reprocess",
                    json={
                        "session_id": sid,
                        "operation": "diarize",
                        "target": first,
                        "diarization": {"mode": "remote", "endpoint": "http://diar"},
                    },
                )
            ).json()["id"]
            await _wait_done(client, diar)

            sources = {e.source for e in await ctx.transcripts.for_session(sid)}
            assert {f"reprocess:{first}", f"reprocess:{second}", f"diarize:{first}"} <= sources
            original_before = (await client.get(f"/api/session/{sid}")).json()["transcript"]
            assert original_before

            # A diarize job's id is not a version: it relabels one in place
            # rather than creating its own, so it is not addressable here.
            not_a_version = await client.delete(
                f"/api/session/{sid}/transcript", params={"version": diar}
            )
            assert not_a_version.status_code == 404

            resp = await client.delete(f"/api/session/{sid}/transcript", params={"version": first})
            assert resp.status_code == 200

            sources = {e.source for e in await ctx.transcripts.for_session(sid)}
            assert f"reprocess:{first}" not in sources
            assert f"diarize:{first}" not in sources  # the relabeling went with its base rows
            assert f"reprocess:{second}" in sources
            assert (await client.get(f"/api/session/{sid}")).json()["transcript"] == original_before

            # The version's own job row and the diarize job aimed at it are gone;
            # the surviving version keeps its own.
            remaining = (await client.get("/api/reprocess", params={"session_id": sid})).json()
            assert {j["id"] for j in remaining} == {second}

            # Deleting it twice is a 404, not a silent success.
            assert (
                await client.delete(f"/api/session/{sid}/transcript", params={"version": first})
            ).status_code == 404


async def test_delete_original_version_is_refused(client: AsyncClient) -> None:
    """The original is the live capture: nothing can produce it again, so the
    server refuses it rather than trusting the page to hide the button."""
    pid = await _provider(client)
    sid = await _run_session(client, pid)
    before = (await client.get(f"/api/session/{sid}")).json()["transcript"]
    assert before

    resp = await client.delete(f"/api/session/{sid}/transcript", params={"version": "original"})
    assert resp.status_code == 409
    assert "original" in resp.json()["detail"]
    assert (await client.get(f"/api/session/{sid}")).json()["transcript"] == before


async def test_delete_version_unknown_session(client: AsyncClient) -> None:
    resp = await client.delete("/api/session/missing/transcript", params={"version": "nope"})
    assert resp.status_code == 404


class _GatedBackend(FakeBackend):
    """FakeBackend that waits on a gate before every utterance after the first.

    Holds a run open at a known, non-final segment count so a test can read the
    job row mid-flight, which is the whole point of publishing that count while
    the job is still going. The same factory also serves the live capture that
    produces the audio, so the gate starts open and is closed once that capture
    is done - a blocked live session would just sit in the stop drain instead.
    """

    def __init__(
        self,
        config: ProviderConfig,
        secrets: SecretStore,
        model: str | None,
        gate: asyncio.Event,
    ) -> None:
        super().__init__(config, secrets, model)
        self._gate = gate
        self._seen = 0

    async def transcribe(
        self,
        utterance: Utterance,
        *,
        session_id: str,
        glossary: object = None,
    ) -> TranscriptEvent | None:
        self._seen += 1
        if self._seen > 1:
            await self._gate.wait()
        return await super().transcribe(utterance, session_id=session_id, glossary=glossary)


def _two_utterance_capture(_req: object, _sample_rate: int) -> tuple[FakeSource, SpeechDetector]:
    """Capture yielding two utterances: speech, a full silence gap, then speech.

    The shared fake capture produces a single utterance, and a run over one
    utterance publishes nothing before it ends (the router hands over a whole
    utterance's events at once), so a live count needs more than one.
    """
    pattern = [True] * 5 + [False] * 45 + [True] * 5
    frames = iter(pattern)

    def detector(_frame: bytes) -> bool:
        return next(frames, False)

    return FakeSource(frames=len(pattern)), detector


async def test_running_job_publishes_its_segment_count(tmp_path: Path) -> None:
    """`segments_added` reaches the row while the job runs, not only at the end.

    That is what lets the page (and a refresh, or a second browser) show a
    re-transcription filling up instead of a motionless "running".
    """
    settings = Settings(data_dir=tmp_path / "data", auth_password="", jwt_secret="t")
    gate = asyncio.Event()
    gate.set()  # open for the live capture below; closed before the job runs

    def factory(config: ProviderConfig, secrets: SecretStore, model: str | None) -> _GatedBackend:
        return _GatedBackend(config, secrets, model, gate)

    app = create_app(
        settings,
        capture_factory=_two_utterance_capture,  # type: ignore[arg-type]
        backend_factory=factory,  # type: ignore[arg-type]
        diarizer_factory=fake_diarizers,
        diarizer_probe=_reachable_diarizer,
    )
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            pid = await _provider(client)
            sid = await _run_session(client, pid)
            ctx = app.state.ctx  # pyright: ignore[reportAny]
            assert len(ctx.audio_store.read_utterances(sid)) == 2

            gate.clear()
            job_id = (
                await client.post(
                    "/api/reprocess", json={"session_id": sid, "provider_id": pid, "model": _MODEL}
                )
            ).json()["id"]
            job: dict[str, object] = {}
            for _ in range(100):
                job = (await client.get(f"/api/reprocess/{job_id}")).json()
                if int(job["segments_added"]) >= 1:  # type: ignore[arg-type]
                    break
                await asyncio.sleep(0.02)
            assert job["status"] == "running"
            assert job["segments_added"] == 1

            # A version still being written cannot be deleted: the run would
            # keep inserting rows that no job row explains any more.
            busy = await client.delete(f"/api/session/{sid}/transcript", params={"version": job_id})
            assert busy.status_code == 409

            gate.set()
            await ctx.reprocess.wait(job_id)
            final = (await client.get(f"/api/reprocess/{job_id}")).json()
            assert final["status"] == "done"
            assert final["segments_added"] == 2


async def test_reprocess_fails_with_the_vendors_reason_when_credit_runs_out(
    tmp_path: Path,
) -> None:
    """A provider that cannot answer any utterance must end the job, not run it.

    Before, every utterance paid for the same doomed request and the job
    finished "done" with nothing in it. Now the job carries what the vendor
    said, which is what the session page shows next to the failed run.
    """
    settings = Settings(data_dir=tmp_path / "data", auth_password="", jwt_secret="t")
    live = [FakeBackend]  # the capture itself must still work

    def factory(config: ProviderConfig, secrets: SecretStore, model: str | None) -> FakeBackend:
        cls = live.pop() if live else OutOfCreditBackend
        return cls(config, secrets, model)

    app = create_app(
        settings,
        capture_factory=capture_factory,  # type: ignore[arg-type]
        backend_factory=factory,  # type: ignore[arg-type]
        diarizer_factory=fake_diarizers,
        diarizer_probe=_reachable_diarizer,
    )
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            pid = await _provider(client)
            sid = await _run_session(client, pid)

            enqueue = await client.post(
                "/api/reprocess", json={"session_id": sid, "provider_id": pid, "model": _MODEL}
            )
            job_id = enqueue.json()["id"]
            ctx = app.state.ctx  # pyright: ignore[reportAny]
            await ctx.reprocess.wait(job_id)

            job = (await client.get(f"/api/reprocess/{job_id}")).json()
            assert job["status"] == "error"
            assert "no credits remaining" in job["error"]
            assert job["segments_added"] == 0


def _three_utterance_capture(_req: object, _sample_rate: int) -> tuple[FakeSource, SpeechDetector]:
    """Capture yielding three utterances, so a run has one left to decline.

    Two is not enough to cancel by. The flag is read where the *next* utterance
    would be handed to the router (see ``loreline.reprocess.jobs._aiter``), so
    a run parked on its last one has nothing left to refuse and finishes
    normally, correctly reporting "done". Three gives the press something to
    actually stop, and leaves the assertion "it wrote fewer than it would have"
    meaning something.
    """
    pattern = ([True] * 5 + [False] * 45) * 2 + [True] * 5
    frames = iter(pattern)

    def detector(_frame: bytes) -> bool:
        return next(frames, False)

    return FakeSource(frames=len(pattern)), detector


class _Gated(NamedTuple):
    """A client, the app behind it, and the gate that parks a run mid-flight.

    The gate is what makes cancelling testable at all: a job over in
    milliseconds cannot be caught in the act, so the backend holds every
    utterance after the first until a test opens it. ``app`` is here for the
    repositories - what the run left on disk is half of what these tests are
    checking, and the HTTP surface only shows the other half.
    """

    client: AsyncClient
    app: FastAPI
    gate: asyncio.Event


@pytest_asyncio.fixture
async def gated(tmp_path: Path) -> AsyncIterator[_Gated]:
    settings = Settings(data_dir=tmp_path / "data", auth_password="", jwt_secret="t")
    gate = asyncio.Event()
    gate.set()  # open for the live capture; the run under test closes it

    def factory(config: ProviderConfig, secrets: SecretStore, model: str | None) -> _GatedBackend:
        return _GatedBackend(config, secrets, model, gate)

    app = create_app(
        settings,
        capture_factory=_three_utterance_capture,  # type: ignore[arg-type]
        backend_factory=factory,  # type: ignore[arg-type]
        diarizer_factory=fake_diarizers,
        diarizer_probe=_reachable_diarizer,
    )
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield _Gated(ac, app, gate)


async def _cancel_mid_run(gated: _Gated, pid: str, sid: str) -> tuple[str, dict[str, object]]:
    """Re-transcribe, stop the run after its first segment, return the settled row.

    Deterministic rather than timed. The backend is parked inside its second
    utterance when Cancel is pressed, so the run is guaranteed to be in flight
    with exactly one segment on disk; opening the gate lets that second
    utterance finish and land, and the flag is read at the boundary after it.
    Which is why the run ends on two segments and not one: cancelling asks a
    run to stop at its next clean boundary, not to abandon work in progress.
    """
    gated.gate.clear()
    job_id: str = (
        await gated.client.post(
            "/api/reprocess", json={"session_id": sid, "provider_id": pid, "model": _MODEL}
        )
    ).json()["id"]
    job: dict[str, object] = {}
    for _ in range(100):
        job = (await gated.client.get(f"/api/reprocess/{job_id}")).json()
        if int(job["segments_added"]) >= 1:  # type: ignore[arg-type]
            break
        await asyncio.sleep(0.02)
    assert job["status"] == "running"

    resp = await gated.client.post(f"/api/reprocess/{job_id}/cancel")
    assert resp.status_code == 200

    gated.gate.set()
    ctx = gated.app.state.ctx  # pyright: ignore[reportAny]
    await ctx.reprocess.wait(job_id)
    return job_id, (await gated.client.get(f"/api/reprocess/{job_id}")).json()


async def test_cancelling_a_running_job_keeps_what_it_already_wrote(gated: _Gated) -> None:
    """The whole point of the feature: stop early, keep the transcript so far.

    A GM watching a re-transcription fill up can tell within a minute or two
    whether the model is worth the rest of the recording. Stopping it must not
    throw away what they were reading - those rows are how they decided, and
    they are about to read them again before deleting the version.
    """
    pid = await _provider(gated.client)
    sid = await _run_session(gated.client, pid)
    ctx = gated.app.state.ctx  # pyright: ignore[reportAny]
    assert len(ctx.audio_store.read_utterances(sid)) == 3

    job_id, job = await _cancel_mid_run(gated, pid, sid)

    assert job["status"] == "cancelled"
    assert job["error"] is None  # a decision, not a failure
    # The utterance in flight when the press landed still counted; the third
    # was never asked for, which is what "it stopped" means here.
    assert job["segments_added"] == 2
    rows = (
        await gated.client.get(f"/api/session/{sid}/transcript", params={"version": job_id})
    ).json()
    assert len(rows) == 2
    # And the version is still readable under its own name, which is what keeps
    # the partial transcript on screen while the GM decides about it.
    assert f"reprocess:{job_id}" in {e.source for e in await ctx.transcripts.for_session(sid)}


async def test_deleting_a_cancelled_versions_transcript(gated: _Gated) -> None:
    """The end of the story the feature was asked for: stop it, then bin it.

    The delete guard refuses a version a job is "still being written", and a
    cancelled job must not look like one - the row only reaches that status
    once the run has stopped writing, so there is nothing left to protect the
    version from.
    """
    pid = await _provider(gated.client)
    sid = await _run_session(gated.client, pid)
    job_id, job = await _cancel_mid_run(gated, pid, sid)
    assert job["status"] == "cancelled"
    ctx = gated.app.state.ctx  # pyright: ignore[reportAny]

    resp = await gated.client.delete(f"/api/session/{sid}/transcript", params={"version": job_id})
    assert resp.status_code == 200

    assert f"reprocess:{job_id}" not in {e.source for e in await ctx.transcripts.for_session(sid)}
    assert (await gated.client.get("/api/reprocess", params={"session_id": sid})).json() == []
    # The capture the run was compared against is untouched.
    assert (await gated.client.get(f"/api/session/{sid}")).json()["transcript"]


async def test_a_cancelled_job_is_not_reported_as_a_failure(gated: _Gated) -> None:
    """The session page's "Last failed job" line reads this list and filters it
    on ``status == 'error'``. A run the GM stopped themselves must not show up
    there: nothing broke, and reporting their own decision back to them as the
    session's most recent failure is both wrong and alarming."""
    pid = await _provider(gated.client)
    sid = await _run_session(gated.client, pid)
    _, job = await _cancel_mid_run(gated, pid, sid)
    assert job["status"] == "cancelled"

    jobs = (await gated.client.get("/api/reprocess", params={"session_id": sid})).json()
    assert [j["status"] for j in jobs] == ["cancelled"]
    assert [j for j in jobs if j["status"] == "error"] == []
    assert all(j["error"] is None for j in jobs)


async def test_cancelling_a_queued_job_stops_it_before_it_runs(tmp_path: Path) -> None:
    """A job cancelled before the loop ever started it must not run at all.

    Through the manager rather than over HTTP, because that is the only way to
    press Cancel while the job is genuinely QUEUED: ``enqueue`` creates the
    task and returns without awaiting anything after it, so control comes back
    here before the loop has run a line of it. That window is exactly what the
    flag raised before ``cancel``'s first await exists for. ``started_at``
    staying None is the proof it never ran - the run sets it before it does
    anything else at all.
    """
    settings = Settings(data_dir=tmp_path / "data", auth_password="", jwt_secret="t")
    app = create_app(
        settings,
        capture_factory=capture_factory,  # type: ignore[arg-type]
        backend_factory=FakeBackend,  # type: ignore[arg-type]
        diarizer_factory=fake_diarizers,
        diarizer_probe=_reachable_diarizer,
    )
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            pid = await _provider(client)
            sid = await _run_session(client, pid)
            ctx = app.state.ctx  # pyright: ignore[reportAny]

            job = await ctx.reprocess.enqueue(
                ReprocessRequest(session_id=sid, provider_id=pid, model=_MODEL)
            )
            assert job.status is JobStatus.QUEUED
            stopped = await ctx.reprocess.cancel(job.id)

            assert stopped.status is JobStatus.CANCELLED
            assert stopped.started_at is None
            assert stopped.finished_at is not None
            assert stopped.segments_added == 0
            # Nothing was written under the version it would have produced...
            sources = {e.source for e in await ctx.transcripts.for_session(sid)}
            assert f"reprocess:{job.id}" not in sources
            # ...and it stays stopped rather than starting a moment later.
            await asyncio.sleep(0.05)
            assert (await client.get(f"/api/reprocess/{job.id}")).json()["status"] == "cancelled"


async def test_cancelling_a_finished_job_is_a_conflict(client: AsyncClient) -> None:
    """A race the page can genuinely lose, so it gets an answer it can read.

    The job list is polled every 1.5s, so a run can reach the end between the
    poll that drew the Cancel button and the press that arrives here. 409
    naming the state it finished in, and the row left exactly as it was: "too
    late, it is done" is a different fact from "too late, it failed", and the
    page has to be able to tell them apart without a second request.
    """
    pid = await _provider(client)
    sid = await _run_session(client, pid)
    job_id = (
        await client.post(
            "/api/reprocess", json={"session_id": sid, "provider_id": pid, "model": _MODEL}
        )
    ).json()["id"]
    done = await _wait_done(client, job_id)
    assert done["status"] == "done"

    resp = await client.post(f"/api/reprocess/{job_id}/cancel")
    assert resp.status_code == 409
    assert "done" in resp.json()["detail"]
    assert (await client.get(f"/api/reprocess/{job_id}")).json() == done


async def test_cancelling_an_unknown_job_is_a_404(client: AsyncClient) -> None:
    resp = await client.post("/api/reprocess/does-not-exist/cancel")
    assert resp.status_code == 404


class _BlockingDiarizer:
    """Diarizer that answers only once a test lets it.

    Stands in for what a diarize job really is: one call that takes minutes and
    offers no boundary in the middle of itself, so nothing short of
    interrupting the task can stop it.
    """

    def __init__(self, released: asyncio.Event) -> None:
        self._released = released

    async def diarize(
        self,
        wav: bytes,
        *,
        sample_rate: int = 16000,
        min_speakers: int | None = None,
        max_speakers: int | None = None,
        session_id: str | None = None,
    ) -> list[SpeakerSegment]:
        _ = (wav, sample_rate, min_speakers, max_speakers, session_id)
        await self._released.wait()
        return [SpeakerSegment(start=-1e12, end=1e12, speaker="Speaker A")]

    async def aclose(self) -> None:
        return None


async def test_cancelling_a_diarize_job_interrupts_the_call_it_is_stuck_in(
    tmp_path: Path,
) -> None:
    """A diarization is stopped by interrupting the task, and leaves no trace.

    There is no utterance loop to stop between, so the cooperative flag cannot
    reach the work and ``cancel`` cuts the task instead. What must survive that
    is the version being relabeled: a half-written ``diarize:<version>`` copy
    supersedes the version's own rows on read, so it would not show a partial
    relabeling - it would hide every row it had not reached yet. Cancelled
    here, before any of it is written, the version keeps its own rows outright.
    """
    released = asyncio.Event()

    async def diarizers(config: DiarizationConfig) -> DiarizationProvider:
        # Only the job's own remote config blocks. The live capture that
        # produced the audio gets the ordinary fake, so nothing can park it.
        if config.mode is DiarizationMode.REMOTE:
            return _BlockingDiarizer(released)
        return FakeDiarizer()

    settings = Settings(data_dir=tmp_path / "data", auth_password="", jwt_secret="t")
    app = create_app(
        settings,
        capture_factory=capture_factory,  # type: ignore[arg-type]
        backend_factory=FakeBackend,  # type: ignore[arg-type]
        diarizer_factory=diarizers,
        diarizer_probe=_reachable_diarizer,
    )
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            pid = await _provider(client)
            sid = await _run_session(client, pid)
            before = (await client.get(f"/api/session/{sid}")).json()["transcript"]
            assert before

            job_id = (
                await client.post(
                    "/api/reprocess",
                    json={
                        "session_id": sid,
                        "operation": "diarize",
                        "target": "original",
                        "diarization": {"mode": "remote", "endpoint": "http://diar"},
                    },
                )
            ).json()["id"]
            for _ in range(100):
                if (await client.get(f"/api/reprocess/{job_id}")).json()["status"] == "running":
                    break
                await asyncio.sleep(0.02)

            resp = await client.post(f"/api/reprocess/{job_id}/cancel")
            assert resp.status_code == 200
            # A cut task ends at once, so unlike a re-transcription this one has
            # already settled by the time the endpoint answers.
            assert resp.json()["status"] == "cancelled"
            assert resp.json()["segments_added"] == 0

            ctx = app.state.ctx  # pyright: ignore[reportAny]
            sources = {e.source for e in await ctx.transcripts.for_session(sid)}
            assert "diarize:original" not in sources
            assert (await client.get(f"/api/session/{sid}")).json()["transcript"] == before
            released.set()  # nothing waits on it now; leave no task holding it
