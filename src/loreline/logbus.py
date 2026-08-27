"""In-memory log broadcaster for the live log WebSocket.

Holds a bounded ring buffer of recent log lines, each tagged with a monotonic
sequence id, and fans new lines out to WS subscribers. The WS subscribes first,
replays history, then streams live lines with a higher seq - which closes the
gap/duplication window between snapshotting history and subscribing. Fed by a
structlog processor installed in ``configure_logging``.
"""

from __future__ import annotations

from collections import deque

from loreline.bus import EventBus

LogLine = tuple[int, str]


class LogBroadcaster:
    """Ring buffer + pub/sub for rendered log lines (seq-tagged)."""

    def __init__(self, *, maxlen: int = 500) -> None:
        self._buffer: deque[LogLine] = deque(maxlen=maxlen)
        self._bus: EventBus[LogLine] = EventBus()
        self._seq = 0

    @property
    def bus(self) -> EventBus[LogLine]:
        return self._bus

    def history(self) -> list[LogLine]:
        """Return buffered ``(seq, line)`` pairs (oldest first)."""
        return list(self._buffer)

    def emit(self, line: str) -> None:
        """Append a sequence-tagged line and fan it out to subscribers."""
        self._seq += 1
        item = (self._seq, line)
        self._buffer.append(item)
        self._bus.publish_nowait(item)
