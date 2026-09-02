"""Web-layer request/response schemas (API contracts)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from loreline.models import (
    ORIGINAL_VERSION,
    DiarizationConfig,
    OpenRouterRouting,
    Protocol,
    ProviderCaps,
    ProviderKind,
)
from loreline.monitoring.alerts import AlertChannelType, AlertLevel


class LoginRequest(BaseModel):
    """Password login payload."""

    password: str


class ProviderCreate(BaseModel):
    """Create/update payload for an STT provider."""

    name: str
    kind: ProviderKind
    protocol: Protocol
    base_url: str | None = None
    model: str | None = None
    favorite_models: list[str] = Field(default_factory=list[str])
    sample_rate: int = 16000
    language: str = "de"
    capabilities: ProviderCaps = Field(default_factory=ProviderCaps)
    routing: OpenRouterRouting | None = None  # OpenRouter kind only
    enabled: bool = True
    api_key: str | None = Field(
        default=None,
        description="Optional API key set at create/update time; stored write-only.",
    )


class SecretWrite(BaseModel):
    """Write-only secret value for a provider's API key."""

    value: str


class GlossaryWrite(BaseModel):
    """Replace a campaign glossary's terms."""

    terms: list[str] = Field(default_factory=list[str])


class StartSessionRequest(BaseModel):
    """Start a capture session."""

    primary_provider: str
    fallback_provider: str | None = None
    campaign_id: str | None = None
    device: int | str | None = None
    model: str | None = None
    """Override the primary provider's model for this session (chosen on demand)."""
    fallback_model: str | None = None
    """Same, for the fallback provider - it has its own model list."""
    diarization: DiarizationConfig = Field(default_factory=DiarizationConfig)


class DeviceSetting(BaseModel):
    """The persisted default audio input device (device index as a string, or null)."""

    device: str | None = None


class SpeakerNamesUpdate(BaseModel):
    """Per-session speaker rename map ({original diarization label: display name})."""

    names: dict[str, str] = Field(default_factory=dict[str, str])


class SessionIds(BaseModel):
    """A set of session ids for bulk operations (delete / merge)."""

    ids: list[str] = Field(default_factory=list[str])


class ActionDefaults(BaseModel):
    """Per-action defaults surfaced first in the on-demand pickers (blank = none)."""

    stt_provider: str = ""
    stt_model: str = ""
    diar_mode: str = ""
    diar_endpoint: str = ""
    summarize_provider: str = ""
    summarize_model: str = ""
    summarize_prompt: str = ""
    """Summary system prompt; blank means the built-in default (served filled in)."""
    video_provider: str = ""
    video_model: str = ""
    summarize_reasoning_effort: str = ""
    """Default reasoning effort for summaries; blank leaves it to the model."""
    strict_model_filtering: bool = True
    """Hide models that don't look capable of the interaction being picked for.

    On by default, because the common failure it prevents is real: OpenAI's
    ``/models`` lists image and TTS models that cannot transcribe. Turn it off
    to see everything an endpoint offers - needed for a model too new to be
    recognised, or a self-hosted server with its own naming. Only affects the
    guessed name-matching; lists the provider itself scopes (OpenRouter's) stay
    correct either way."""


class SummarizeRequest(BaseModel):
    """Summarize a session with the chosen LLM provider + model."""

    provider_id: str
    model: str | None = None
    reasoning_effort: str | None = None
    """How hard a reasoning model should think. Only meaningful for a model
    that advertises support (ModelInfo.supports_reasoning); ignored otherwise,
    and dropped automatically if the endpoint rejects it."""


class SummarizeResult(BaseModel):
    """The generated session summary."""

    summary: str


class ReprocessRequest(BaseModel):
    """Enqueue a post-session re-processing job."""

    session_id: str
    provider_id: str = ""  # required for "transcribe"; ignored for "diarize"
    operation: Literal["transcribe", "diarize"] = "transcribe"
    diarization: DiarizationConfig = Field(default_factory=DiarizationConfig)
    model: str | None = None
    """Override the provider's model for this job (chosen on demand), "transcribe" only."""
    target: str = ORIGINAL_VERSION
    """Transcript version a "diarize" job relabels ("original" or a transcribe job id)."""


class VideoGenerateRequest(BaseModel):
    """Start a video generation for a session.

    ``prompt`` arrives already edited by the GM - the dialog seeds it from the
    stored summary, but what is sent is whatever they left in the box, so the
    server never re-reads the summary behind their back.

    The optional parameters are exactly those OpenRouter's video API accepts,
    and which of them a given model actually supports comes from
    ``GET /api/video/models`` (see VideoModelInfo). Anything left None is
    omitted from the upstream request rather than guessed at.
    """

    session_id: str
    provider_id: str
    model: str
    prompt: str
    duration: int | None = None  # seconds
    resolution: str | None = None
    aspect_ratio: str | None = None
    generate_audio: bool = False
    seed: int | None = None


class OkResponse(BaseModel):
    """Generic success acknowledgement."""

    ok: bool = True


class AlertChannelWrite(BaseModel):
    """Create/update payload for one alert channel (token is write-only)."""

    type: AlertChannelType
    enabled: bool = True
    min_level: AlertLevel = AlertLevel.WARNING
    server: str = "https://ntfy.sh"
    topic: str | None = None
    chat_id: str | None = None
    url: str | None = None
    token: str | None = Field(
        default=None, description="Write-only token (Telegram bot / ntfy auth)."
    )


class AlertChannelView(BaseModel):
    """One alert channel returned to the UI (token masked as a set/unset flag)."""

    id: str
    type: AlertChannelType
    enabled: bool
    min_level: AlertLevel
    server: str
    topic: str | None
    chat_id: str | None
    url: str | None
    token_set: bool


class AlertTestResult(BaseModel):
    """Delivery outcome of a single channel test."""

    ok: bool


class AutostartState(BaseModel):
    """Whether the systemd unit is enabled to start at boot."""

    enabled: bool


class AutostartUpdate(BaseModel):
    """Toggle systemd autostart."""

    enabled: bool


class RollbackRequest(BaseModel):
    """Roll the deployment back to a prior commit."""

    # Hex-only, no leading `-`: the value is passed straight into `git reset
    # --hard <commit>` argv (see loreline.updater.Updater.rollback). Without
    # this constraint a value like "--upload-pack=..." would be taken as a
    # git option rather than a revision.
    commit: str = Field(pattern=r"^[0-9a-fA-F]{7,40}$")


class RevisionResponse(BaseModel):
    """Current deployed git revision."""

    commit: str | None = None
