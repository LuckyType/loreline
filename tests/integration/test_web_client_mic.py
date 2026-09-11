"""The browser capture socket, end to end: refusals, and a session it fed.

``WS /ws/audio/capture`` is the one socket in this app that reads rather than
writes, so what it has to get right is who may open it (one authenticated
browser, and only one), and that what it carries reaches the same WAV a sound
card's frames would have reached.
"""

from __future__ import annotations

import time
import wave
from collections.abc import Generator, Iterator, MutableMapping
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from test_web_session import FakeBackend, FakeSource, fake_diarizers

from loreline.audio.client_source import ClientMic
from loreline.models import CaptureSourceKind
from loreline.settings import Settings
from loreline.web.app import create_app

if TYPE_CHECKING:
    from loreline.audio.chunker import SpeechDetector
    from loreline.session.manager import CaptureSource
    from loreline.web.schemas import StartSessionRequest

_MODEL = "fake-model"
_RATE = 16000
_FRAME_BYTES = int(_RATE * 0.02) * 2
# Something audible, so the stored WAV can be told apart from silence padding.
_TONE = (1234).to_bytes(2, "little", signed=True) * (_FRAME_BYTES // 2)


def _always_speech(_frame: bytes) -> bool:
    """Stands in for Silero, which needs the optional ``audio`` extra.

    The one thing faked here: the frames, the socket, the registry, the client
    source and the WAV writer are all the real ones, because what this is about
    is whether a browser's audio reaches the recording.
    """
    return True


@contextmanager
def _app(tmp_path: Path, **mic_kwargs: float) -> Generator[TestClient]:
    """The real capture socket and client source, with a fake VAD and STT.

    The registry is built here and handed to both ``create_app`` and the
    capture factory, because they have to be looking at the same one: the
    socket writes into it and the factory reads the source out of it.
    """
    settings = Settings(data_dir=tmp_path / "data", auth_password="", jwt_secret="x")
    mic = ClientMic(preflight_fresh_s=30.0, **mic_kwargs)

    def capture_factory(
        req: StartSessionRequest, sample_rate: int
    ) -> tuple[CaptureSource, SpeechDetector]:
        if req.source is CaptureSourceKind.CLIENT:
            return mic.open(sample_rate), _always_speech
        return FakeSource(), _always_speech

    app = create_app(
        settings,
        client_mic=mic,
        capture_factory=capture_factory,  # type: ignore[arg-type]
        backend_factory=FakeBackend,  # type: ignore[arg-type]
        diarizer_factory=fake_diarizers,
    )
    with TestClient(app) as client:
        yield client


@pytest.fixture
def mic_client(tmp_path: Path) -> Iterator[TestClient]:
    with _app(tmp_path) as client:
        yield client


def _create_provider(client: TestClient) -> str:
    resp = client.post("/api/providers", json={"name": "Fake", "kind": "openai_compat"})
    return str(resp.json()["id"])


def test_an_unauthenticated_browser_cannot_stream(tmp_path: Path) -> None:
    """The socket carries audio into the recording, so it is behind the cookie."""
    settings = Settings(
        data_dir=tmp_path / "data", auth_password="hunter2", jwt_secret="test-secret"
    )
    app = create_app(settings)
    with TestClient(app) as client:
        with (
            pytest.raises(WebSocketDisconnect) as refused,
            client.websocket_connect("/ws/audio/capture") as ws,
        ):
            ws.send_json({"sample_rate": _RATE})
            ws.receive_json()
        assert refused.value.code == 1008


def test_a_second_browser_is_refused_with_a_reason(mic_client: TestClient) -> None:
    """One capture, one socket. A second is told why, not silently swapped in."""
    with mic_client.websocket_connect("/ws/audio/capture") as first:
        first.send_json({"sample_rate": _RATE, "channels": 1, "label": "Built-in"})
        assert first.receive_json() == {"ok": True, "sample_rate": _RATE}

        with mic_client.websocket_connect("/ws/audio/capture") as second:
            second.send_json({"sample_rate": _RATE})
            closed: MutableMapping[str, Any] = second.receive()
            assert closed["type"] == "websocket.close"
            assert closed["code"] == 1013
            assert "already streaming" in closed["reason"]

        # The first socket is untouched by the refusal.
        first.send_bytes(_TONE)


def test_a_hello_that_makes_no_sense_is_refused(mic_client: TestClient) -> None:
    with mic_client.websocket_connect("/ws/audio/capture") as ws:
        ws.send_json({"sample_rate": 3})
        closed: MutableMapping[str, Any] = ws.receive()
        assert closed["type"] == "websocket.close"
        assert closed["code"] == 1003
        assert "plausible" in closed["reason"]


def test_a_client_start_with_no_browser_streaming_is_refused(mic_client: TestClient) -> None:
    """The message is about the browser, not about a missing input device."""
    pid = _create_provider(mic_client)
    resp = mic_client.post(
        "/api/session/start",
        json={"primary_provider": pid, "model": _MODEL, "source": "client"},
    )
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert "browser" in detail
    assert "Settings" not in detail  # the device sentence would send them there


def test_a_device_start_still_works_without_a_browser(mic_client: TestClient) -> None:
    """The default is unchanged, so every stored default keeps working."""
    pid = _create_provider(mic_client)
    resp = mic_client.post("/api/session/start", json={"primary_provider": pid, "model": _MODEL})
    assert resp.status_code == 201
    assert mic_client.get("/api/system/healthz").json()["capture_source"] == "device"
    mic_client.post("/api/session/stop")


def test_what_the_browser_sent_is_what_the_recording_holds(
    mic_client: TestClient, tmp_path: Path
) -> None:
    """A browser capture is an ordinary session: same WAV, same transcript.

    Frames sent before Start keep the pre-flight fresh and are discarded;
    frames sent after it are the recording.
    """
    pid = _create_provider(mic_client)
    with mic_client.websocket_connect("/ws/audio/capture") as ws:
        ws.send_json({"sample_rate": _RATE, "channels": 1, "label": "Test input"})
        assert ws.receive_json()["ok"] is True
        # Opening the microphone before pressing Start is the normal order.
        ws.send_bytes(_TONE)

        start = mic_client.post(
            "/api/session/start",
            json={"primary_provider": pid, "model": _MODEL, "source": "client"},
        )
        assert start.status_code == 201
        session_id = str(start.json()["id"])

        health = mic_client.get("/api/system/healthz").json()
        assert health["capture_status"] == "capturing"
        assert health["capture_source"] == "client"

        sent = 25
        for _ in range(sent):
            ws.send_bytes(_TONE)
        _wait_for_audio(mic_client, sent * 0.02 * 0.6)

        stop = mic_client.post("/api/session/stop")
        assert stop.status_code == 200
        assert stop.json()["status"] == "completed"

    wav_path = tmp_path / "data" / "audio" / f"{session_id}.wav"
    with wave.open(str(wav_path), "rb") as wav:
        assert wav.getframerate() == _RATE
        pcm = wav.readframes(wav.getnframes())
    assert pcm, "the browser's audio never reached the recording"
    assert _TONE[:2] in pcm
    assert len(pcm) % _FRAME_BYTES == 0

    detail = mic_client.get(f"/api/session/{session_id}").json()
    assert detail["session"]["id"] == session_id
    assert mic_client.get("/api/system/healthz").json()["capture_source"] is None


def _wait_for_audio(client: TestClient, seconds: float) -> None:
    """Wait until the capture has recorded ``seconds`` of the browser's audio."""
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        captured = client.get("/api/system/healthz").json()["captured_seconds"]
        if captured is not None and captured >= seconds:
            return
        time.sleep(0.02)
    raise AssertionError("the browser's frames never reached the capture loop")


def test_a_browser_that_comes_back_keeps_the_session(tmp_path: Path) -> None:
    """A laptop that slept for a moment does not end the evening's recording.

    The socket closes, the session stays capturing and its frame age climbs
    (the same warning a stopped device raises), and a re-opened socket resumes
    feeding the same session.
    """
    with _app(tmp_path, reconnect_window_s=30.0) as client:
        pid = _create_provider(client)
        with client.websocket_connect("/ws/audio/capture") as ws:
            ws.send_json({"sample_rate": _RATE})
            ws.receive_json()
            ws.send_bytes(_TONE)
            start = client.post(
                "/api/session/start",
                json={"primary_provider": pid, "model": _MODEL, "source": "client"},
            )
            assert start.status_code == 201
            session_id = str(start.json()["id"])
            for _ in range(10):
                ws.send_bytes(_TONE)
            _wait_for_audio(client, 0.1)

        # The browser is gone; the session is not.
        assert client.get("/api/system/healthz").json()["capture_status"] == "capturing"

        with client.websocket_connect("/ws/audio/capture") as again:
            again.send_json({"sample_rate": _RATE})
            again.receive_json()
            before = float(client.get("/api/system/healthz").json()["captured_seconds"])
            for _ in range(10):
                again.send_bytes(_TONE)
            _wait_for_audio(client, before + 0.15)

        stopped = client.post("/api/session/stop")
        assert stopped.status_code == 200
        assert stopped.json()["status"] == "completed"
        assert stopped.json()["id"] == session_id


def test_a_browser_that_never_comes_back_ends_the_session(tmp_path: Path) -> None:
    """Past the window this ends the way a dead microphone ends a session.

    The audio that did arrive is still a complete, re-transcribable WAV, which
    is the whole reason the recording is closed rather than left open.
    """
    with _app(tmp_path, reconnect_window_s=0.2) as client:
        pid = _create_provider(client)
        with client.websocket_connect("/ws/audio/capture") as ws:
            ws.send_json({"sample_rate": _RATE})
            ws.receive_json()
            ws.send_bytes(_TONE)
            start = client.post(
                "/api/session/start",
                json={"primary_provider": pid, "model": _MODEL, "source": "client"},
            )
            assert start.status_code == 201
            session_id = str(start.json()["id"])
            for _ in range(10):
                ws.send_bytes(_TONE)
            _wait_for_audio(client, 0.1)

        # The row, not the badge: the manager goes idle the moment the runtime
        # leaves its slot, and the stored status is written a teardown later.
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            session = client.get(f"/api/session/{session_id}").json()["session"]
            if session["status"] != "capturing":
                break
            time.sleep(0.05)
        else:
            raise AssertionError("the session outlived the browser that was feeding it")

        assert session["status"] == "error"
        assert client.get("/api/system/healthz").json()["capture_status"] == "idle"
        wav_path = tmp_path / "data" / "audio" / f"{session_id}.wav"
        with wave.open(str(wav_path), "rb") as wav:
            assert wav.getnframes() > 0
