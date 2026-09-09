"""WebSocket tests for live transcript + logs streaming."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from starlette.testclient import TestClient, WebSocketTestSession
from test_web_session import FakeBackend, FakeSource, capture_factory, fake_diarizers

from loreline.audio.chunker import SpeechDetector, Utterance
from loreline.models import TranscriptEvent
from loreline.settings import Settings
from loreline.web.app import create_app

# Any model id: the fake backend never looks at it, but the API requires one -
# a provider row carries no model, so the request is where it is decided.
_MODEL = "fake-model"

# Bound on the log-socket reads below. A fake session emits a handful of lines,
# so anything near this many means the marker is never coming.
_MAX_LINES = 50


@pytest.fixture
def ws_client(tmp_path: Path) -> Iterator[TestClient]:
    settings = Settings(data_dir=tmp_path / "data", auth_password="", jwt_secret="x")
    app = create_app(
        settings,
        capture_factory=capture_factory,  # type: ignore[arg-type]
        backend_factory=FakeBackend,  # type: ignore[arg-type]
        diarizer_factory=fake_diarizers,
    )
    with TestClient(app) as client:
        yield client


def _create_provider(client: TestClient) -> str:
    resp = client.post(
        "/api/providers",
        json={"name": "Fake", "kind": "openai_compat"},
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


def _start(client: TestClient, provider_id: str) -> str:
    """Start a capture and return its session id."""
    resp = client.post(
        "/api/session/start", json={"primary_provider": provider_id, "model": _MODEL}
    )
    assert resp.status_code == 201
    session_id: str = resp.json()["id"]
    return session_id


def _lines_before(ws: WebSocketTestSession, marker: str) -> list[str]:
    """Read a log socket up to the line containing ``marker``; return the rest.

    Proving a pane stayed silent means proving a negative, which a blocking
    socket cannot do on its own. The marker is a line the feed delivers
    whatever else it does - the next capture's ``session.start``, which is the
    one thing this filter has always got right - so everything that arrived
    before it is the complete answer, including nothing at all.
    """
    lines: list[str] = []
    for _ in range(_MAX_LINES):
        line = ws.receive_text()
        if marker in line:
            return lines
        lines.append(line)
    raise AssertionError(f"the log feed never mentioned {marker}: {lines}")


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


def test_logs_ws_carries_the_lines_a_teardown_writes(ws_client: TestClient) -> None:
    """Stop is when a session says most of what the GM is waiting to read.

    The runtime is emptied before the first of those lines exists, so a feed
    scoped to "a capture is running right now" delivers none of them: the pane
    freezes on the line before Stop and never moves again, under a card
    promising "Transcribing what is still queued and closing the recording".
    """
    pid = _create_provider(ws_client)
    with ws_client.websocket_connect("/ws/logs") as ws:
        first = _start(ws_client, pid)
        assert ws_client.post("/api/session/stop").status_code == 200
        second = _start(ws_client, pid)

        lines = _lines_before(ws, second)
        assert any("session.stop" in line and first in line for line in lines), lines
    ws_client.post("/api/session/stop")


def test_logs_ws_withholds_a_reprocess_of_the_session_at_the_microphone(
    ws_client: TestClient,
) -> None:
    """The job filter has to hold for the case it was written for.

    A re-transcription logs against the session whose stored audio it replays,
    and nothing stops that being the session at the microphone: those lines
    carry the live capture's own id, so the job id is the only thing telling
    them apart. Keeping the pane quiet is not enough on its own - the same
    socket has to go on delivering the capture's lines, teardown included,
    which is what the second assertion is for.
    """
    pid = _create_provider(ws_client)
    with ws_client.websocket_connect("/ws/logs") as ws:
        sid = _start(ws_client, pid)
        job_id = _reprocess_running_capture(ws_client, sid, pid)
        _await_job(ws_client, job_id)
        assert ws_client.post("/api/session/stop").status_code == 200
        second = _start(ws_client, pid)

        lines = _lines_before(ws, second)
        assert not any(job_id in line for line in lines), lines
        assert any("session.stop" in line and sid in line for line in lines), lines
    ws_client.post("/api/session/stop")


def _reprocess_running_capture(client: TestClient, session_id: str, provider_id: str) -> str:
    """Enqueue a re-transcription of the session being recorded right now.

    Retried because a capture becomes re-processable partway through itself:
    the job is refused until the index sidecar beside the growing WAV exists,
    which is the first indexed utterance.
    """
    body = {"session_id": session_id, "provider_id": provider_id, "model": _MODEL}
    for _ in range(100):
        resp = client.post("/api/reprocess", json=body)
        if resp.status_code == 202:
            job_id: str = resp.json()["id"]
            return job_id
        time.sleep(0.02)
    raise AssertionError("the running capture never became re-processable")


def test_logs_ws_goes_quiet_once_the_teardown_is_over(ws_client: TestClient) -> None:
    """A finished session stops being what the dashboard's panes are about.

    The probe is a line carrying a session id and no job id, written long after
    that session ended: deleting a re-processed version logs one. It belongs in
    the session's own log file and nowhere else, and a live view left pointing
    at the session it was about would take it - along with every line already
    in the ring buffer, replayed to this socket the moment it connects.
    """
    pid = _create_provider(ws_client)
    sid = _start(ws_client, pid)
    assert ws_client.post("/api/session/stop").status_code == 200
    job_id = _reprocess_running_capture(ws_client, sid, pid)
    _await_job(ws_client, job_id)

    with ws_client.websocket_connect("/ws/logs") as ws:
        deleted = ws_client.request(
            "DELETE", f"/api/session/{sid}/transcript", params={"version": job_id}
        )
        assert deleted.status_code == 200
        live = _start(ws_client, pid)

        assert _lines_before(ws, live) == []
    ws_client.post("/api/session/stop")


def test_transcript_ws_carries_what_the_drain_settles(tmp_path: Path) -> None:
    """An event published during the teardown still reaches the dashboard.

    Stop drains the live path before it finalizes anything, and a streaming
    connector settles its open turn inside that drain: those finals carry the
    id of a session the manager has already stopped calling current. Dropped,
    they leave the dimmed interim row they were meant to replace dimmed for the
    rest of the evening, showing text the stored transcript has already
    corrected.

    The pair below stands in for a streaming connector's timing, because what
    decides the outcome is when an event is published and not which path
    published it. Stopping the frame source is the first thing the teardown
    does, and it happens after the runtime is already gone, so a backend that
    waits for it answers from inside exactly the window Stop opens.
    """
    tearing_down = asyncio.Event()

    class StoppedSource(FakeSource):
        """A frame source that says when its session began to be torn down."""

        def stop(self) -> None:
            super().stop()
            tearing_down.set()

    def stopping_capture(_req: object, _rate: int) -> tuple[object, SpeechDetector]:
        def detector(_frame: bytes) -> bool:
            return True

        return StoppedSource(), detector

    class DrainBackend(FakeBackend):
        """Answers each utterance only once the teardown is under way."""

        async def transcribe(
            self,
            utterance: Utterance,
            *,
            session_id: str,
            glossary: object = None,
        ) -> TranscriptEvent | None:
            await tearing_down.wait()
            return await super().transcribe(utterance, session_id=session_id, glossary=glossary)

    settings = Settings(data_dir=tmp_path / "data", auth_password="", jwt_secret="x")
    app = create_app(
        settings,
        capture_factory=stopping_capture,  # type: ignore[arg-type]
        backend_factory=DrainBackend,  # type: ignore[arg-type]
        diarizer_factory=fake_diarizers,
    )
    with TestClient(app) as client, client.websocket_connect("/ws/transcript") as ws:
        pid = _create_provider(client)
        sid = _start(client, pid)
        stopped = client.post("/api/session/stop")
        assert stopped.json()["status"] == "completed"

        event = json.loads(ws.receive_text())
        assert event["session_id"] == sid
        assert event["text"] == "hello world"


class _DyingSource:
    """A device unplugged a few frames into the session.

    The other half of the teardown story (see
    tests/integration/test_capture_failure.py): nobody pressed Stop, so nobody
    is waiting on an answer, and the log pane is the only place the GM can
    learn that the microphone is gone.
    """

    def stop(self) -> None:
        return None

    async def frames(self) -> AsyncIterator[tuple[bytes, float]]:
        for i in range(3):
            await asyncio.sleep(0)
            yield b"\x01\x00" * 160, i * 0.02
        msg = "device disconnected"
        raise RuntimeError(msg)


def _await_idle(client: TestClient) -> None:
    """Wait for a capture that is ending itself to have released the runtime."""
    for _ in range(250):  # 5 s at 20 ms
        if client.get("/api/system/healthz").json()["capture_status"] == "idle":
            return
        time.sleep(0.02)
    raise AssertionError("the capture never ended")


def test_logs_ws_carries_the_teardown_of_a_capture_that_died(tmp_path: Path) -> None:
    """A session nobody stopped ends the same way, and reports it the same way.

    The done callback that finalizes a dead capture empties the runtime first,
    exactly as Stop does, so everything it writes afterwards - the death
    itself, then the ordinary end-of-session lines - rides on the live view
    outliving the capture. Getting this wrong is worse here than under Stop:
    there is no request whose answer says what happened, so a pane that shows
    nothing is all the GM gets.
    """

    def detector(_frame: bytes) -> bool:
        return True

    def dying_capture(_req: object, _rate: int) -> tuple[object, object]:
        return _DyingSource(), detector

    settings = Settings(data_dir=tmp_path / "data", auth_password="", jwt_secret="x")
    app = create_app(
        settings,
        capture_factory=dying_capture,  # type: ignore[arg-type]
        backend_factory=FakeBackend,  # type: ignore[arg-type]
        diarizer_factory=fake_diarizers,
    )
    with TestClient(app) as client, client.websocket_connect("/ws/logs") as ws:
        pid = _create_provider(client)
        sid = _start(client, pid)
        _await_idle(client)
        # The next start waits out the teardown, which the manager runs under
        # the lock it holds from Start to the last finalizing step, so this line
        # marks "everything the dead session had to say has been said".
        second = _start(client, pid)

        lines = _lines_before(ws, second)
        assert any("session.capture.died" in line and sid in line for line in lines), lines
        assert any("session.stop" in line and sid in line for line in lines), lines
