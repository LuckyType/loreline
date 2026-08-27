"""System routes: health, self-update, autostart, and push-alert config.

``/healthz`` stays unauthenticated (external pollers / uptime checks); the
mutating ops endpoints require auth.
"""

from __future__ import annotations

import time
import uuid

from fastapi import APIRouter, Depends, Request
from fastapi.exceptions import HTTPException
from pydantic import BaseModel
from starlette.status import HTTP_404_NOT_FOUND

from loreline import __version__
from loreline.monitoring import (
    AlertChannel,
    channel_token_secret,
    disk_usage,
    overall_status,
)
from loreline.secrets import SecretStore
from loreline.updater import UpdateResult
from loreline.web.auth import require_auth
from loreline.web.deps import get_state
from loreline.web.schemas import (
    ActionDefaults,
    AlertChannelView,
    AlertChannelWrite,
    AlertTestResult,
    AutostartState,
    AutostartUpdate,
    OkResponse,
    RevisionResponse,
    RollbackRequest,
)

router = APIRouter(prefix="/api/system", tags=["system"])
_auth = [Depends(require_auth)]
_DEFAULTS_KEY = "action_defaults"  # kv_settings key for the per-action default models/mode


class HealthResponse(BaseModel):
    """Health/status payload for monitoring and the UI status badge."""

    status: str
    version: str
    uptime_seconds: float
    capture_status: str
    active_session_id: str | None = None
    disk_free_bytes: int = 0
    disk_total_bytes: int = 0
    alerts_enabled: bool = False


@router.get("/healthz")
async def healthz(request: Request) -> HealthResponse:
    """Return service health. Used by UI badge, push alerts, and external polling."""
    state = get_state(request)
    capture_status = state.manager.status().value
    free, total = disk_usage(state.settings.data_dir)
    threshold = state.settings.disk_alert_threshold_mb * 1024 * 1024
    alert_config = await state.alerts.get_config()
    return HealthResponse(
        status=overall_status(
            capture_status=capture_status, disk_free=free, disk_threshold_bytes=threshold
        ),
        version=__version__,
        uptime_seconds=round(time.monotonic() - state.started_at, 3),
        capture_status=capture_status,
        active_session_id=state.manager.current_session_id(),
        disk_free_bytes=free,
        disk_total_bytes=total,
        alerts_enabled=any(c.enabled for c in alert_config.channels),
    )


@router.get("/revision", dependencies=_auth)
async def revision(request: Request) -> RevisionResponse:
    """Return the currently deployed git commit."""
    return RevisionResponse(commit=await get_state(request).updater.current_revision())


@router.post("/update", dependencies=_auth)
async def trigger_update(request: Request) -> UpdateResult:
    """Run the self-update (git pull + uv sync + restart)."""
    return await get_state(request).updater.update()


@router.post("/rollback", dependencies=_auth)
async def rollback(request: Request, body: RollbackRequest) -> UpdateResult:
    """Roll the deployment back to a prior commit."""
    return await get_state(request).updater.rollback(body.commit)


@router.get("/autostart", dependencies=_auth)
async def get_autostart(request: Request) -> AutostartState:
    """Report whether the systemd unit is enabled at boot."""
    return AutostartState(enabled=await get_state(request).autostart.is_enabled())


@router.put("/autostart", dependencies=_auth)
async def set_autostart(request: Request, body: AutostartUpdate) -> AutostartState:
    """Enable or disable systemd autostart."""
    return AutostartState(enabled=await get_state(request).autostart.set_enabled(body.enabled))


@router.get("/defaults", dependencies=_auth)
async def get_defaults(request: Request) -> ActionDefaults:
    """Return the per-action default models/mode used to pre-select the pickers."""
    raw = await get_state(request).settings_repo.get(_DEFAULTS_KEY)
    return ActionDefaults.model_validate_json(raw) if raw else ActionDefaults()


@router.put("/defaults", dependencies=_auth)
async def set_defaults(request: Request, body: ActionDefaults) -> ActionDefaults:
    """Persist the per-action defaults."""
    await get_state(request).settings_repo.set(_DEFAULTS_KEY, body.model_dump_json())
    return body


def _channel_view(channel: AlertChannel, secrets: SecretStore) -> AlertChannelView:
    return AlertChannelView(
        id=channel.id,
        type=channel.type,
        enabled=channel.enabled,
        min_level=channel.min_level,
        server=channel.server,
        topic=channel.topic,
        chat_id=channel.chat_id,
        url=channel.url,
        token_set=secrets.get(channel_token_secret(channel.id)) is not None,
    )


@router.get("/alerts/channels", dependencies=_auth)
async def list_alert_channels(request: Request) -> list[AlertChannelView]:
    """List configured alert channels (tokens masked as set/unset)."""
    state = get_state(request)
    config = await state.alerts.get_config()
    return [_channel_view(c, state.secrets) for c in config.channels]


@router.post("/alerts/channels", status_code=201, dependencies=_auth)
async def create_alert_channel(request: Request, body: AlertChannelWrite) -> AlertChannelView:
    """Add an alert channel; store its token write-only when supplied."""
    state = get_state(request)
    config = await state.alerts.get_config()
    channel = AlertChannel(id=uuid.uuid4().hex, **body.model_dump(exclude={"token"}))
    config.channels.append(channel)
    await state.alerts.set_config(config)
    if body.token:
        state.secrets.set(channel_token_secret(channel.id), body.token)
    return _channel_view(channel, state.secrets)


@router.put("/alerts/channels/{channel_id}", dependencies=_auth)
async def update_alert_channel(
    request: Request, channel_id: str, body: AlertChannelWrite
) -> AlertChannelView:
    """Replace an alert channel; set its token too when one is supplied."""
    state = get_state(request)
    config = await state.alerts.get_config()
    index = next((i for i, c in enumerate(config.channels) if c.id == channel_id), None)
    if index is None:
        raise HTTPException(status_code=HTTP_404_NOT_FOUND, detail="alert channel not found")
    channel = AlertChannel(id=channel_id, **body.model_dump(exclude={"token"}))
    config.channels[index] = channel
    await state.alerts.set_config(config)
    if body.token:
        state.secrets.set(channel_token_secret(channel_id), body.token)
    return _channel_view(channel, state.secrets)


@router.delete("/alerts/channels/{channel_id}", dependencies=_auth)
async def delete_alert_channel(request: Request, channel_id: str) -> OkResponse:
    """Delete an alert channel and its stored token."""
    state = get_state(request)
    config = await state.alerts.get_config()
    remaining = [c for c in config.channels if c.id != channel_id]
    if len(remaining) == len(config.channels):
        raise HTTPException(status_code=HTTP_404_NOT_FOUND, detail="alert channel not found")
    config.channels = remaining
    await state.alerts.set_config(config)
    state.secrets.delete(channel_token_secret(channel_id))
    return OkResponse()


@router.post("/alerts/channels/{channel_id}/test", dependencies=_auth)
async def test_alert_channel(request: Request, channel_id: str) -> AlertTestResult:
    """Send a test notification to one channel."""
    return AlertTestResult(ok=await get_state(request).alerts.test_channel(channel_id))
