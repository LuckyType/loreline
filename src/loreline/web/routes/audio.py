"""Audio routes: input-device enumeration, input-level meters, browser capture.

``GET /api/audio/devices`` lists selectable mics. ``WS /ws/audio/level`` opens a
short-lived capture stream for the chosen device and pushes peak/RMS levels so
the UI can show a meter (set gain / pick the right mic) before starting a
session - it requires the ``audio`` extra; without it the device list is empty
and the level socket reports an error and closes. ``WS /ws/audio/live-level``
is the same meter for a session already in progress: it opens no device of its
own, and instead relays the throttled readings ``SessionManager`` already takes
from the frames flowing through the active capture.

All three of those are about a microphone on this machine. ``WS
/ws/audio/capture`` is the other direction: the browser is the microphone and
this is where its frames arrive (see ``docs/adr/0010``). It carries audio
rather than serving it, which is why it is the one socket here that reads.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncGenerator
from contextlib import suppress
from typing import cast

from fastapi import APIRouter, Depends, Request, WebSocket, WebSocketDisconnect
from starlette.status import (
    WS_1003_UNSUPPORTED_DATA,
    WS_1008_POLICY_VIOLATION,
    WS_1013_TRY_AGAIN_LATER,
)

from loreline.audio.client_source import ClientHello, ClientMic, ClientMicBusyError
from loreline.audio.devices import InputDevice, list_input_devices
from loreline.audio.level import levels
from loreline.logging import get_logger
from loreline.web.auth import COOKIE_NAME, auth_enabled, require_auth, verify_token
from loreline.web.deps import get_state
from loreline.web.schemas import DeviceSetting, OkResponse
from loreline.web.ws_util import stream_until_disconnected

log = get_logger(__name__)

router = APIRouter(tags=["audio"])

_SEND_INTERVAL_S = 0.05  # throttle level pushes to ~20 Hz
INPUT_DEVICE_KEY = "input_device"  # kv_settings key for the default capture device

# How long the hello may take to arrive after the socket opens. A browser sends
# it in the same tick it connects; a peer that does not is not one of ours, and
# leaving the socket open would hold the capture slot against the tab that is.
_HELLO_TIMEOUT_S = 10.0
# Device labels come from the browser, so they are untrusted text that ends up
# in a log line. Long enough for any real device name, short enough that a
# hostile client cannot write an essay into every session's log.
_MAX_LABEL_CHARS = 120
# A close frame's reason is capped at 123 bytes by the protocol itself, and a
# frame over it is dropped rather than truncated - which would cost the tab the
# one sentence explaining why it was closed.
_MAX_CLOSE_REASON_CHARS = 110


@router.get("/api/audio/devices", dependencies=[Depends(require_auth)])
async def audio_devices() -> list[InputDevice]:
    """List available audio input devices (empty without the ``audio`` extra)."""
    return list_input_devices()


@router.get("/api/audio/device", dependencies=[Depends(require_auth)])
async def get_input_device(request: Request) -> DeviceSetting:
    """Return the persisted default input device (null = system default)."""
    stored = await get_state(request).settings_repo.get(INPUT_DEVICE_KEY)
    return DeviceSetting(device=stored or None)


@router.put("/api/audio/device", dependencies=[Depends(require_auth)])
async def set_input_device(request: Request, body: DeviceSetting) -> OkResponse:
    """Persist the default input device used when starting a session."""
    await get_state(request).settings_repo.set(INPUT_DEVICE_KEY, body.device or "")
    return OkResponse()


def parse_device(raw: str | None) -> int | str | None:
    """Coerce a stored/query device value to an index (int) or name (str), or None."""
    if not raw:
        return None
    return int(raw) if raw.lstrip("-").isdigit() else raw


async def _authenticated(ws: WebSocket) -> bool:
    """Close an unauthenticated socket, the same cookie check the others make."""
    settings = ws.app.state.ctx.settings
    if not auth_enabled(settings):
        return True
    token = ws.cookies.get(COOKIE_NAME)
    if token and verify_token(token, settings):
        return True
    await ws.close(code=WS_1008_POLICY_VIOLATION)
    return False


def _hello_from(payload: object) -> ClientHello:
    """Read the browser's opening message, or say what is wrong with it."""
    if not isinstance(payload, dict):
        msg = "the first message must be a JSON object describing the audio"
        raise ValueError(msg)
    data = cast("dict[str, object]", payload)
    rate = data.get("sample_rate")
    channels = data.get("channels", 1)
    label = data.get("label", "")
    if not isinstance(rate, int) or isinstance(rate, bool):
        msg = "sample_rate is required and must be a whole number of hertz"
        raise ValueError(msg)
    if not isinstance(channels, int) or isinstance(channels, bool):
        msg = "channels must be a whole number"
        raise ValueError(msg)
    return ClientHello(
        sample_rate=rate,
        channels=channels,
        label=str(label)[:_MAX_LABEL_CHARS] if isinstance(label, str) else "",
    ).validated()


@router.websocket("/ws/audio/capture")
async def audio_capture_ws(ws: WebSocket) -> None:
    """Receive a browser's microphone as PCM16 frames, for the client source.

    The protocol is deliberately the smallest thing that answers the two
    questions a frame cannot: one JSON hello (``sample_rate``, ``channels``,
    an optional device ``label`` for the log), a JSON acknowledgement back, and
    then binary PCM16 frames until the tab goes away.

    Frames arriving while no session is running are discarded, and only their
    arrival time is kept (see ``ClientMic``). That is what lets a GM open the
    microphone, watch the meter and press Start afterwards: opening early costs
    nothing and is what makes the capture pre-flight answerable.

    At most one browser at a time. A second is closed with 1013 and a reason
    the tab can read out, never swapped in behind the first: the running
    session's audio would change hands with nothing on screen to say so.
    """
    if not await _authenticated(ws):
        return
    mic: ClientMic = ws.app.state.ctx.client_mic
    await ws.accept()

    try:
        payload = await asyncio.wait_for(ws.receive_json(), timeout=_HELLO_TIMEOUT_S)
        hello = _hello_from(payload)
    except (TimeoutError, ValueError, KeyError, TypeError) as exc:
        reason = (str(exc) if isinstance(exc, ValueError) else "no audio description was sent")[
            :_MAX_CLOSE_REASON_CHARS
        ]
        log.warning("audio.client.hello_rejected", error=reason)
        with suppress(Exception):
            await ws.close(code=WS_1003_UNSUPPORTED_DATA, reason=reason)
        return
    except WebSocketDisconnect:
        return

    try:
        token = mic.connect(hello)
    except ClientMicBusyError:
        log.warning("audio.client.refused_busy")
        with suppress(Exception):
            await ws.close(
                code=WS_1013_TRY_AGAIN_LATER,
                reason="another tab or device is already streaming audio to Loreline",
            )
        return

    try:
        await ws.send_json({"ok": True, "sample_rate": hello.sample_rate})
        while True:
            message = await ws.receive()
            if message["type"] == "websocket.disconnect":
                return
            chunk = message.get("bytes")
            if chunk:
                mic.feed(token, chunk)
    except WebSocketDisconnect:
        return
    finally:
        mic.disconnect(token)


@router.websocket("/ws/audio/level")
async def audio_level_ws(ws: WebSocket) -> None:
    """Stream ``{peak, rms}`` (0-1) for the selected device until disconnected."""
    if not await _authenticated(ws):
        return

    device = parse_device(ws.query_params.get("device"))
    await ws.accept()

    from loreline.audio.capture import SoundDeviceSource  # noqa: PLC0415

    source = SoundDeviceSource(device=device, sample_rate=16000)
    frames = cast("AsyncGenerator[tuple[bytes, float], None]", source.frames())
    hold = 0.0
    last = 0.0
    try:
        async for frame, _ts in stream_until_disconnected(ws, frames):
            peak, rms = levels(frame)
            hold = max(hold, peak)
            now = time.monotonic()
            if now - last >= _SEND_INTERVAL_S:
                await ws.send_json({"peak": hold, "rms": rms})
                last = now
                hold = 0.0
    except WebSocketDisconnect:
        return
    except Exception as exc:  # missing audio extra / device unavailable
        log.warning("audio.level.failed", error=str(exc))
        with suppress(Exception):
            await ws.send_json({"error": str(exc)})
    finally:
        source.stop()
        with suppress(Exception):
            await frames.aclose()


@router.websocket("/ws/audio/live-level")
async def audio_live_level_ws(ws: WebSocket) -> None:
    """Stream the active session's throttled ``{peak, rms}`` (0-1) meter.

    The dashboard's live gain meter while a session is capturing. Unlike
    ``/ws/audio/level`` above this opens no device of its own - it relays
    whatever ``SessionManager`` already computed from the frames flowing
    through the running capture (see ``_LevelWatch`` there), because a second
    simultaneous reader on the same device fails outright on some hardware
    (see the mic-resampling fix). Silent while idle: nothing is published to
    the bus outside an active capture, so the socket simply waits.
    """
    if not await _authenticated(ws):
        return

    await ws.accept()
    manager = ws.app.state.ctx.manager
    try:
        async with manager.level_bus.subscribe() as stream:
            async for peak, rms in stream_until_disconnected(ws, stream):
                await ws.send_json({"peak": peak, "rms": rms})
    except WebSocketDisconnect:
        return
