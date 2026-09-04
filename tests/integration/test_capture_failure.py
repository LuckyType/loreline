"""What the API does when the microphone is the thing that fails.

The bug behind these tests: a device that could not be opened left a session
reporting ``capturing`` with a ticking timer and a WAV stuck at its 44-byte
header, for as long as the GM left it running, because the failure happened
behind an already-answered start request and nothing was watching the task it
happened in.

Three defences, one per test: the start request fails instead of pretending, a
capture that dies mid-session ends the session by itself, and health reports how
much audio has actually arrived so a stalled capture is visible while it stalls.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from asgi_lifespan import LifespanManager
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from test_web_session import FakeBackend, fake_diarizers

from loreline.settings import Settings
from loreline.web.app import create_app

_MODEL = "fake-model"
_FRAME_BYTES = int(16000 * 0.02) * 2  # 20 ms of int16 mono
_FRAME = b"\x01\x00" * (_FRAME_BYTES // 2)


class _UnopenableSource:
    """A device that refuses to open - what the pre-flight is there to catch."""

    async def preflight(self) -> None:
        msg = "Invalid sample rate [PaErrorCode -9997]"
        raise RuntimeError(msg)

    def stop(self) -> None:
        return None

    async def frames(self) -> AsyncIterator[tuple[bytes, float]]:
        raise AssertionError("capture must never start after a failed pre-flight")
        yield _FRAME, 0.0  # pragma: no cover - unreachable; makes this a generator


class _DyingSource:
    """A device unplugged a few frames into the session."""

    def __init__(self, *, frames_before_death: int = 3) -> None:
        self._n = frames_before_death

    def stop(self) -> None:
        return None

    async def frames(self) -> AsyncIterator[tuple[bytes, float]]:
        for i in range(self._n):
            await asyncio.sleep(0)
            yield _FRAME, i * 0.02
        msg = "device disconnected"
        raise RuntimeError(msg)


class _LiveSource:
    """A healthy device: half a second of audio, then quiet until stopped."""

    def __init__(self, *, frames: int = 25) -> None:
        self._n = frames
        self._stopped = asyncio.Event()

    def stop(self) -> None:
        self._stopped.set()

    async def frames(self) -> AsyncIterator[tuple[bytes, float]]:
        for i in range(self._n):
            await asyncio.sleep(0)
            yield _FRAME, i * 0.02
        await self._stopped.wait()


def _app(settings: Settings, source: object) -> FastAPI:
    def detector(_frame: bytes) -> bool:
        return True

    def capture_factory(_req: object, _sample_rate: int) -> tuple[object, object]:
        return source, detector

    return create_app(
        settings,
        capture_factory=capture_factory,  # type: ignore[arg-type]
        backend_factory=FakeBackend,  # type: ignore[arg-type]
        diarizer_factory=fake_diarizers,
    )


@asynccontextmanager
async def _client(settings: Settings, source: object) -> AsyncGenerator[AsyncClient, None]:
    app = _app(settings, source)
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield client


async def _create_provider(client: AsyncClient) -> str:
    resp = await client.post("/api/providers", json={"name": "Fake", "kind": "openai_compat"})
    return str(resp.json()["id"])


async def _await_health(
    client: AsyncClient, ready: Callable[[dict[str, Any]], bool], *, what: str
) -> dict[str, Any]:
    """Poll health until it shows what the test is waiting for (bounded)."""
    health: dict[str, Any] = {}
    for _ in range(250):  # 5 s at 20 ms
        health = (await client.get("/api/system/healthz")).json()
        if ready(health):
            return health
        await asyncio.sleep(0.02)
    raise AssertionError(f"health never reported {what}: {health}")


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(data_dir=tmp_path / "data", auth_password="", jwt_secret="test-secret")


async def test_start_rejects_a_microphone_that_cannot_be_opened(settings: Settings) -> None:
    """A broken mic is a failed start request, not a session that records nothing."""
    async with _client(settings, _UnopenableSource()) as client:
        pid = await _create_provider(client)
        start = await client.post(
            "/api/session/start", json={"primary_provider": pid, "model": _MODEL}
        )

        assert start.status_code == 400
        detail = start.json()["detail"]
        assert "could not be opened" in detail
        assert "PaErrorCode" in detail  # the device's own words, not a paraphrase

        # Nothing was started, so there is nothing to clean up afterwards.
        assert (await client.get("/api/session")).json() == []
        assert (await client.get("/api/system/healthz")).json()["capture_status"] == "idle"


async def test_a_dead_capture_ends_its_own_session(settings: Settings) -> None:
    """A capture that dies mid-session finalizes as an error, unprompted.

    Before, the queue's sentinel was skipped when the frame source raised, so
    the router waited on it forever: the session stayed ``capturing`` until
    someone pressed Stop, and even then only ended 30 seconds later, by timeout.
    """
    async with _client(settings, _DyingSource()) as client:
        pid = await _create_provider(client)
        start = await client.post(
            "/api/session/start", json={"primary_provider": pid, "model": _MODEL}
        )
        assert start.status_code == 201
        session_id = start.json()["id"]

        await _await_health(client, lambda h: h["capture_status"] == "idle", what="idle again")

        detail = await client.get(f"/api/session/{session_id}")
        assert detail.json()["session"]["status"] == "error"
        # Already finalized, so Stop has nothing left to stop.
        assert (await client.post("/api/session/stop")).status_code == 409


async def test_health_reports_the_audio_that_arrived(settings: Settings) -> None:
    """The numbers that tell a live recording from a stalled one."""
    async with _client(settings, _LiveSource()) as client:
        pid = await _create_provider(client)
        start = await client.post(
            "/api/session/start", json={"primary_provider": pid, "model": _MODEL}
        )
        assert start.status_code == 201

        health = await _await_health(
            client,
            lambda h: (h["captured_seconds"] or 0) >= 0.5,
            what="captured audio",
        )
        assert health["capture_status"] == "capturing"
        assert health["captured_seconds"] == 0.5  # 25 frames of 20 ms, exactly
        assert health["capture_last_frame_age"] < 5.0

        stop = await client.post("/api/session/stop")
        assert stop.status_code == 200
        assert stop.json()["status"] == "completed"

        idle = (await client.get("/api/system/healthz")).json()
        assert idle["captured_seconds"] is None
        assert idle["capture_last_frame_age"] is None
