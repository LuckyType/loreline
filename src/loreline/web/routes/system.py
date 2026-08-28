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
from starlette.status import HTTP_404_NOT_FOUND, HTTP_503_SERVICE_UNAVAILABLE

from loreline import __version__
from loreline.diarization.remote import probe_health
from loreline.monitoring import (
    AlertChannel,
    channel_token_secret,
    disk_usage,
    overall_status,
)
from loreline.secrets import SecretStore
from loreline.services import DockerUnavailableError, ServiceState
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

# The UI polls /healthz every few seconds; re-probing the diarizer that often
# would be wasteful (and slow when it's down and every probe waits for a
# timeout). Cache the verdict briefly - a diarizer coming up or going away is
# noticed within this window, which is plenty for a status badge.
_DIARIZER_PROBE_TTL_S = 20.0
_diarizer_probe: tuple[str, float, bool] | None = None


async def _diarizer_status(endpoint: str) -> bool:
    """Reachability of ``endpoint``, cached for ``_DIARIZER_PROBE_TTL_S``."""
    global _diarizer_probe  # noqa: PLW0603 - module-level memo, single event loop
    now = time.monotonic()
    if _diarizer_probe is not None:
        cached_endpoint, checked_at, reachable = _diarizer_probe
        if cached_endpoint == endpoint and now - checked_at < _DIARIZER_PROBE_TTL_S:
            return reachable
    reachable = await probe_health(endpoint)
    _diarizer_probe = (endpoint, now, reachable)
    return reachable


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
    diarizer_endpoint: str | None = None
    """The configured remote-diarization endpoint, or null when none is set."""
    diarizer_reachable: bool | None = None
    """Whether that endpoint answered; null when no endpoint is configured."""
    stt_degraded_since: float | None = None
    """Epoch time the active session's live transcription started failing
    (primary and fallback both producing nothing); null when healthy or idle.
    Audio keeps recording either way - this drives the dashboard warning."""


@router.get("/healthz")
async def healthz(request: Request) -> HealthResponse:
    """Return service health. Used by UI badge, push alerts, and external polling."""
    state = get_state(request)
    capture_status = state.manager.status().value
    free, total = disk_usage(state.settings.data_dir)
    threshold = state.settings.disk_alert_threshold_mb * 1024 * 1024
    alert_config = await state.alerts.get_config()

    raw_defaults = await state.settings_repo.get(_DEFAULTS_KEY)
    defaults = (
        ActionDefaults.model_validate_json(raw_defaults) if raw_defaults else ActionDefaults()
    )
    endpoint = defaults.diar_endpoint or None
    reachable = await _diarizer_status(endpoint) if endpoint else None

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
        diarizer_endpoint=endpoint,
        diarizer_reachable=reachable,
        stt_degraded_since=state.manager.stt_degraded_since(),
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


class ServiceLogs(BaseModel):
    """Recent container output for one service."""

    name: str
    logs: str


class ServiceAction(BaseModel):
    """Start or stop a service."""

    running: bool


@router.get("/services", dependencies=_auth)
async def list_services(request: Request) -> list[ServiceState]:
    """The containers in this compose project, with their current state."""
    services = get_state(request).services
    if not services.enabled:
        return []
    try:
        return await services.list_services()
    except DockerUnavailableError as exc:
        raise HTTPException(status_code=HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc


@router.post("/services/{name}", dependencies=_auth)
async def set_service_running(request: Request, name: str, body: ServiceAction) -> ServiceState:
    """Start or stop one of the optional services."""
    try:
        return await get_state(request).services.set_running(name, running=body.running)
    except DockerUnavailableError as exc:
        raise HTTPException(status_code=HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc


@router.get("/services/{name}/logs", dependencies=_auth)
async def service_logs(request: Request, name: str, tail: int = 200) -> ServiceLogs:
    """Recent stdout/stderr for one service's container."""
    try:
        return ServiceLogs(name=name, logs=await get_state(request).services.logs(name, tail=tail))
    except DockerUnavailableError as exc:
        raise HTTPException(status_code=HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
