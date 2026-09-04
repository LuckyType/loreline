"""Shared helpers for WebSocket-based STT connectors."""

from __future__ import annotations

import asyncio
import json
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


def as_dict(raw: str | bytes) -> dict[str, object]:
    """Parse a JSON message into a string-keyed dict (empty if not an object)."""
    data: object = json.loads(raw)
    if isinstance(data, dict):
        return cast("dict[str, object]", data)
    return {}


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
    What arrives after the upgrade is graded by :func:`probe_health`.

    ``read_timeout_s`` bounds the wait for the first reply and defaults to
    :data:`SOCKET_READ_TIMEOUT_S` at call time, so a test can shorten the
    quiet-server case without waiting the real five seconds.
    """
    if read_timeout_s is None:
        read_timeout_s = SOCKET_READ_TIMEOUT_S
    try:
        async with asyncio.timeout(timeout_s):
            async with connect(url, additional_headers=headers) as ws:
                return await probe_health(ws, first_frame, timeout_s=read_timeout_s)
    except TimeoutError:
        return HealthReport(HealthStatus.UNREACHABLE, "the socket did not open in time")
    except (OSError, WebSocketException) as exc:
        return classify_handshake_error(exc)


async def probe_health(
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
