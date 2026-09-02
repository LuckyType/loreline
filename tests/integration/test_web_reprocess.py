"""Integration tests for export endpoints + the re-processing pipeline."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest_asyncio
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient
from test_web_session import (  # type: ignore[import-not-found]
    FakeBackend,
    FakeDiarizer,
    capture_factory,
)

from loreline.models import ProviderConfig, SpeakerSegment
from loreline.secrets import SecretStore
from loreline.settings import Settings
from loreline.web.app import create_app


@pytest_asyncio.fixture
async def client(tmp_path: Path) -> AsyncIterator[AsyncClient]:
    settings = Settings(data_dir=tmp_path / "data", auth_password="", jwt_secret="t")
    app = create_app(
        settings,
        capture_factory=capture_factory,  # type: ignore[arg-type]
        backend_factory=FakeBackend,  # type: ignore[arg-type]
        diarizer_factory=lambda _cfg: FakeDiarizer(),
    )
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac


async def _provider(client: AsyncClient) -> str:
    resp = await client.post(
        "/api/providers",
        json={"name": "Fake", "kind": "openai_compat", "protocol": "http_batch"},
    )
    return resp.json()["id"]


async def _run_session(client: AsyncClient, pid: str) -> str:
    start = await client.post("/api/session/start", json={"primary_provider": pid})
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


async def test_audio_download(client: AsyncClient) -> None:
    pid = await _provider(client)
    sid = await _run_session(client, pid)
    resp = await client.get(f"/api/session/{sid}/audio")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "audio/wav"
    assert resp.content[:4] == b"RIFF"


async def test_reprocess_transcribe_with_on_demand_model(tmp_path: Path) -> None:
    """A "transcribe" reprocess job can override the provider's stored model,
    same as starting a live session (see test_start_session_with_on_demand_model)."""
    settings = Settings(data_dir=tmp_path / "data", auth_password="", jwt_secret="t")
    captured: dict[str, str | None] = {}

    def factory(config: ProviderConfig, secrets: SecretStore) -> FakeBackend:
        captured["model"] = config.model
        return FakeBackend(config, secrets)

    app = create_app(
        settings,
        capture_factory=capture_factory,  # type: ignore[arg-type]
        backend_factory=factory,  # type: ignore[arg-type]
        diarizer_factory=lambda _cfg: FakeDiarizer(),
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
            # The override is recorded on the job row as the model that ran.
            assert job["model"] == "nova-9000"


async def test_reprocess_job(tmp_path: Path) -> None:
    # Needs the raw repo (not just the HTTP client) to check the alternate
    # transcript is persisted, so build the app directly like
    # test_diarize_session_relabels_globally does below.
    settings = Settings(data_dir=tmp_path / "data", auth_password="", jwt_secret="t")
    app = create_app(
        settings,
        capture_factory=capture_factory,  # type: ignore[arg-type]
        backend_factory=FakeBackend,  # type: ignore[arg-type]
        diarizer_factory=lambda _cfg: FakeDiarizer(),
    )
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            pid = await _provider(client)
            sid = await _run_session(client, pid)

            enqueue = await client.post(
                "/api/reprocess", json={"session_id": sid, "provider_id": pid}
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

            # The job records the model it resolved at enqueue time (the
            # provider here has none stored and no override was given).
            assert job["model"] is None

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
                "/api/reprocess", json={"session_id": sid, "provider_id": pid}
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
    it is for (see loreline.capabilities.LIVE_CAPTURE_EXCLUDED). The provider
    pickers mirror this split, so a regression here would silently strand every
    batch provider on the session page."""
    live_pid = await _provider(client)
    sid = await _run_session(client, live_pid)
    batch = await client.post(
        "/api/providers",
        json={"name": "OpenRouter", "kind": "openrouter", "protocol": "http_batch"},
    )
    batch_pid = batch.json()["id"]

    start = await client.post("/api/session/start", json={"primary_provider": batch_pid})
    assert start.status_code == 400
    assert "live" in start.json()["detail"]

    enqueue = await client.post(
        "/api/reprocess", json={"session_id": sid, "provider_id": batch_pid}
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
    resp = await client.post("/api/reprocess", json={"session_id": "missing", "provider_id": pid})
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
    ) -> list[SpeakerSegment]:
        _ = (wav, sample_rate, min_speakers, max_speakers)
        return [SpeakerSegment(start=-1e12, end=1e12, speaker="Speaker A")]

    async def aclose(self) -> None:
        return None


async def test_diarize_session_relabels_globally(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "data", auth_password="", jwt_secret="t")
    app = create_app(
        settings,
        capture_factory=capture_factory,  # type: ignore[arg-type]
        backend_factory=FakeBackend,  # type: ignore[arg-type]
        diarizer_factory=lambda _cfg: _WholeSessionDiarizer(),
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
            rp = await client.post("/api/reprocess", json={"session_id": sid, "provider_id": pid})
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
