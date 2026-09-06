"""What a live session does when the disk it records onto runs out.

Both halves, end to end through the API: the warning that arrives while there
is still time to act on it, and the session that ends *completed* with an
explanation rather than "error" with none - because the audio captured up to
the moment the disk filled is complete, and the GM has to be able to trust it.
"""

from __future__ import annotations

import asyncio
import errno
import json
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient
from test_web_session import FakeBackend, fake_diarizers

from loreline.monitoring.alerts import AlertChannel, AlertConfig, AlertLevel
from loreline.persistence import AudioStore, SessionAudioWriter
from loreline.settings import Settings
from loreline.web.app import AppState, create_app

_MODEL = "fake-model"
_FRAME_BYTES = int(16000 * 0.02) * 2  # 20 ms of int16 mono
_FRAME = b"\x01\x00" * (_FRAME_BYTES // 2)

Alert = dict[str, str]


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


class _FullDiskWriter(SessionAudioWriter):
    """A real writer whose filesystem fills up a few frames into the session."""

    def __init__(self, wav_path: Path, index_path: Path, *, sample_rate: int) -> None:
        super().__init__(wav_path, index_path, sample_rate=sample_rate)
        self._room_for = 5

    def append_frame(self, frame: bytes) -> None:
        if self._room_for <= 0:
            raise OSError(errno.ENOSPC, "No space left on device")
        self._room_for -= 1
        super().append_frame(frame)


class _FullDiskStore(AudioStore):
    """Hands the session the writer above, in place of the ordinary one."""

    def writer(self, session_id: str, *, sample_rate: int) -> SessionAudioWriter:
        return _FullDiskWriter(
            self.wav_path(session_id), self.index_path(session_id), sample_rate=sample_rate
        )


def _recording_alerts(sent: list[Alert]) -> Callable[[], httpx.AsyncClient]:
    """An alert client that files every push notification into ``sent``."""

    def handle(request: httpx.Request) -> httpx.Response:
        sent.append(cast("Alert", json.loads(request.content)))
        return httpx.Response(200)

    def factory() -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(handle))

    return factory


@asynccontextmanager
async def _client(
    settings: Settings, source: object, sent: list[Alert]
) -> AsyncGenerator[AsyncClient, None]:
    def detector(_frame: bytes) -> bool:
        return True

    def capture_factory(_req: object, _sample_rate: int) -> tuple[object, object]:
        return source, detector

    app = create_app(
        settings,
        capture_factory=capture_factory,  # type: ignore[arg-type]
        backend_factory=FakeBackend,  # type: ignore[arg-type]
        diarizer_factory=fake_diarizers,
        alert_client_factory=_recording_alerts(sent),
    )
    async with LifespanManager(app):
        state: AppState = app.state.ctx
        # One webhook channel gated at INFO, so every alert the session pushes
        # reaches ``sent`` rather than being filtered on its way out.
        channel = AlertChannel(
            id="w1", type="webhook", url="http://alerts.test/hook", min_level=AlertLevel.INFO
        )
        await state.alerts.set_config(AlertConfig(channels=[channel]))
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield client


async def _create_provider(client: AsyncClient) -> str:
    resp = await client.post("/api/providers", json={"name": "Fake", "kind": "openai_compat"})
    return str(resp.json()["id"])


async def _start_session(client: AsyncClient) -> str:
    pid = await _create_provider(client)
    start = await client.post("/api/session/start", json={"primary_provider": pid, "model": _MODEL})
    assert start.status_code == 201
    return str(start.json()["id"])


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


async def _await_alert(sent: list[Alert], title: str) -> Alert:
    """Wait for one pushed alert with this title (bounded)."""
    for _ in range(250):  # 5 s at 20 ms
        for alert in sent:
            if alert["title"] == title:
                return alert
        await asyncio.sleep(0.02)
    raise AssertionError(f"no {title!r} alert arrived: {sent}")


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(data_dir=tmp_path / "data", auth_password="", jwt_secret="test-secret")


async def test_a_recording_session_warns_before_the_disk_fills(tmp_path: Path) -> None:
    """The badge changing colour is not enough while nobody is looking at it."""
    tight = Settings(
        data_dir=tmp_path / "data",
        auth_password="",
        jwt_secret="test-secret",
        disk_alert_threshold_mb=10**9,  # a petabyte: no filesystem clears this
    )
    sent: list[Alert] = []
    async with _client(tight, _LiveSource(), sent) as client:
        await _start_session(client)

        alert = await _await_alert(sent, "Disk space low")
        assert "MB floor" in alert["message"]
        assert "recording left" in alert["message"]  # what the GM has to decide with
        assert alert["level"] == "warning"

        # A warning, not a stop: there is still room, so the recording goes on.
        assert (await client.get("/api/system/healthz")).json()["capture_status"] == "capturing"
        stop = await client.post("/api/session/stop")
        assert stop.status_code == 200
        assert stop.json()["status"] == "completed"

        # And exactly once, however many times the check ran.
        assert [a["title"] for a in sent].count("Disk space low") == 1


async def test_a_full_disk_ends_the_session_as_a_completed_recording(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The recording stopped; nothing about it is broken, and it says so."""
    monkeypatch.setattr("loreline.web.app.AudioStore", _FullDiskStore)
    sent: list[Alert] = []
    async with _client(settings, _LiveSource(), sent) as client:
        session_id = await _start_session(client)

        await _await_health(client, lambda h: h["capture_status"] == "idle", what="idle again")

        # The alert goes out last, after the session row is written, so waiting
        # for it is also waiting for the teardown to have finished.
        alert = await _await_alert(sent, "Recording stopped: disk full")
        assert "saved and complete" in alert["message"]
        assert alert["level"] == "error"

        # Completed, not error: what reached the disk is a complete recording,
        # and "session error" would say the opposite of that.
        detail = await client.get(f"/api/session/{session_id}")
        assert detail.json()["session"]["status"] == "completed"

        # Already finalized, so Stop has nothing left to stop.
        assert (await client.post("/api/session/stop")).status_code == 409
