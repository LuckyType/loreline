"""Shared helpers for WebSocket-based STT connectors."""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import AsyncIterator
from typing import cast

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import InvalidHandshake, InvalidStatus, InvalidURI, WebSocketException

from loreline.health import (
    PROBE_TIMEOUT_S,
    SOCKET_READ_TIMEOUT_S,
    HealthReport,
    HealthStatus,
    classify_status,
    error_message,
    looks_like_auth_message,
)
from loreline.logging import get_logger

log = get_logger(__name__)

# Longest a connector will spend dropping a socket. ``websockets`` waits its
# own ``close_timeout`` (10 s) for the other end to answer the closing
# handshake, and the sockets this is called on are usually ones that have
# already stopped answering: a stop is waiting on every one of them, and the
# handshake is a courtesy rather than something a transcript depends on.
CLOSE_TIMEOUT_S = 2.0


def as_dict(raw: str | bytes) -> dict[str, object]:
    """Parse one frame into a string-keyed dict; empty for anything else.

    Empty rather than an exception for a frame that is not JSON at all, which
    is the whole point: every reader here calls this once per frame inside its
    receive loop, so a single malformed frame - a proxy's error page, a
    truncated write, a vendor's stray keepalive text - used to raise
    ``JSONDecodeError`` out of the loop and end a live connection. Skipping it
    costs one log line and the connection survives to the next frame.
    """
    try:
        data: object = json.loads(raw)
    except (ValueError, TypeError):
        text = raw if isinstance(raw, str) else raw.decode("utf-8", "replace")
        log.warning("ws.frame.unparsed", frame=text[:200])
        return {}
    if isinstance(data, dict):
        return cast("dict[str, object]", data)
    return {}


async def close_socket(ws: ClientConnection, *, timeout_s: float = CLOSE_TIMEOUT_S) -> None:
    """Drop a socket within a bounded time, cancellation included. Never raises.

    Two failures this exists to stop, and both of them cost a session its
    shutdown rather than a frame. ``ws.close()`` performs the closing
    handshake and waits ``close_timeout`` for an answer, which a dead socket
    never sends, so an unbounded close holds Stop for ten seconds per
    connection. And ``contextlib.suppress(Exception)`` does not cover
    ``CancelledError``, so a connector cancelled while closing used to leave
    the socket to the garbage collector - the file descriptor, the TLS session
    and the vendor's own session with it.

    The close therefore runs as a task of its own, which bounds itself. A
    caller that is cancelled while waiting for it re-raises, as it must, and
    the close finishes behind it.
    """
    closing = asyncio.create_task(_close_quietly(ws, timeout_s))
    try:
        await asyncio.shield(closing)
    except asyncio.CancelledError:
        raise  # ...and `closing` runs on to completion behind us
    except Exception:  # pragma: no cover - _close_quietly swallows its own
        log.debug("ws.close.failed")


async def _close_quietly(ws: ClientConnection, timeout_s: float) -> None:
    """The bounded close itself, which is never worth raising out of."""
    with contextlib.suppress(Exception):
        async with asyncio.timeout(timeout_s):
            await ws.close()


async def next_frame(
    frames: AsyncIterator[str | bytes], *, deadline: float | None
) -> str | bytes | None:
    """The next frame, or None once ``deadline`` (a monotonic instant) passes.

    Racing the read against the deadline, rather than checking the clock as
    frames happen to arrive, is the difference between leaving a session on
    time and leaving it when the room next says something. Both vendors that
    cap a session announce the cap in advance (Gemini's ``goAway``,
    AssemblyAI's ``expires_at``), and a quiet table produces no frames at all,
    so a deadline checked only inside ``async for`` is a deadline the server
    reaches first - and the server's own moment is mid-turn.

    Raises ``StopAsyncIteration`` when the socket ends, which is the ordinary
    way a connection finishes and is a different answer from the deadline.
    """
    if deadline is None:
        return await anext(frames)
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return None
    try:
        async with asyncio.timeout(remaining):
            return await anext(frames)
    except TimeoutError:
        return None


def as_list(value: object) -> list[object]:
    """Return ``value`` as a list, or an empty list."""
    return cast("list[object]", value) if isinstance(value, list) else []


def as_obj_dict(value: object) -> dict[str, object]:
    """Return ``value`` as a string-keyed dict, or an empty dict."""
    if isinstance(value, dict):
        return {str(k): v for k, v in cast("dict[object, object]", value).items()}
    return {}


def get_str(mapping: dict[str, object], key: str, default: str = "") -> str:
    value = mapping.get(key)
    return value if isinstance(value, str) else default


def get_float(mapping: dict[str, object], key: str, default: float = 0.0) -> float:
    value = mapping.get(key)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return default


def get_bool(mapping: dict[str, object], key: str, *, default: bool = False) -> bool:
    value = mapping.get(key)
    return value if isinstance(value, bool) else default


async def probe_socket(
    url: str,
    headers: dict[str, str],
    first_frame: str | None,
    *,
    timeout_s: float = PROBE_TIMEOUT_S,
    read_timeout_s: float | None = None,
) -> HealthReport:
    """Open a socket, say ``first_frame`` if there is one, grade what happens.

    Never raises. This is the whole of a streaming vendor's health probe, and
    the four connectors used to carry it as four byte-identical methods that
    differed only in the connect target and the first frame, which are now
    the arguments: the surface's URL (the key already in the query string
    where that is how the vendor authenticates) and headers, and the frame
    the surface declares in capabilities.yaml, if any.

    A rejected upgrade is a plain HTTP response, so a bad key surfaces as a
    status code on the handshake and grades exactly like an HTTP probe, which
    is what makes "wrong key" distinguishable from "wrong host" here at all.
    What arrives after the upgrade is graded by :func:`_grade_first_frame`.

    ``read_timeout_s`` bounds the wait for the first reply and defaults to
    :data:`SOCKET_READ_TIMEOUT_S` at call time, so a test can shorten the
    quiet-server case without waiting the real five seconds.
    """
    if read_timeout_s is None:
        read_timeout_s = SOCKET_READ_TIMEOUT_S
    try:
        async with asyncio.timeout(timeout_s):
            async with connect(url, additional_headers=headers) as ws:
                return await _grade_first_frame(ws, first_frame, timeout_s=read_timeout_s)
    except TimeoutError:
        return HealthReport(HealthStatus.UNREACHABLE, "the socket did not open in time")
    except (OSError, WebSocketException) as exc:
        return classify_handshake_error(exc)


async def _grade_first_frame(
    ws: ClientConnection, prompt: str | None, *, timeout_s: float = SOCKET_READ_TIMEOUT_S
) -> HealthReport:
    """Decide reachability/auth from an already-connected STT socket.

    Connecting performs the auth handshake - a bad key is rejected there, as an
    HTTP status on the upgrade, which never reaches this function; see
    :func:`classify_handshake_error` for that half. What is left for here is a
    key rejected *after* the upgrade, which arrives as an ``error`` frame, and
    the ordinary case of a session that simply waits for audio and says nothing
    at all. The latter is a clean timeout, and healthy.

    An error frame we cannot read is ``UNKNOWN`` rather than a failure: the
    socket opened, so the endpoint and the credential got at least that far,
    and the vendor's own words in the frame say more than a red badge would.
    """
    if prompt is not None:
        await ws.send(prompt)
    try:
        async with asyncio.timeout(timeout_s):
            raw = await ws.recv()
    except TimeoutError:
        return HealthReport(HealthStatus.HEALTHY)
    frame = as_dict(raw)
    if get_str(frame, "type").lower() != "error":
        return HealthReport(HealthStatus.HEALTHY)
    # Vendors spell the text differently: Deepgram uses "description", the
    # AssemblyAI streaming API "error", and both may add a "message".
    message = next(
        (get_str(frame, key) for key in ("description", "error", "message") if get_str(frame, key)),
        "the provider returned an error frame",
    )
    if looks_like_auth_message(message):
        return HealthReport(HealthStatus.UNAUTHORIZED, message)
    return HealthReport(HealthStatus.UNKNOWN, message)


def classify_handshake_error(exc: OSError | WebSocketException) -> HealthReport:
    """Grade a failed websocket connect. Never raises.

    The upgrade is a plain HTTP request, so a rejected one carries a status
    code and grades exactly like any other probe response - which is what makes
    a bad key on a socket connector distinguishable from a wrong host at all.
    Everything below that (DNS, refused connection, TLS, a malformed URL) is
    ``UNREACHABLE``; a websocket-protocol failure we cannot read is ``UNKNOWN``,
    on the same reasoning as the error frame above.
    """
    if isinstance(exc, InvalidStatus):
        body = exc.response.body.decode("utf-8", "replace")
        return classify_status(
            exc.response.status_code,
            error_message(body) or exc.response.reason_phrase,
            # The whole body, not just the extracted sentence: Deepgram states
            # the cause in a sibling ``category: UNAUTHORIZED`` field rather
            # than in the message.
            auth_hint=looks_like_auth_message(body),
        )
    if isinstance(exc, (TimeoutError, InvalidURI, OSError)):
        return HealthReport(HealthStatus.UNREACHABLE, f"could not connect: {exc}")
    if isinstance(exc, InvalidHandshake):
        # An upgrade that failed without an HTTP status: a proxy or a server
        # that is not speaking websockets at this path.
        return HealthReport(HealthStatus.UNREACHABLE, f"not a websocket endpoint: {exc}")
    return HealthReport(HealthStatus.UNKNOWN, str(exc) or type(exc).__name__)
