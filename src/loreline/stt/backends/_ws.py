"""Shared helpers for WebSocket-based STT connectors."""

from __future__ import annotations

import asyncio
import json
from typing import cast

from websockets.asyncio.client import ClientConnection


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


async def probe_health(ws: ClientConnection, prompt: str | None, *, timeout_s: float = 5.0) -> bool:
    """Decide reachability/auth from an already-connected STT socket.

    Connecting performs the auth handshake - a bad key raises during ``connect``
    for the header-auth providers we use. This additionally reads one server
    frame, returning ``False`` on an explicit ``error`` frame (a key rejected
    *after* the handshake) and letting an immediate close propagate as an error
    to the caller. A clean timeout (a session that simply waits for audio) is
    treated as healthy.
    """
    if prompt is not None:
        await ws.send(prompt)
    try:
        async with asyncio.timeout(timeout_s):
            raw = await ws.recv()
    except TimeoutError:
        return True
    return get_str(as_dict(raw), "type").lower() != "error"
