"""Tests for the full session lifecycle via the web API with injected fakes."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from asgi_lifespan import LifespanManager
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from loreline.audio.chunker import SpeechDetector, Utterance
from loreline.diarization import openai_diarizer
from loreline.diarization.base import DiarizationProvider
from loreline.models import (
    REPROCESS_SOURCE_PREFIX,
    DiarizationConfig,
    JobStatus,
    ProviderConfig,
    ReprocessJob,
    Session,
    SessionStatus,
    SpeakerSegment,
    TranscriptEvent,
)
from loreline.secrets import SecretStore
from loreline.settings import Settings
from loreline.web.app import AppState, create_app

# Any model id: the fake backend never looks at it, but the API requires one -
# a provider row carries no model, so the request is where it is decided.
_MODEL = "fake-model"

_SAMPLE_RATE = 16000
_FRAME_BYTES = int(_SAMPLE_RATE * 0.02) * 2  # 20 ms of int16 mono


class FakeBackend:
    """STT backend emitting one final event per utterance.

    Takes the model the same way a real factory does (config, secrets, model),
    and keeps it so a test can assert which one the session resolved.
    """

    def __init__(
        self, config: ProviderConfig, _secrets: SecretStore, model: str | None = None
    ) -> None:
        self.config = config
        self.model = model

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
            text="hello world",
            start_ts=utterance.start,
            end_ts=utterance.end,
            is_final=True,
        )

    async def aclose(self) -> None:
        return None


class GlossaryRecordingBackend(FakeBackend):
    """FakeBackend that records the glossary handed to each transcribe call.

    The router is where the choice becomes observable: an opted-out run must
    reach the backend with ``glossary=None``, not with an empty one.
    """

    def __init__(
        self,
        config: ProviderConfig,
        secrets: SecretStore,
        model: str | None = None,
        seen: list[object] | None = None,
    ) -> None:
        super().__init__(config, secrets, model)
        self.seen: list[object] = seen if seen is not None else []

    async def transcribe(
        self,
        utterance: Utterance,
        *,
        session_id: str,
        glossary: object = None,
    ) -> TranscriptEvent | None:
        self.seen.append(glossary)
        return await super().transcribe(utterance, session_id=session_id, glossary=glossary)


class FakeSource:
    """Finite frame source emitting a fixed number of frames then stopping."""

    def __init__(self, *, frames: int = 5) -> None:
        self._frames = frames

    def stop(self) -> None:
        self._frames = 0

    async def frames(self) -> AsyncIterator[tuple[bytes, float]]:
        for i in range(self._frames):
            yield b"\x01\x00" * (_FRAME_BYTES // 2), i * 0.02


def capture_factory(_req: object, _sample_rate: int) -> tuple[FakeSource, SpeechDetector]:
    def detector(_frame: bytes) -> bool:
        return True

    return FakeSource(), detector


class FakeDiarizer:
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
        return []

    async def aclose(self) -> None:
        return None


async def fake_diarizers(_config: DiarizationConfig) -> DiarizationProvider:
    """Stands in for the app's diarizer factory: a fake for every mode."""
    return FakeDiarizer()


@pytest.fixture
def session_settings(tmp_path: Path) -> Settings:
    return Settings(data_dir=tmp_path / "data", auth_password="", jwt_secret="test-secret")


@pytest_asyncio.fixture
async def session_client(session_settings: Settings) -> AsyncIterator[AsyncClient]:
    app = create_app(
        session_settings,
        capture_factory=capture_factory,  # type: ignore[arg-type]
        backend_factory=FakeBackend,  # type: ignore[arg-type]
        diarizer_factory=fake_diarizers,
    )
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac


async def _create_provider(client: AsyncClient) -> str:
    resp = await client.post(
        "/api/providers",
        json={"name": "Fake", "kind": "openai_compat"},
    )
    return resp.json()["id"]


async def test_openai_diarization_runs_a_live_session_with_the_stored_key(
    session_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A live session in OpenAI diarization mode gets its key the way a
    reprocess job does: from the configured OpenAI provider row first, the
    environment second. The session path used to build its diarizer through a
    factory that knew nothing of the row, so only the env var ever reached it.
    No diarizer override here: this is the app's own factory."""
    built: list[str | None] = []

    class Recording(FakeDiarizer):
        def __init__(self, *, api_key: str | None = None, **_: object) -> None:
            built.append(api_key)

    monkeypatch.setattr(openai_diarizer, "OpenAIDiarizer", Recording)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-from-env")
    app = create_app(
        session_settings,
        capture_factory=capture_factory,  # type: ignore[arg-type]
        backend_factory=FakeBackend,  # type: ignore[arg-type]
    )
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            pid = await _create_provider(client)
            stored = await client.post(
                "/api/providers",
                json={"name": "OpenAI", "kind": "openai", "api_key": "sk-from-store"},
            )
            assert stored.status_code == 201
            start = await client.post(
                "/api/session/start",
                json={
                    "primary_provider": pid,
                    "model": _MODEL,
                    "diarization": {"mode": "openai"},
                },
            )
            assert start.status_code == 201
            await client.post("/api/session/stop")
    assert built == ["sk-from-store"]


async def test_session_lifecycle(session_client: AsyncClient) -> None:
    pid = await _create_provider(session_client)

    start = await session_client.post(
        "/api/session/start", json={"primary_provider": pid, "model": _MODEL}
    )
    assert start.status_code == 201
    session_id = start.json()["id"]

    health = await session_client.get("/api/system/healthz")
    assert health.json()["capture_status"] in {"capturing", "idle"}

    stop = await session_client.post("/api/session/stop")
    assert stop.status_code == 200
    assert stop.json()["status"] == "completed"

    detail = await session_client.get(f"/api/session/{session_id}")
    assert detail.status_code == 200
    body = detail.json()
    assert body["session"]["id"] == session_id
    assert len(body["transcript"]) == 1
    assert body["transcript"][0]["text"] == "hello world"


async def test_start_unknown_provider(session_client: AsyncClient) -> None:
    resp = await session_client.post(
        "/api/session/start", json={"primary_provider": "missing", "model": _MODEL}
    )
    assert resp.status_code == 404


async def test_double_start_conflicts(session_client: AsyncClient) -> None:
    pid = await _create_provider(session_client)
    first = await session_client.post(
        "/api/session/start", json={"primary_provider": pid, "model": _MODEL}
    )
    assert first.status_code == 201
    second = await session_client.post(
        "/api/session/start", json={"primary_provider": pid, "model": _MODEL}
    )
    assert second.status_code == 409
    await session_client.post("/api/session/stop")


async def test_start_names_the_model_for_the_backend(session_settings: Settings) -> None:
    """The start request's model is what the connector is built with.

    It is also the only source now: the provider row holds none, so this is
    where a session's model is decided, full stop."""
    captured: dict[str, str | None] = {}

    def factory(config: ProviderConfig, secrets: SecretStore, model: str | None) -> FakeBackend:
        captured["model"] = model
        return FakeBackend(config, secrets, model)

    app = create_app(
        session_settings,
        capture_factory=capture_factory,  # type: ignore[arg-type]
        backend_factory=factory,  # type: ignore[arg-type]
        diarizer_factory=fake_diarizers,
    )
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            pid = await _create_provider(ac)
            start = await ac.post(
                "/api/session/start", json={"primary_provider": pid, "model": "nova-9000"}
            )
            assert start.status_code == 201
            assert captured["model"] == "nova-9000"
            await ac.post("/api/session/stop")


async def test_start_names_the_fallback_model_separately(session_settings: Settings) -> None:
    """`fallback_model` names the *fallback* provider's model, independently of
    the primary's - the two providers have separate model lists, so one pick
    cannot serve both."""
    seen: list[str | None] = []

    def factory(config: ProviderConfig, secrets: SecretStore, model: str | None) -> FakeBackend:
        seen.append(model)
        return FakeBackend(config, secrets, model)

    app = create_app(
        session_settings,
        capture_factory=capture_factory,  # type: ignore[arg-type]
        backend_factory=factory,  # type: ignore[arg-type]
        diarizer_factory=fake_diarizers,
    )
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            primary_id = await _create_provider(ac)
            fallback_id = await _create_provider(ac)
            start = await ac.post(
                "/api/session/start",
                json={
                    "primary_provider": primary_id,
                    "fallback_provider": fallback_id,
                    "model": "primary-model",
                    "fallback_model": "fallback-model",
                },
            )
            assert start.status_code == 201
            assert seen == ["primary-model", "fallback-model"]
            await ac.post("/api/session/stop")


async def test_start_applies_the_glossary_unless_switched_off(session_settings: Settings) -> None:
    """`use_glossary` defaults to on (the behaviour before the option existed);
    off means the backend is handed no glossary at all, not an empty one."""
    seen: list[object] = []

    def factory(
        config: ProviderConfig, secrets: SecretStore, model: str | None
    ) -> GlossaryRecordingBackend:
        return GlossaryRecordingBackend(config, secrets, model, seen)

    app = create_app(
        session_settings,
        capture_factory=capture_factory,  # type: ignore[arg-type]
        backend_factory=factory,  # type: ignore[arg-type]
        diarizer_factory=fake_diarizers,
    )
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            pid = await _create_provider(ac)
            await ac.put("/api/glossary", json={"terms": ["Drakonia"]})

            await ac.post("/api/session/start", json={"primary_provider": pid, "model": _MODEL})
            await ac.post("/api/session/stop")
            assert seen, "the backend was never asked to transcribe"
            assert [getattr(g, "terms", None) for g in seen] == [["Drakonia"]] * len(seen)

            seen.clear()
            await ac.post(
                "/api/session/start",
                json={"primary_provider": pid, "model": _MODEL, "use_glossary": False},
            )
            await ac.post("/api/session/stop")
            assert seen, "the backend was never asked to transcribe"
            assert all(g is None for g in seen)


async def test_a_campaign_session_is_transcribed_with_its_terms(
    session_settings: Settings,
) -> None:
    """The session's own campaign's terms, then the default list.

    The merge itself is ``GlossaryRepository.get_effective``'s; what this covers
    is that the campaign a session was *started in* is the one whose terms are
    asked for, which is the whole point of picking one on the capture card. A
    second campaign's terms are seeded too, so a merge that read every campaign
    rather than this session's would fail here rather than pass by accident.
    """
    seen: list[object] = []

    def factory(
        config: ProviderConfig, secrets: SecretStore, model: str | None
    ) -> GlossaryRecordingBackend:
        return GlossaryRecordingBackend(config, secrets, model, seen)

    app = create_app(
        session_settings,
        capture_factory=capture_factory,  # type: ignore[arg-type]
        backend_factory=factory,  # type: ignore[arg-type]
        diarizer_factory=fake_diarizers,
    )
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            pid = await _create_provider(ac)
            await ac.put("/api/glossary", json={"terms": ["Aurora"]})
            ours = (await ac.post("/api/campaigns", json={"name": "Barovia"})).json()["id"]
            theirs = (await ac.post("/api/campaigns", json={"name": "Sigil"})).json()["id"]
            await ac.put(f"/api/glossary/{ours}", json={"terms": ["Strahd"]})
            await ac.put(f"/api/glossary/{theirs}", json={"terms": ["Lady of Pain"]})

            await ac.post(
                "/api/session/start",
                json={"primary_provider": pid, "model": _MODEL, "campaign_id": ours},
            )
            await ac.post("/api/session/stop")

            assert seen, "the backend was never asked to transcribe"
            # The campaign's own term leads the always-on list now: the ceiling
            # is spent from the head, and a term somebody put on this campaign
            # is likelier to be in this audio than one on the global list.
            assert [getattr(g, "terms", None) for g in seen] == [["Strahd", "Aurora"]] * len(seen)


async def test_stop_without_session(session_client: AsyncClient) -> None:
    resp = await session_client.post("/api/session/stop")
    assert resp.status_code == 409


async def test_start_disabled_provider_conflicts(session_client: AsyncClient) -> None:
    pid = await _create_provider(session_client)
    disabled = await session_client.put(
        f"/api/providers/{pid}",
        json={"name": "Fake", "kind": "openai_compat", "enabled": False},
    )
    assert disabled.status_code == 200
    resp = await session_client.post(
        "/api/session/start", json={"primary_provider": pid, "model": _MODEL}
    )
    assert resp.status_code == 409
    assert "disabled" in resp.json()["detail"]


async def test_merge_concatenates_audio(tmp_path: Path) -> None:
    """Merging sessions that all have stored audio also merges the WAVs, so
    the merged session stays re-processable and downloadable."""
    settings = Settings(data_dir=tmp_path / "data", auth_password="", jwt_secret="t")
    app = create_app(
        settings,
        capture_factory=capture_factory,  # type: ignore[arg-type]
        backend_factory=FakeBackend,  # type: ignore[arg-type]
        diarizer_factory=fake_diarizers,
    )
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/providers",
                json={"name": "Fake", "kind": "openai_compat"},
            )
            pid = resp.json()["id"]

            ids: list[str] = []
            for _ in range(2):
                start = await client.post(
                    "/api/session/start", json={"primary_provider": pid, "model": _MODEL}
                )
                ids.append(start.json()["id"])
                await client.post("/api/session/stop")

            ctx = app.state.ctx  # pyright: ignore[reportAny]
            store = ctx.audio_store
            part_durations = [store.duration_s(i) for i in ids]
            part_counts = [len(store.read_utterances(i)) for i in ids]

            merged = (await client.post("/api/session/merge", json={"ids": ids})).json()
            assert merged["audio_path"]

            # The merged WAV is the parts back to back, and the merged index
            # carries every utterance with the later part shifted past the
            # first part's audio.
            assert abs(store.duration_s(merged["id"]) - sum(part_durations)) < 1e-6
            utterances = store.read_utterances(merged["id"])
            assert len(utterances) == sum(part_counts)
            assert utterances[part_counts[0]].start >= part_durations[0]

            audio = await client.get(f"/api/session/{merged['id']}/audio")
            assert audio.status_code == 200
            assert audio.content[:4] == b"RIFF"

            # ...which makes the merged session re-processable end to end.
            enqueue = await client.post(
                "/api/reprocess",
                json={"session_id": merged["id"], "provider_id": pid, "model": _MODEL},
            )
            assert enqueue.status_code == 202
            job_id = enqueue.json()["id"]
            await ctx.reprocess.wait(job_id)
            job = (await client.get(f"/api/reprocess/{job_id}")).json()
            assert job["status"] == "done"
            assert int(job["segments_added"]) == len(utterances)


async def test_session_detail_reports_audio_duration(session_client: AsyncClient) -> None:
    """GET /api/session/{id} reports the stored WAV's length, computed off the
    file itself rather than kept on the session row - a stored value would go
    stale while a live capture keeps growing the WAV."""
    pid = await _create_provider(session_client)
    start = await session_client.post(
        "/api/session/start", json={"primary_provider": pid, "model": _MODEL}
    )
    session_id = start.json()["id"]
    await session_client.post("/api/session/stop")

    detail = (await session_client.get(f"/api/session/{session_id}")).json()
    assert detail["audio_duration_s"] is not None
    assert detail["audio_duration_s"] > 0


def _silent_capture_factory(_req: object, _sample_rate: int) -> tuple[FakeSource, SpeechDetector]:
    """Like capture_factory, but the source ends without ever yielding a
    frame - nothing is appended to the session's WAV, matching a session that
    errored before capturing any audio. The WAV (and its audio_path) still get
    written, just as a bare header."""

    def detector(_frame: bytes) -> bool:
        return True

    return FakeSource(frames=0), detector


async def test_session_detail_reports_zero_duration_for_a_headeronly_wav(
    tmp_path: Path,
) -> None:
    """A session whose capture produced no frames at all still gets a WAV and
    an audio_path - just a bare header, indistinguishable from a real
    recording by path alone. The reported duration says so as 0, which is
    what lets the frontend tell the two apart."""
    settings = Settings(data_dir=tmp_path / "data", auth_password="", jwt_secret="t")
    app = create_app(
        settings,
        capture_factory=_silent_capture_factory,  # type: ignore[arg-type]
        backend_factory=FakeBackend,  # type: ignore[arg-type]
        diarizer_factory=fake_diarizers,
    )
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            pid = await _create_provider(client)
            start = await client.post(
                "/api/session/start", json={"primary_provider": pid, "model": _MODEL}
            )
            session_id = start.json()["id"]
            await client.post("/api/session/stop")

            detail = (await client.get(f"/api/session/{session_id}")).json()
            assert detail["session"]["audio_path"]
            assert detail["audio_duration_s"] == 0


class HangingBackend(FakeBackend):
    """Accepts the utterance, then never answers - a black-holed provider."""

    async def transcribe(
        self,
        utterance: Utterance,
        *,
        session_id: str,
        glossary: object = None,
    ) -> TranscriptEvent | None:
        _ = (utterance, session_id, glossary)
        await asyncio.sleep(3600)
        return None


async def test_stop_returns_despite_hung_backend(
    session_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stop must not be held hostage by a dead backend draining its backlog:
    after the drain timeout the router is cancelled and the session still
    completes - its audio is on disk for later re-transcription."""
    monkeypatch.setattr("loreline.session.manager._STOP_DRAIN_TIMEOUT_S", 0.3)
    app = create_app(
        session_settings,
        capture_factory=capture_factory,  # type: ignore[arg-type]
        backend_factory=HangingBackend,  # type: ignore[arg-type]
        diarizer_factory=fake_diarizers,
    )
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            pid = await _create_provider(client)
            start = await client.post(
                "/api/session/start", json={"primary_provider": pid, "model": _MODEL}
            )
            session_id = start.json()["id"]
            await asyncio.sleep(0.1)  # let capture hand the utterance to the hung backend

            began = time.monotonic()
            stop = await client.post("/api/session/stop")
            assert stop.status_code == 200
            assert stop.json()["status"] == "completed"
            assert time.monotonic() - began < 5.0

            ctx = app.state.ctx  # pyright: ignore[reportAny]
            assert ctx.audio_store.exists(session_id)  # recording stayed adoptable


async def test_startup_rebuilds_orphaned_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A WAV left without its index sidecar (unclean death) is adopted at the
    next startup: the background sweep re-runs VAD and restores the index; a
    stray WAV with no session row is left alone."""

    def any_byte_detector(_sample_rate: int) -> SpeechDetector:
        return any  # a frame with any nonzero byte counts as speech

    monkeypatch.setattr("loreline.session.recovery._default_detector", any_byte_detector)
    settings = Settings(data_dir=tmp_path / "data", auth_password="", jwt_secret="t")

    def make_app() -> FastAPI:
        return create_app(
            settings,
            capture_factory=capture_factory,  # type: ignore[arg-type]
            backend_factory=FakeBackend,  # type: ignore[arg-type]
            diarizer_factory=fake_diarizers,
        )

    app = make_app()
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            pid = await _create_provider(client)
            start = await client.post(
                "/api/session/start", json={"primary_provider": pid, "model": _MODEL}
            )
            session_id = start.json()["id"]
            await client.post("/api/session/stop")
        store = app.state.ctx.audio_store  # pyright: ignore[reportAny]
        original = store.read_utterances(session_id)
        store.index_path(session_id).unlink()  # simulate a pre-sidecar crash
        with store.writer("stray", sample_rate=16000) as writer:  # no session row
            writer.append_frame(b"\x01\x00" * 320)
        store.index_path("stray").unlink()

    app = make_app()
    async with LifespanManager(app):
        store = app.state.ctx.audio_store  # pyright: ignore[reportAny]
        for _ in range(100):
            if store.exists(session_id):
                break
            await asyncio.sleep(0.05)
        rebuilt = store.read_utterances(session_id)
        assert len(rebuilt) >= 1
        assert len(rebuilt) == len(original)
        assert not store.index_path("stray").exists()


async def test_merge_and_delete_sessions(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "d", auth_password="", jwt_secret="t")
    app = create_app(settings)
    async with LifespanManager(app):
        ctx = app.state.ctx  # pyright: ignore[reportAny]
        await ctx.sessions.create(Session(id="a", status=SessionStatus.COMPLETED, started_at=100.0))
        await ctx.sessions.create(Session(id="b", status=SessionStatus.COMPLETED, started_at=200.0))
        await ctx.transcripts.add(
            TranscriptEvent(
                session_id="a",
                source="p",
                text="part a",
                speaker="Speaker A",
                start_ts=0.0,
                end_ts=5.0,
                is_final=True,
            )
        )
        await ctx.transcripts.add(
            TranscriptEvent(
                session_id="b", source="p", text="part b", start_ts=0.0, end_ts=3.0, is_final=True
            )
        )
        await ctx.sessions.set_speaker_names("a", {"Speaker A": "GM"})

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            merged = (await client.post("/api/session/merge", json={"ids": ["b", "a"]})).json()
            assert merged["speaker_names"] == {"Speaker A": "GM"}  # maps unioned
            detail = (await client.get(f"/api/session/{merged['id']}")).json()
            # a starts at 0; b is shifted past a's 5 s span -> back-to-back, oldest first
            assert sorted(e["start_ts"] for e in detail["transcript"]) == [0.0, 5.0]

            assert (await client.post("/api/session/merge", json={"ids": ["a"]})).status_code == 409

            assert (
                await client.post("/api/session/delete", json={"ids": ["a", "b"]})
            ).status_code == 200
            ids = {s["id"] for s in (await client.get("/api/session")).json()}
            assert "a" not in ids and "b" not in ids  # originals deleted
            assert merged["id"] in ids  # merged session kept


class OutOfCreditBackend(FakeBackend):
    """Answers every utterance with OpenAI's real "no credits remaining" 429."""

    async def transcribe(
        self,
        utterance: Utterance,
        *,
        session_id: str,
        glossary: object = None,
    ) -> TranscriptEvent | None:
        _ = (utterance, session_id, glossary)
        request = httpx.Request("POST", "https://api.openai.com/v1/audio/transcriptions")
        response = httpx.Response(
            429,
            json={
                "error": {
                    "message": (
                        "You have no credits remaining. Add credits to continue using "
                        "the API at https://platform.openai.com/settings/organization/"
                        "billing/."
                    ),
                    "type": "insufficient_quota",
                    "code": "insufficient_quota",
                }
            },
            request=request,
        )
        raise httpx.HTTPStatusError("429", request=request, response=response)


async def test_capture_keeps_recording_when_transcription_dies(
    session_settings: Settings,
) -> None:
    """A provider that will never answer again stops transcription, not capture.

    The audio is the artifact that cannot be produced a second time, so the
    session degrades to a recorder and stays re-transcribable; what the GM gets
    instead of a silently empty transcript is the vendor's own sentence, on
    /healthz and therefore on the dashboard.
    """
    app = create_app(
        session_settings,
        capture_factory=capture_factory,  # type: ignore[arg-type]
        backend_factory=OutOfCreditBackend,  # type: ignore[arg-type]
        diarizer_factory=fake_diarizers,
    )
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            pid = await _create_provider(client)
            start = await client.post(
                "/api/session/start", json={"primary_provider": pid, "model": _MODEL}
            )
            session_id = start.json()["id"]

            reason = None
            for _ in range(50):
                reason = (await client.get("/api/system/healthz")).json()["stt_error"]
                if reason:
                    break
                await asyncio.sleep(0.02)
            assert reason is not None
            assert "no credits remaining" in reason

            stop = await client.post("/api/session/stop")
            # Still a completed capture: the recording is intact and the whole
            # session can be re-transcribed once the account is topped up.
            assert stop.json()["status"] == "completed"
            ctx = app.state.ctx  # pyright: ignore[reportAny]
            assert ctx.audio_store.exists(session_id)
            assert (await client.get("/api/system/healthz")).json()["stt_error"] is None


async def _row(ctx: AppState, session_id: str, source: str, text: str, end: float) -> None:
    await ctx.transcripts.add(
        TranscriptEvent(
            session_id=session_id,
            source=source,
            text=text,
            start_ts=0.0,
            end_ts=end,
            is_final=True,
        )
    )


async def _done_job(ctx: AppState, session_id: str, job_id: str, created_at: float) -> None:
    await ctx.reprocess_jobs.create(
        ReprocessJob(
            id=job_id,
            session_id=session_id,
            provider_id="p",
            model="nova-9000",
            status=JobStatus.DONE,
            created_at=created_at,
        )
    )


async def test_merge_takes_each_source_s_newest_re_transcription(tmp_path: Path) -> None:
    """A merge is a GM saying "make this one session", so each part
    contributes the best text it has: its newest completed re-transcription
    that produced segments, and its live capture only when it has none.

    Merging used to copy every source's original unconditionally, which threw
    away every re-transcription of every part - the exact thing the parts were
    re-transcribed for - and left nothing on the merged row to say so.
    """
    settings = Settings(data_dir=tmp_path / "d", auth_password="", jwt_secret="t")
    app = create_app(settings)
    async with LifespanManager(app):
        ctx = app.state.ctx  # pyright: ignore[reportAny]
        await ctx.sessions.create(Session(id="a", status=SessionStatus.COMPLETED, started_at=100.0))
        await ctx.sessions.create(Session(id="b", status=SessionStatus.COMPLETED, started_at=200.0))

        await _row(ctx, "a", "p", "a original", 5.0)
        await _row(ctx, "a", f"{REPROCESS_SOURCE_PREFIX}older", "a older", 4.0)
        await _row(ctx, "a", f"{REPROCESS_SOURCE_PREFIX}newer", "a newer", 6.0)
        # A job that finished having written nothing, and a job that failed
        # after writing some: newer than the winner, and neither is an
        # improvement on it.
        await _row(ctx, "a", f"{REPROCESS_SOURCE_PREFIX}broken", "a broken", 9.0)
        await _done_job(ctx, "a", "older", 10.0)
        await _done_job(ctx, "a", "newer", 20.0)
        await _done_job(ctx, "a", "empty", 30.0)
        await ctx.reprocess_jobs.create(
            ReprocessJob(
                id="broken",
                session_id="a",
                provider_id="p",
                status=JobStatus.ERROR,
                created_at=40.0,
            )
        )
        # b was never re-transcribed at all, so its live capture is its best.
        await _row(ctx, "b", "p", "b original", 3.0)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            merged = (await client.post("/api/session/merge", json={"ids": ["b", "a"]})).json()

            detail = (await client.get(f"/api/session/{merged['id']}")).json()
            rows = sorted(detail["transcript"], key=lambda e: e["start_ts"])  # pyright: ignore[reportAny]
            assert [e["text"] for e in rows] == ["a newer", "b original"]
            # b is shifted past a's chosen version, not past a's original.
            assert [e["start_ts"] for e in rows] == [0.0, 6.0]

            # The merged rows are the merged session's own live text: a
            # `reprocess:<job id>` tag carried over would name a job belonging
            # to another session, and the merged session's default view would
            # then be empty. Each is stamped with the provider that produced
            # it: the job's for a's re-transcription, and b's own id for a
            # capture with no provider recorded.
            assert [e["source"] for e in rows] == ["p", "b"]

            # A merge never has a stop button pressed, so nothing else was
            # going to fill ended_at in and the header showed a start time and
            # no duration. It runs to the end of the merged timeline.
            assert merged["ended_at"] == 100.0 + 6.0 + 3.0

            # And the row says what it was made from, which is the only thing
            # that tells it apart from its oldest source in the history list.
            assert merged["merged_from"] == ["a", "b"]


async def test_merge_stamps_each_part_with_the_provider_that_produced_it(tmp_path: Path) -> None:
    """A merged row's source is a provider id, and it has to be the right one.

    Every part was stamped with its session's primary provider whatever text
    it contributed, so a session captured with A and re-transcribed with B
    landed in the merge saying A had heard words it never did. The stamp is
    the re-transcription job's provider when that is the text taken, and the
    capture's when it is not.
    """
    settings = Settings(data_dir=tmp_path / "d", auth_password="", jwt_secret="t")
    app = create_app(settings)
    async with LifespanManager(app):
        ctx = app.state.ctx  # pyright: ignore[reportAny]
        for sid, started in (("a", 100.0), ("b", 200.0), ("c", 300.0)):
            await ctx.sessions.create(
                Session(
                    id=sid,
                    status=SessionStatus.COMPLETED,
                    started_at=started,
                    primary_provider="live",
                )
            )
        # a and c were re-transcribed with a better provider; b never was.
        await _row(ctx, "a", "live", "a original", 5.0)
        await _row(ctx, "a", f"{REPROCESS_SOURCE_PREFIX}job-a", "a better", 5.0)
        await _row(ctx, "b", "live", "b original", 3.0)
        await _row(ctx, "c", "live", "c original", 4.0)
        await _row(ctx, "c", f"{REPROCESS_SOURCE_PREFIX}job-c", "c better", 4.0)
        for sid, job_id in (("a", "job-a"), ("c", "job-c")):
            await ctx.reprocess_jobs.create(
                ReprocessJob(
                    id=job_id,
                    session_id=sid,
                    provider_id="better",
                    model="nova-9000",
                    status=JobStatus.DONE,
                    created_at=10.0,
                )
            )

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            merged = (await client.post("/api/session/merge", json={"ids": ["c", "a", "b"]})).json()
            detail = (await client.get(f"/api/session/{merged['id']}")).json()
            rows = sorted(detail["transcript"], key=lambda e: e["start_ts"])  # pyright: ignore[reportAny]

            assert [(e["text"], e["source"]) for e in rows] == [
                ("a better", "better"),
                ("b original", "live"),
                ("c better", "better"),
            ]
            # Two parts stamped with the same provider both land. The stamp is
            # part of the transcript's (session, source, turn) upsert key, and
            # the turn id the merge drops is what keeps settled copies from
            # colliding on it.
            assert len(rows) == 3


async def test_transcript_version_lookup_tells_a_typo_from_a_run_in_flight(tmp_path: Path) -> None:
    """An unknown version is a 404; a queued re-transcription is an empty 200.

    The transcript route answered every misspelt version id with an empty
    list, which looks exactly like a version that captured nothing. The one
    version that legitimately has no rows yet is a re-transcription still in
    flight: the session page lists it and lets the GM select it the moment it
    is queued, so that one has to stay readable, on export as well.
    """
    settings = Settings(data_dir=tmp_path / "d", auth_password="", jwt_secret="t")
    app = create_app(settings)
    async with LifespanManager(app):
        ctx = app.state.ctx  # pyright: ignore[reportAny]
        await ctx.sessions.create(Session(id="a", status=SessionStatus.COMPLETED, started_at=100.0))
        await ctx.sessions.create(Session(id="b", status=SessionStatus.COMPLETED, started_at=200.0))
        await _row(ctx, "a", "p", "a original", 5.0)
        await ctx.reprocess_jobs.create(
            ReprocessJob(
                id="queued",
                session_id="a",
                provider_id="p",
                status=JobStatus.QUEUED,
                created_at=10.0,
            )
        )
        # A diarize job relabels a version; it is not one.
        await ctx.reprocess_jobs.create(
            ReprocessJob(
                id="relabel",
                session_id="a",
                provider_id="p",
                operation="diarize",
                status=JobStatus.QUEUED,
                created_at=11.0,
            )
        )

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            missing = await client.get("/api/session/a/transcript", params={"version": "nope"})
            assert missing.status_code == 404
            assert "nope" in missing.json()["detail"]
            relabel = await client.get("/api/session/a/transcript", params={"version": "relabel"})
            assert relabel.status_code == 404

            queued = await client.get("/api/session/a/transcript", params={"version": "queued"})
            assert queued.status_code == 200
            assert queued.json() == []
            export = await client.get(
                "/api/session/a/export", params={"fmt": "txt", "version": "queued"}
            )
            assert export.status_code == 200

            # A job is a version of the session it belongs to and of no other.
            other = await client.get("/api/session/b/transcript", params={"version": "queued"})
            assert other.status_code == 404

            # The original is always a version a session has, rows or none.
            assert (await client.get("/api/session/b/transcript")).status_code == 200


async def test_merge_ends_where_the_merged_audio_does(tmp_path: Path) -> None:
    """With merged audio the parts advance by their audio length, so that is
    what the merged session runs to - the transcript may stop short of it."""
    settings = Settings(data_dir=tmp_path / "data", auth_password="", jwt_secret="t")
    app = create_app(
        settings,
        capture_factory=capture_factory,  # type: ignore[arg-type]
        backend_factory=FakeBackend,  # type: ignore[arg-type]
        diarizer_factory=fake_diarizers,
    )
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            pid = await _create_provider(client)
            ids: list[str] = []
            for _ in range(2):
                start = await client.post(
                    "/api/session/start", json={"primary_provider": pid, "model": _MODEL}
                )
                ids.append(start.json()["id"])
                await client.post("/api/session/stop")

            store = app.state.ctx.audio_store  # pyright: ignore[reportAny]
            total = sum(store.duration_s(i) for i in ids)
            merged = (await client.post("/api/session/merge", json={"ids": ids})).json()

            assert merged["ended_at"] is not None
            assert abs(merged["ended_at"] - (merged["started_at"] + total)) < 1e-6
