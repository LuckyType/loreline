"""Tests for the full session lifecycle via the web API with injected fakes."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

from loreline.audio.chunker import SpeechDetector, Utterance
from loreline.models import (
    ProviderConfig,
    Session,
    SessionStatus,
    SpeakerSegment,
    TranscriptEvent,
)
from loreline.secrets import SecretStore
from loreline.settings import Settings
from loreline.web.app import create_app

_SAMPLE_RATE = 16000
_FRAME_BYTES = int(_SAMPLE_RATE * 0.02) * 2  # 20 ms of int16 mono


class FakeBackend:
    """STT backend emitting one final event per utterance."""

    def __init__(self, config: ProviderConfig, _secrets: SecretStore) -> None:
        self.config = config

    async def transcribe(
        self,
        audio: AsyncIterator[Utterance],
        *,
        session_id: str,
        glossary: object = None,
    ) -> AsyncIterator[TranscriptEvent]:
        _ = glossary
        async for utt in audio:
            yield TranscriptEvent(
                session_id=session_id,
                source=self.config.id,
                text="hello world",
                start_ts=utt.start,
                end_ts=utt.end,
                is_final=True,
            )

    async def health(self) -> bool:
        return True

    async def aclose(self) -> None:
        return None


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
    ) -> list[SpeakerSegment]:
        _ = (wav, sample_rate, min_speakers, max_speakers)
        return []

    async def aclose(self) -> None:
        return None


@pytest.fixture
def session_settings(tmp_path: Path) -> Settings:
    return Settings(data_dir=tmp_path / "data", auth_password="", jwt_secret="test-secret")


@pytest_asyncio.fixture
async def session_client(session_settings: Settings) -> AsyncIterator[AsyncClient]:
    app = create_app(
        session_settings,
        capture_factory=capture_factory,  # type: ignore[arg-type]
        backend_factory=FakeBackend,  # type: ignore[arg-type]
        diarizer_factory=lambda _cfg: FakeDiarizer(),
    )
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac


async def _create_provider(client: AsyncClient) -> str:
    resp = await client.post(
        "/api/providers",
        json={"name": "Fake", "kind": "openai_compat", "protocol": "http_batch"},
    )
    return resp.json()["id"]


async def test_session_lifecycle(session_client: AsyncClient) -> None:
    pid = await _create_provider(session_client)

    start = await session_client.post("/api/session/start", json={"primary_provider": pid})
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
    resp = await session_client.post("/api/session/start", json={"primary_provider": "missing"})
    assert resp.status_code == 404


async def test_double_start_conflicts(session_client: AsyncClient) -> None:
    pid = await _create_provider(session_client)
    first = await session_client.post("/api/session/start", json={"primary_provider": pid})
    assert first.status_code == 201
    second = await session_client.post("/api/session/start", json={"primary_provider": pid})
    assert second.status_code == 409
    await session_client.post("/api/session/stop")


async def test_start_overrides_model(session_settings: Settings) -> None:
    """A `model` in the start request overrides the provider's stored model."""
    captured: dict[str, str | None] = {}

    def factory(config: ProviderConfig, secrets: SecretStore) -> FakeBackend:
        captured["model"] = config.model
        return FakeBackend(config, secrets)

    app = create_app(
        session_settings,
        capture_factory=capture_factory,  # type: ignore[arg-type]
        backend_factory=factory,  # type: ignore[arg-type]
        diarizer_factory=lambda _cfg: FakeDiarizer(),
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


async def test_stop_without_session(session_client: AsyncClient) -> None:
    resp = await session_client.post("/api/session/stop")
    assert resp.status_code == 409


async def test_start_disabled_provider_conflicts(session_client: AsyncClient) -> None:
    pid = await _create_provider(session_client)
    disabled = await session_client.put(
        f"/api/providers/{pid}",
        json={"name": "Fake", "kind": "openai_compat", "protocol": "http_batch", "enabled": False},
    )
    assert disabled.status_code == 200
    resp = await session_client.post("/api/session/start", json={"primary_provider": pid})
    assert resp.status_code == 409
    assert "disabled" in resp.json()["detail"]


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
