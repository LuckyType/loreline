"""Audio routes: input-device enumeration + a live input-level meter.

``GET /api/audio/devices`` lists selectable mics. ``WS /ws/audio/level`` opens a
short-lived capture stream for the chosen device and pushes peak/RMS levels so
the UI can show a meter (set gain / pick the right mic) before starting a
session. Both require the ``audio`` extra; without it the device list is empty
and the level socket reports an error and closes.
"""

from __future__ import annotations

import time
from collections.abc import AsyncGenerator
from contextlib import suppress
from typing import cast

from fastapi import APIRouter, Depends, Request, WebSocket, WebSocketDisconnect
from starlette.status import WS_1008_POLICY_VIOLATION

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


@router.websocket("/ws/audio/level")
async def audio_level_ws(ws: WebSocket) -> None:
    """Stream ``{peak, rms}`` (0-1) for the selected device until disconnected."""
    settings = ws.app.state.ctx.settings
    if auth_enabled(settings):
        token = ws.cookies.get(COOKIE_NAME)
        if not token or not verify_token(token, settings):
            await ws.close(code=WS_1008_POLICY_VIOLATION)
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
