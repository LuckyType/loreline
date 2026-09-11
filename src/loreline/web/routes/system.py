"""System routes: health, self-update, autostart, and push-alert config.

``/livez`` is the only unauthenticated route here: it says the process is up
and nothing else, which is all an external poller needs. Everything else,
``/healthz`` and its operational snapshot included, requires auth.
"""

from __future__ import annotations

import time
import uuid

from fastapi import APIRouter, Depends, Request
from fastapi.exceptions import HTTPException
from pydantic import BaseModel
from starlette.status import HTTP_404_NOT_FOUND, HTTP_409_CONFLICT, HTTP_503_SERVICE_UNAVAILABLE

from loreline import __version__
from loreline.diarization.remote import probe_diarizer
from loreline.health import HealthReport, HealthStatus
from loreline.llm import DEFAULT_RECAP_PROMPT, DEFAULT_SCENE_PROMPT, DEFAULT_SYSTEM_PROMPT
from loreline.models import CaptureSourceKind, SessionStatus
from loreline.monitoring import (
    AlertChannel,
    channel_token_secret,
    disk_usage,
    overall_status,
)
from loreline.secrets import SecretStore
from loreline.services import DockerUnavailableError, ServiceState
from loreline.updater import AutostartToggleError, AutostartUnavailableError, UpdateResult
from loreline.web.auth import require_auth
from loreline.web.deps import ACTION_DEFAULTS_KEY, get_state, load_action_defaults
from loreline.web.schemas import (
    ActionDefaults,
    AlertChannelView,
    AlertChannelWrite,
    AlertTestResult,
    AutostartState,
    AutostartUpdate,
    LivenessResponse,
    OkResponse,
    RevisionResponse,
    RollbackRequest,
)

router = APIRouter(prefix="/api/system", tags=["system"])
_auth = [Depends(require_auth)]

# The UI polls /healthz every few seconds; re-probing the diarizer that often
# would be wasteful (and slow when it's down and every probe waits for a
# timeout). Cache the verdict briefly - a diarizer coming up or going away is
# noticed within this window, which is plenty for a status badge.
_DIARIZER_PROBE_TTL_S = 20.0
_diarizer_probe: tuple[str, float, HealthReport] | None = None


async def _diarizer_status(endpoint: str) -> HealthReport:
    """The graded probe of ``endpoint``, cached for ``_DIARIZER_PROBE_TTL_S``."""
    global _diarizer_probe  # noqa: PLW0603 - module-level memo, single event loop
    now = time.monotonic()
    if _diarizer_probe is not None:
        cached_endpoint, checked_at, report = _diarizer_probe
        if cached_endpoint == endpoint and now - checked_at < _DIARIZER_PROBE_TTL_S:
            return report
    report = await probe_diarizer(endpoint)
    _diarizer_probe = (endpoint, now, report)
    return report


class HealthResponse(BaseModel):
    """Health/status payload for monitoring and the UI status badge."""

    status: str
    version: str
    uptime_seconds: float
    capture_status: SessionStatus
    """The capture lifecycle state, as the enum rather than a bare string: the
    settings page and the header badge both switch on it, and a typed field is
    what makes a renamed state a compile error in the browser rather than a
    condition that silently stops matching."""
    active_session_id: str | None = None
    disk_free_bytes: int = 0
    disk_total_bytes: int = 0
    alerts_enabled: bool = False
    diarizer_endpoint: str | None = None
    """The configured remote-diarization endpoint, or null when none is set."""
    diarizer_reachable: bool | None = None
    """Whether something answered at that endpoint; null when no endpoint is
    configured. Derived from ``diarizer_status``: everything but
    ``unreachable`` answered, including a service that answered badly."""
    diarizer_status: HealthStatus | None = None
    """The probe's graded verdict on the endpoint, the same five states the
    settings page renders for a provider; null when no endpoint is configured.
    A bool cannot say that a service answered 503 while its model loads."""
    diarizer_detail: str | None = None
    """The service's own words on why it is not healthy, where it gave any."""
    stt_degraded_since: float | None = None
    """Epoch time the active session's live transcription started failing
    (primary and fallback both producing nothing); null when healthy or idle.
    Audio keeps recording either way - this drives the dashboard warning."""
    stt_error: str | None = None
    """Why the active session stopped transcribing for good, in the vendor's own
    words ("OpenAI: You have no credits remaining."); null while any provider
    still works. Set only for failures that repeat for every utterance, so the
    dashboard can say what to fix rather than only that something is wrong."""
    captured_seconds: float | None = None
    """Seconds of audio the active session has captured; null while idle. With
    ``capture_last_frame_age`` this is the dashboard's proof that a recording is
    a recording: "capturing" says only that the machinery was started."""
    capture_last_frame_age: float | None = None
    """Seconds since the microphone last delivered a frame; null while idle.
    A device sends frames whether or not anyone is speaking, so this stays near
    zero all evening on a healthy capture and climbs on a dead one - which is
    the difference between a quiet table and a microphone that stopped."""
    capture_source: CaptureSourceKind | None = None
    """Which microphone the active session is listening to; null while idle.
    ``client`` means a browser is the source (``docs/adr/0010``), which every
    other screen has to know: a phone opening the dashboard sees a session
    that is capturing and must not be told it is the one holding it up, while
    the tab that *is* the source has to be told that closing it ends the
    recording."""


@router.get("/livez")
async def livez() -> LivenessResponse:
    """Say that this process is up. The one route here that needs no cookie.

    Empty on purpose: it touches no disk, no database and no diarizer, so it
    cannot fail for a reason that has nothing to do with the process being
    alive, and it hands an anonymous caller nothing about the deployment. That
    is what makes it safe to leave open for the installer's start-up poll and
    for any uptime check pointed at the box.
    """
    return LivenessResponse()


@router.get("/healthz", dependencies=_auth)
async def healthz(request: Request) -> HealthResponse:
    """Return the full operational snapshot, for the UI badge and push alerts.

    Behind auth, unlike ``/livez`` above: the exact version, free disk, capture
    state, the operator's own diarizer endpoint and the STT vendor's raw error
    text add up to a reconnaissance report, and this app publishes its port on
    the LAN. It also makes the shell's guard real - the SPA layout already
    redirects to /login when this call answers 401, a branch that could not be
    reached while the route was open.
    """
    state = get_state(request)
    capture_status = state.manager.status()
    free, total = disk_usage(state.settings.data_dir)
    threshold = state.settings.disk_alert_threshold_bytes
    alert_config = await state.alerts.get_config()

    defaults = await load_action_defaults(state)
    endpoint = defaults.diar_endpoint or None
    diarizer = await _diarizer_status(endpoint) if endpoint else None

    return HealthResponse(
        status=overall_status(
            capture_status=capture_status.value, disk_free=free, disk_threshold_bytes=threshold
        ),
        version=__version__,
        uptime_seconds=round(time.monotonic() - state.started_at, 3),
        capture_status=capture_status,
        active_session_id=state.manager.current_session_id(),
        disk_free_bytes=free,
        disk_total_bytes=total,
        alerts_enabled=any(c.enabled for c in alert_config.channels),
        diarizer_endpoint=endpoint,
        diarizer_reachable=(
            diarizer.status is not HealthStatus.UNREACHABLE if diarizer is not None else None
        ),
        diarizer_status=diarizer.status if diarizer is not None else None,
        diarizer_detail=diarizer.detail if diarizer is not None else None,
        stt_degraded_since=state.manager.stt_degraded_since(),
        stt_error=state.manager.stt_error(),
        captured_seconds=_rounded(state.manager.captured_seconds()),
        capture_last_frame_age=_rounded(state.manager.capture_last_frame_age()),
        capture_source=state.manager.capture_source(),
    )


def _rounded(value: float | None) -> float | None:
    """Trim a duration to milliseconds; the UI shows whole seconds of it."""
    return None if value is None else round(value, 3)


class DiarizerProbeResponse(BaseModel):
    """The graded probe of one diarization endpoint, checked on demand.

    The same verdict shape ``HealthResponse`` already carries for the stored
    default, but for whatever endpoint the caller names. The capture panel
    uses this to check the value currently typed into its endpoint field,
    which ``/healthz`` cannot: that call only ever probes
    ``defaults.diar_endpoint``, the last *saved* value, never a value someone
    is only trying out.
    """

    reachable: bool
    """Whether something answered, graded the same way as ``diarizer_reachable``
    above: everything but ``unreachable`` counts, including a service that
    answered badly."""
    status: HealthStatus
    detail: str | None = None


@router.get("/diarizer/probe", dependencies=_auth)
async def probe_diarizer_endpoint(endpoint: str) -> DiarizerProbeResponse:
    """Probe an arbitrary diarization endpoint, uncached and on demand.

    Deliberately not routed through ``_diarizer_status`` above: that cache
    exists because ``/healthz`` is polled every few seconds for one settings
    value, while this answers a one-off question about whatever a GM is
    currently typing into the capture panel's endpoint field - a different
    string on every keystroke, which a cache keyed for a single settings value
    would not help and could even answer wrong (a stale verdict for a value
    that has since changed back).

    Requires auth like every other route here: naming an endpoint makes the
    server issue an arbitrary outbound HTTP request, which is already true of
    the stored default's probe behind ``/healthz`` above - this is the same
    exposure on a second, caller-supplied value, not a new one.
    """
    report = await probe_diarizer(endpoint)
    return DiarizerProbeResponse(
        reachable=report.status is not HealthStatus.UNREACHABLE,
        status=report.status,
        detail=report.detail,
    )


@router.get("/revision", dependencies=_auth)
async def revision(request: Request) -> RevisionResponse:
    """Return the deployed commit, `git describe`'s name for it, and the way back.

    Both, rather than one: the SHA is what /rollback takes and what the update
    result reports, while the described string is the only one of the two worth
    reading off a screen. Either can be null where this deployment cannot know
    it - a Docker image built without the revision baked in - and the UI is
    written to show a dash for that instead of a wrong answer.

    The previous pair is the same two answers for the commit HEAD was on before
    it last moved, which is what Settings > Client offers to roll back to, and
    ``rollback_unavailable`` is the one sentence a Docker deployment shows in
    place of that offer. Answered here rather than on a route of its own so the
    page learns everything it needs about the deployment in one call.
    """
    updater = get_state(request).updater
    previous = await updater.previous_revision()
    return RevisionResponse(
        commit=await updater.current_revision(),
        described=await updater.current_described(),
        previous_commit=previous,
        previous_described=await updater.previous_described() if previous else None,
        rollback_unavailable=updater.rollback_unavailable(),
    )


@router.post("/update", dependencies=_auth)
async def trigger_update(request: Request) -> UpdateResult:
    """Run the self-update: git pull + uv sync + restart, or the updater service in Docker."""
    return await get_state(request).updater.update()


@router.post("/rollback", dependencies=_auth)
async def rollback(request: Request, body: RollbackRequest) -> UpdateResult:
    """Roll the deployment back to a prior commit."""
    return await get_state(request).updater.rollback(body.commit)


@router.get("/autostart", dependencies=_auth)
async def get_autostart(request: Request) -> AutostartState:
    """Report whether the systemd unit is enabled at boot."""
    try:
        return AutostartState(enabled=await get_state(request).autostart.is_enabled())
    except AutostartUnavailableError as exc:
        raise HTTPException(status_code=HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc


@router.put("/autostart", dependencies=_auth)
async def set_autostart(request: Request, body: AutostartUpdate) -> AutostartState:
    """Enable or disable systemd autostart."""
    try:
        enabled = await get_state(request).autostart.set_enabled(body.enabled)
    except AutostartUnavailableError as exc:
        raise HTTPException(status_code=HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    except AutostartToggleError as exc:
        raise HTTPException(status_code=HTTP_409_CONFLICT, detail=str(exc)) from exc
    return AutostartState(enabled=enabled)


# The prompt fields that are editable copies of a built-in text, and the text
# each one falls back to. Stated once because there are three of them now and
# the rule is the same for all three in both directions: served filled in so
# the settings page shows concrete instructions, stored blank when it still
# says what the built-in text says, so an untouched field keeps tracking later
# improvements to that text rather than pinning today's copy.
_BUILT_IN_PROMPTS: dict[str, str] = {
    "summarize_prompt": DEFAULT_SYSTEM_PROMPT,
    "recap_prompt": DEFAULT_RECAP_PROMPT,
    "scene_prompt": DEFAULT_SCENE_PROMPT,
}


def _filled_in(defaults: ActionDefaults) -> ActionDefaults:
    """A copy with every blank prompt showing the built-in text it stands for."""
    filled = {
        field: text
        for field, text in _BUILT_IN_PROMPTS.items()
        if not getattr(defaults, field).strip()
    }
    return defaults.model_copy(update=filled) if filled else defaults


def _blanked(defaults: ActionDefaults) -> ActionDefaults:
    """A copy with every prompt that still says the built-in text stored blank."""
    blanked = {
        field: ""
        for field, text in _BUILT_IN_PROMPTS.items()
        if getattr(defaults, field).strip() in ("", text)
    }
    return defaults.model_copy(update=blanked) if blanked else defaults


@router.get("/defaults", dependencies=_auth)
async def get_defaults(request: Request) -> ActionDefaults:
    """Return the per-action default models/mode used to pre-select the pickers.

    A blank stored prompt is served as the built-in default text, so the
    settings UI always shows the concrete, editable instructions - clearing the
    field and saving is the reset-to-default gesture.
    """
    return _filled_in(await load_action_defaults(get_state(request)))


@router.put("/defaults", dependencies=_auth)
async def set_defaults(request: Request, body: ActionDefaults) -> ActionDefaults:
    """Persist the per-action defaults.

    A prompt equal to its built-in default (or blank) is stored blank; see
    :data:`_BUILT_IN_PROMPTS`. The response mirrors GET: served filled in.
    """
    stored = _blanked(body)
    await get_state(request).settings_repo.set(ACTION_DEFAULTS_KEY, stored.model_dump_json())
    return _filled_in(stored)


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
    """Send a test notification to one channel, and say why when it fails.

    Always 200, failure included: the request itself succeeded, and the answer
    to "did this channel take it" is the body. Turning a refused webhook into a
    5xx here would make the page's own error path swallow the reason.
    """
    result = await get_state(request).alerts.test_channel(channel_id)
    return AlertTestResult(ok=result.ok, detail=result.detail)


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
