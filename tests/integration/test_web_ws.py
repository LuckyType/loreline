"""WebSocket tests for live transcript + logs streaming."""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from starlette.testclient import TestClient
from test_web_session import FakeBackend, FakeDiarizer, capture_factory

from loreline.settings import Settings
from loreline.web.app import create_app

# Any model id: the fake backend never looks at it, but the API requires one -
# a provider row carries no model, so the request is where it is decided.
_MODEL = "fake-model"


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


def _await_job(client: TestClient, job_id: str) -> dict[str, object]:
    """Poll a re-processing job to completion (bounded)."""
    for _ in range(100):
        job: dict[str, object] = client.get(f"/api/reprocess/{job_id}").json()
        if job["status"] in {"done", "error"}:
            return job
        time.sleep(0.02)
    raise AssertionError("job did not finish")


def test_transcript_ws_streams_events(ws_client: TestClient) -> None:
    pid = _create_provider(ws_client)
    with ws_client.websocket_connect("/ws/transcript") as ws:
        ws_client.post("/api/session/start", json={"primary_provider": pid, "model": _MODEL})
        payload = json.loads(ws.receive_text())
        assert payload["text"] == "hello world"
    ws_client.post("/api/session/stop")


def test_transcript_ws_keeps_reprocess_out_of_the_dashboard(ws_client: TestClient) -> None:
    """A re-transcription reaches a session-filtered subscriber, and only that one.

    The session page wants it (that is how a version fills up on screen while
    the job runs); the dashboard's unfiltered socket must not, or last week's
    session starts scrolling past as if it were being said right now.
    """
    pid = _create_provider(ws_client)
    started = ws_client.post("/api/session/start", json={"primary_provider": pid, "model": _MODEL})
    sid = started.json()["id"]
    ws_client.post("/api/session/stop")

    with (
        ws_client.websocket_connect(f"/ws/transcript?session_id={sid}") as session_ws,
        ws_client.websocket_connect("/ws/transcript") as dashboard_ws,
    ):
        enqueued = ws_client.post(
            "/api/reprocess", json={"session_id": sid, "provider_id": pid, "model": _MODEL}
        ).json()
        job = _await_job(ws_client, enqueued["id"])
        assert job["status"] == "done"

        event = json.loads(session_ws.receive_text())
        assert event["session_id"] == sid
        # The version tag rides along, so a subscriber files the segment under
        # the version that produced it rather than into the original.
        assert event["source"] == f"reprocess:{enqueued['id']}"

        # Nothing of that run may be queued for the dashboard. Proven by what
        # it delivers next: the first frame is a *live* event of a new capture,
        # not one of the re-process events published before it.
        second = ws_client.post(
            "/api/session/start", json={"primary_provider": pid, "model": _MODEL}
        )
        live_sid = second.json()["id"]
        live = json.loads(dashboard_ws.receive_text())
        assert live["session_id"] == live_sid
        assert live["source"] == pid
    ws_client.post("/api/session/stop")


def test_logs_ws_streams_only_the_running_capture(ws_client: TestClient) -> None:
    """The dashboard's log feed is the running capture's, or nothing at all."""
    pid = _create_provider(ws_client)
    with ws_client.websocket_connect("/ws/logs") as ws:
        # Startup and the provider creation above already logged, and the
        # history replay ran on connect - all of it withheld, because no
        # capture is running for any of it to belong to.
        started = ws_client.post(
            "/api/session/start", json={"primary_provider": pid, "model": _MODEL}
        )
        sid = started.json()["id"]
        line = ws.receive_text()
        assert "session.start" in line
        assert sid in line
    ws_client.post("/api/session/stop")


def test_logs_ws_withholds_a_reprocess_of_another_session(ws_client: TestClient) -> None:
    """Re-processing logs never reach the dashboard, running capture or not.

    This is the leak the filter exists for: a re-transcription logs against the
    session whose stored audio it replays, which is never the one at the
    microphone, and it used to scroll through the live log panel as if it were.
    """
    pid = _create_provider(ws_client)
    started = ws_client.post("/api/session/start", json={"primary_provider": pid, "model": _MODEL})
    other_sid = started.json()["id"]
    ws_client.post("/api/session/stop")

    with ws_client.websocket_connect("/ws/logs") as ws:
        enqueued = ws_client.post(
            "/api/reprocess", json={"session_id": other_sid, "provider_id": pid, "model": _MODEL}
        ).json()
        assert _await_job(ws_client, enqueued["id"])["status"] == "done"

        # Whatever that run logged, the socket held all of it back. The proof
        # is the next line it does deliver: a fresh capture's own start.
        live = ws_client.post(
            "/api/session/start", json={"primary_provider": pid, "model": _MODEL}
        ).json()
        line = ws.receive_text()
        assert "session.start" in line
        assert live["id"] in line
    ws_client.post("/api/session/stop")
