"""Integration tests for the stored per-version log files and their route."""

from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from starlette.testclient import TestClient
from test_web_session import (  # type: ignore[import-not-found]
    FakeBackend,
    OutOfCreditBackend,
    capture_factory,
    fake_diarizers,
)

from loreline.audio.chunker import Utterance
from loreline.logging import get_logger
from loreline.models import ProviderConfig, TranscriptEvent
from loreline.secrets import SecretStore
from loreline.settings import Settings
from loreline.web.app import create_app

# Any model id: the fake backend never looks at it, but the API requires one -
# a provider row carries no model, so the request is where it is decided.
_MODEL = "fake-model"

log = get_logger("tests.chatty_backend")


class ChattyBackend(FakeBackend):
    """A backend that logs the way the real ones do: with no session id.

    Nothing under ``SessionManager`` (router, backends, VAD) is told which
    session it serves, so a line like this can only be attributed by the
    context the capture task bound - which is exactly what these tests check.
    """

    async def transcribe(
        self,
        utterance: Utterance,
        *,
        session_id: str,
        glossary: object = None,
    ) -> TranscriptEvent | None:
        event = await super().transcribe(utterance, session_id=session_id, glossary=glossary)
        log.info("test.backend.transcribed")
        return event


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    settings = Settings(data_dir=tmp_path / "data", auth_password="", jwt_secret="x")
    app = create_app(
        settings,
        capture_factory=capture_factory,  # type: ignore[arg-type]
        backend_factory=ChattyBackend,  # type: ignore[arg-type]
        diarizer_factory=fake_diarizers,
    )
    with TestClient(app) as test_client:
        yield test_client


def _provider(client: TestClient) -> str:
    resp = client.post(
        "/api/providers",
        json={"name": "Fake", "kind": "openai_compat"},
    )
    return resp.json()["id"]


def _run_session(client: TestClient, pid: str) -> str:
    session_id: str = client.post(
        "/api/session/start", json={"primary_provider": pid, "model": _MODEL}
    ).json()["id"]
    client.post("/api/session/stop")
    return session_id


def _reprocess(client: TestClient, session_id: str, pid: str) -> str:
    job_id: str = client.post(
        "/api/reprocess", json={"session_id": session_id, "provider_id": pid, "model": _MODEL}
    ).json()["id"]
    for _ in range(100):
        if client.get(f"/api/reprocess/{job_id}").json()["status"] in {"done", "error"}:
            return job_id
        time.sleep(0.02)
    raise AssertionError("job did not finish")


def test_original_capture_log_is_written_and_readable(client: TestClient) -> None:
    pid = _provider(client)
    sid = _run_session(client, pid)

    resp = client.get(f"/api/session/{sid}/logs")
    assert resp.status_code == 200
    body = resp.json()
    assert body["version"] == "original"
    assert "session.start" in body["logs"]
    assert "session.stop" in body["logs"]
    # The line the backend emitted without knowing anything about sessions.
    # Without the capture task binding session_id into its logging context,
    # this file would hold the two lifecycle lines and nothing else - the
    # feature would look broken exactly where it is wanted most.
    assert "test.backend.transcribed" in body["logs"]


def test_reprocess_logs_go_to_their_own_version(client: TestClient) -> None:
    pid = _provider(client)
    sid = _run_session(client, pid)
    job_id = _reprocess(client, sid, pid)

    job_logs = client.get(f"/api/session/{sid}/logs", params={"version": job_id})
    assert job_logs.status_code == 200
    assert "reprocess.enqueue" in job_logs.json()["logs"]
    assert "test.backend.transcribed" in job_logs.json()["logs"]

    # And none of it bled into the capture's own log.
    original = client.get(f"/api/session/{sid}/logs").json()["logs"]
    assert job_id not in original
    assert "reprocess.enqueue" not in original


def test_a_run_records_what_it_set_out_to_do_and_what_it_wrote(client: TestClient) -> None:
    """The README's promise, which a finished run did not keep.

    Every version's log used to hold the caller's ``reprocess.enqueue`` line
    and whatever the backend happened to say - on the deployment this was
    measured on, two completed re-transcriptions of 954 and 1322 segments had
    no stored log at all. So the one screen meant for answering "what happened
    to this version" could say neither that the run started, nor what it
    produced, nor how long it took.
    """
    pid = _provider(client)
    sid = _run_session(client, pid)
    job_id = _reprocess(client, sid, pid)

    logs = client.get(f"/api/session/{sid}/logs", params={"version": job_id}).json()["logs"]
    assert "reprocess.start" in logs
    assert f"version={job_id}" in logs  # which version this run writes
    assert f"provider_id={pid}" in logs  # and what does the work
    assert f"model={_MODEL}" in logs
    assert "reprocess.finished" in logs
    assert "segments_added=" in logs
    assert "elapsed_s=" in logs


def test_a_failed_run_writes_the_reason_and_the_traceback(tmp_path: Path) -> None:
    """What the row's one sentence tells a reader to go and look at.

    Both halves are load-bearing and neither was there. The ``error=`` field is
    the same sentence the session page shows, so the file and the page cannot
    disagree about which run failed and why; the traceback is the part the page
    deliberately does not carry. It reaches the file only because the log tap
    runs after ``format_exc_info`` (see ``loreline.logging``) - before that, a
    failed run's file recorded the fact of an exception as ``exc_info=True``
    and not one line of it.
    """
    settings = Settings(data_dir=tmp_path / "data", auth_password="", jwt_secret="x")
    live = [ChattyBackend]  # the capture itself must still work

    def factory(config: ProviderConfig, secrets: SecretStore, model: str | None) -> FakeBackend:
        cls = live.pop() if live else OutOfCreditBackend
        return cls(config, secrets, model)

    app = create_app(
        settings,
        capture_factory=capture_factory,  # type: ignore[arg-type]
        backend_factory=factory,  # type: ignore[arg-type]
        diarizer_factory=fake_diarizers,
    )
    with TestClient(app) as client:
        pid = _provider(client)
        sid = _run_session(client, pid)
        job_id = _reprocess(client, sid, pid)
        assert client.get(f"/api/reprocess/{job_id}").json()["status"] == "error"

        logs = client.get(f"/api/session/{sid}/logs", params={"version": job_id}).json()["logs"]

    assert "reprocess.start" in logs  # the run is bracketed even when it fails
    assert "reprocess.failed" in logs
    assert "no credits remaining" in logs  # the row's own sentence, in the file
    assert "Traceback (most recent call last)" in logs
    assert "ProvidersExhaustedError" in logs  # the exception, named, not "exc_info=True"


def test_logs_are_deleted_with_their_version_and_their_session(
    client: TestClient, tmp_path: Path
) -> None:
    pid = _provider(client)
    sid = _run_session(client, pid)
    job_id = _reprocess(client, sid, pid)

    deleted = client.request("DELETE", f"/api/session/{sid}/transcript", params={"version": job_id})
    assert deleted.status_code == 200
    assert client.get(f"/api/session/{sid}/logs", params={"version": job_id}).status_code == 404
    assert client.get(f"/api/session/{sid}/logs").status_code == 200

    client.post("/api/session/delete", json={"ids": [sid]})
    # Deleting a session takes its logs the way it takes its stored audio.
    assert not (tmp_path / "data" / "logs" / sid).exists()


def test_unknown_version_and_traversal_are_refused(client: TestClient) -> None:
    pid = _provider(client)
    sid = _run_session(client, pid)

    assert client.get(f"/api/session/{sid}/logs", params={"version": "nope"}).status_code == 404
    # A version is a path segment on disk, so this must never resolve upward.
    escape = client.get(f"/api/session/{sid}/logs", params={"version": "../../secrets"})
    assert escape.status_code == 404
    assert client.get("/api/session/missing/logs").status_code == 404
