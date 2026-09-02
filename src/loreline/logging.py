"""Structured logging configuration via structlog."""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING

import structlog

from loreline.models import ORIGINAL_VERSION

if TYPE_CHECKING:
    from contextlib import AbstractContextManager

    from loreline.logbus import LogBroadcaster
    from loreline.persistence import LogStore


@dataclass(slots=True)
class _Sinks:
    """Where the tap forwards lines *right now*.

    The tap reads through this instead of closing over its sinks because
    structlog caches a bound logger's processor chain on first use: a processor
    holding the broadcaster it was built with keeps feeding a previous
    configuration long after ``configure_logging`` ran again, so lines vanish
    from the app that is actually serving. Indirection here means a reconfigure
    is picked up by loggers that were already cached.
    """

    broadcaster: LogBroadcaster | None = None
    store: LogStore | None = None


_SINKS = _Sinks()


def _field(event_dict: structlog.typing.EventDict, key: str) -> str | None:
    """Read one structured field as a non-empty string, or None."""
    value = event_dict.get(key)
    return value if isinstance(value, str) and value else None


def _tap(
    _logger: object, _method: str, event_dict: structlog.typing.EventDict
) -> structlog.typing.EventDict:
    """Forward each rendered line to the broadcaster and the log files.

    The rendered line is kept (it is what both the dashboard and the stored
    files show), but the fields that say *what produced it* travel alongside it
    rather than only melted into the text: filtering a stream on
    ``session_id=abc`` re-parsed out of a rendered string is guesswork, and
    every consumer would have to do it for itself.

    ``session_id`` and ``job_id`` come from the event dict, which by this point
    holds both explicit keyword arguments and whatever the emitting task bound
    into its context (see :func:`bind_log_context`).
    """
    broadcaster = _SINKS.broadcaster
    if broadcaster is None:
        return event_dict
    ts = event_dict.get("timestamp", "")
    level = event_dict.get("level", "")
    event = event_dict.get("event", "")
    extras = " ".join(
        f"{k}={v}" for k, v in event_dict.items() if k not in {"timestamp", "level", "event"}
    )
    line = f"{ts} [{level}] {event} {extras}".rstrip()
    session_id = _field(event_dict, "session_id")
    job_id = _field(event_dict, "job_id")
    broadcaster.emit(line, session_id=session_id, job_id=job_id)
    store = _SINKS.store
    if store is not None and session_id is not None:
        # A line with no job behind it is the live capture, whose version tag is
        # ORIGINAL_VERSION - the same name the versions table, the transcript
        # route and the exports use for it.
        store.append(session_id, job_id or ORIGINAL_VERSION, line)
    return event_dict


def bind_log_context(**fields: str) -> None:
    """Bind fields onto every log line the current task emits from here on.

    Attribution is what makes a line routable (dashboard vs stored file), and
    passing ``session_id=`` by hand at each call site does not reach the router,
    the STT backends or the VAD - the code that produces most of a capture's
    log lines and knows nothing about sessions. ``asyncio`` copies the context
    into each task at creation time, so binding at the top of a task body
    covers everything that task awaits without leaking into unrelated tasks.
    """
    structlog.contextvars.bind_contextvars(**fields)


def log_context(**fields: str) -> AbstractContextManager[None]:
    """Bind fields for the duration of a block (see :func:`bind_log_context`)."""
    return structlog.contextvars.bound_contextvars(**fields)


def configure_logging(
    *,
    level: str = "INFO",
    json_logs: bool = False,
    broadcaster: LogBroadcaster | None = None,
    log_store: LogStore | None = None,
) -> None:
    """Configure structlog + stdlib logging.

    Console renderer for dev, JSON renderer for prod. When ``broadcaster`` is
    given, rendered lines are also forwarded to the live log WebSocket, and to
    the per-version log files when ``log_store`` is given too.
    """
    log_level = getattr(logging, level.upper(), logging.INFO)
    _SINKS.broadcaster = broadcaster
    _SINKS.store = log_store

    shared_processors: list[structlog.typing.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        # After merge_contextvars, so the tap sees the bound session/job fields.
        # Always in the chain, no-op without a broadcaster: the chain a cached
        # logger holds must not depend on how the *first* configure call went.
        _tap,
    ]

    renderer: structlog.typing.Processor = (
        structlog.processors.JSONRenderer()
        if json_logs
        else structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())
    )

    structlog.configure(
        processors=[*shared_processors, structlog.processors.format_exc_info, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=True,
    )

    logging.basicConfig(
        format="%(message)s",
        stream=sys.stderr,
        level=log_level,
    )


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Return a bound structlog logger."""
    return structlog.get_logger(name)  # pyright: ignore[reportReturnType]
