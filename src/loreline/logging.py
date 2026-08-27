"""Structured logging configuration via structlog."""

from __future__ import annotations

import logging
import sys
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from loreline.logbus import LogBroadcaster


def _make_tap(broadcaster: LogBroadcaster) -> structlog.typing.Processor:
    """Build a processor forwarding rendered lines to the log broadcaster."""

    def tap(
        _logger: object, _method: str, event_dict: structlog.typing.EventDict
    ) -> structlog.typing.EventDict:
        ts = event_dict.get("timestamp", "")
        level = event_dict.get("level", "")
        event = event_dict.get("event", "")
        extras = " ".join(
            f"{k}={v}" for k, v in event_dict.items() if k not in {"timestamp", "level", "event"}
        )
        broadcaster.emit(f"{ts} [{level}] {event} {extras}".rstrip())
        return event_dict

    return tap


def configure_logging(
    *,
    level: str = "INFO",
    json_logs: bool = False,
    broadcaster: LogBroadcaster | None = None,
) -> None:
    """Configure structlog + stdlib logging.

    Console renderer for dev, JSON renderer for prod. When ``broadcaster`` is
    given, rendered lines are also forwarded to the live log WebSocket.
    """
    log_level = getattr(logging, level.upper(), logging.INFO)

    shared_processors: list[structlog.typing.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
    ]
    if broadcaster is not None:
        shared_processors.append(_make_tap(broadcaster))

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
