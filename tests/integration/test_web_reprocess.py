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
    FakeSource,
    GlossaryRecordingBackend,
    OutOfCreditBackend,
    capture_factory,
)

from loreline.audio.chunker import SpeechDetector, Utterance
from loreline.models import ProviderConfig, SpeakerSegment, TranscriptEvent
from loreline.secrets import SecretStore
from loreline.settings import Settings
from loreline.web.app import create_app

# Any model id: the fake backend never looks at it, but the API requires one -
# a provider row carries no model, so the request is where it is decided.
_MODEL = "fake-model"


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
            # And the job row records the model that actually ran.
            assert job["model"] == "nova-9000"


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
        diarizer_factory=lambda _cfg: FakeDiarizer(),
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
        diarizer_factory=lambda _cfg: FakeDiarizer(),
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
    """Poll a job until it leaves queued/running, and return the finished row."""
    job: dict[str, object] = {}
    for _ in range(50):
        job = (await client.get(f"/api/reprocess/{job_id}")).json()
        if job["status"] in {"done", "error"}:
            return job
        await asyncio.sleep(0.02)
    return job


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
        diarizer_factory=lambda _cfg: _WholeSessionDiarizer(),
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
        audio: AsyncIterator[Utterance],
        *,
        session_id: str,
        glossary: object = None,
    ) -> AsyncIterator[TranscriptEvent]:
        self._seen += 1
        if self._seen > 1:
            await self._gate.wait()
        async for event in super().transcribe(audio, session_id=session_id, glossary=glossary):
            yield event


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
        diarizer_factory=lambda _cfg: FakeDiarizer(),
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
        diarizer_factory=lambda _cfg: FakeDiarizer(),
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
