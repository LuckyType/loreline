"""WebSocket tests for live transcript + logs streaming."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from starlette.testclient import TestClient
from test_web_session import FakeBackend, FakeDiarizer, capture_factory

from loreline.settings import Settings
from loreline.web.app import create_app


@pytest.fixture
def ws_client(tmp_path: Path) -> Iterator[TestClient]:
    settings = Settings(data_dir=tmp_path / "data", auth_password="", jwt_secret="x")
    app = create_app(
        settings,
        capture_factory=capture_factory,  # type: ignore[arg-type]
        backend_factory=FakeBackend,  # type: ignore[arg-type]
        diarizer_factory=lambda _cfg: FakeDiarizer(),
    )
    with TestClient(app) as client:
        yield client


def _create_provider(client: TestClient) -> str:
    resp = client.post(
        "/api/providers",
        json={"name": "Fake", "kind": "openai_compat", "protocol": "http_batch"},
    )
    return resp.json()["id"]


def test_transcript_ws_streams_events(ws_client: TestClient) -> None:
    pid = _create_provider(ws_client)
    with ws_client.websocket_connect("/ws/transcript") as ws:
        ws_client.post("/api/session/start", json={"primary_provider": pid})
        payload = json.loads(ws.receive_text())
        assert payload["text"] == "hello world"
    ws_client.post("/api/session/stop")


def test_logs_ws_replays_history(ws_client: TestClient) -> None:
    # Startup logging already produced lines into the broadcaster buffer.
    with ws_client.websocket_connect("/ws/logs") as ws:
        line = ws.receive_text()
        assert isinstance(line, str)
        assert line
