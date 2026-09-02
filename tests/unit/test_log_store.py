"""Unit tests for the per-version log files and the broadcaster's records."""

from __future__ import annotations

from pathlib import Path

import pytest

from loreline.logbus import LogBroadcaster, LogRecord
from loreline.persistence.log_store import LogStore


def test_append_read_and_delete_per_version(tmp_path: Path) -> None:
    store = LogStore(tmp_path)
    assert store.exists("s1", "original") is False

    store.append("s1", "original", "capture line one")
    store.append("s1", "original", "capture line two")
    store.append("s1", "job1", "reprocess line")

    assert store.path("s1", "original") == tmp_path / "s1" / "original.log"
    assert store.read("s1", "original") == "capture line one\ncapture line two\n"
    # A re-processing run's lines land in their own version's file, never in
    # the capture's - which is the whole reason the files are per version.
    assert store.read("s1", "job1") == "reprocess line\n"

    store.delete_version("s1", "job1")
    assert store.exists("s1", "job1") is False
    assert store.exists("s1", "original") is True

    store.delete_session("s1")
    assert store.session_dir("s1").exists() is False


def test_prune_drops_only_unknown_sessions(tmp_path: Path) -> None:
    store = LogStore(tmp_path)
    store.append("keep", "original", "x")
    store.append("gone", "original", "x")

    assert store.prune({"keep"}) == 1
    assert store.exists("keep", "original") is True
    assert store.session_dir("gone").exists() is False


def test_paths_outside_the_store_are_refused(tmp_path: Path) -> None:
    """``version`` comes from a query string, so it is checked, not trusted."""
    store = LogStore(tmp_path)
    with pytest.raises(ValueError, match="unsafe"):
        store.path("s1", "../../secrets")
    with pytest.raises(ValueError, match="unsafe"):
        store.read("s1", "..")
    # The write path swallows it instead: a bad id must not break logging.
    store.append("s1", "../escape", "line")
    assert list(tmp_path.rglob("*.log")) == []


def test_records_belong_to_the_running_capture_only() -> None:
    """What the dashboard's log socket filters on (see logs_ws)."""
    capture = LogRecord(seq=1, line="l", session_id="A")
    job = LogRecord(seq=2, line="l", session_id="A", job_id="j1")
    untagged = LogRecord(seq=3, line="l")

    assert capture.is_capture_line("A") is True
    # Session A's line while session B is the one at the microphone.
    assert capture.is_capture_line("B") is False
    # Nothing at all when no capture is running.
    assert capture.is_capture_line(None) is False
    # Re-processing carries a job id even when it replays the live session.
    assert job.is_capture_line("A") is False
    assert untagged.is_capture_line("A") is False


def test_broadcaster_keeps_the_fields_next_to_the_line() -> None:
    broadcaster = LogBroadcaster(maxlen=2)
    broadcaster.emit("first", session_id="A")
    broadcaster.emit("second", session_id="A", job_id="j1")
    broadcaster.emit("third")

    history = broadcaster.history()
    assert [r.seq for r in history] == [2, 3]  # ring buffer dropped the oldest
    assert (history[0].line, history[0].session_id, history[0].job_id) == ("second", "A", "j1")
    assert (history[1].session_id, history[1].job_id) == (None, None)
