"""In-memory log broadcaster for the live log WebSocket.

Holds a bounded ring buffer of recent log records, each tagged with a monotonic
sequence id, and fans new records out to WS subscribers. The WS subscribes
first, replays history, then streams live records with a higher seq - which
closes the gap/duplication window between snapshotting history and subscribing.
Fed by a structlog processor installed in ``configure_logging``.

A record keeps the structured fields that say what produced the line
(``session_id`` / ``job_id``) next to the rendered text. Without them every
subscriber gets every line the process emits, so a re-transcription running in
the background scrolls past the dashboard as if it were the microphone - see
``loreline.web.routes.logs_ws`` for the filter they exist for.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from loreline.bus import EventBus


@dataclass(frozen=True, slots=True)
class LogRecord:
    """One rendered log line plus the fields attributing it to a capture/job."""

    seq: int
    line: str
    session_id: str | None = None
    job_id: str | None = None

    def is_capture_line(self, active_session_id: str | None) -> bool:
        """Whether this line came from the live capture of ``active_session_id``.

        False for everything not tied to a session (startup, HTTP, provider
        edits) and for re-processing, which carries a ``job_id`` even when it
        replays the very session being captured.
        """
        return (
            active_session_id is not None
            and self.session_id == active_session_id
            and self.job_id is None
        )


class LogBroadcaster:
    """Ring buffer + pub/sub for rendered log records (seq-tagged)."""

    def __init__(self, *, maxlen: int = 500) -> None:
        self._buffer: deque[LogRecord] = deque(maxlen=maxlen)
        self._bus: EventBus[LogRecord] = EventBus()
        self._seq = 0

    @property
    def bus(self) -> EventBus[LogRecord]:
        return self._bus

    def history(self) -> list[LogRecord]:
        """Return the buffered records (oldest first)."""
        return list(self._buffer)

    def emit(self, line: str, *, session_id: str | None = None, job_id: str | None = None) -> None:
        """Append a sequence-tagged record and fan it out to subscribers."""
        self._seq += 1
        record = LogRecord(seq=self._seq, line=line, session_id=session_id, job_id=job_id)
        self._buffer.append(record)
        self._bus.publish_nowait(record)
