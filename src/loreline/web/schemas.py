"""Web-layer request/response schemas (API contracts)."""

from __future__ import annotations

from typing import Literal, Self
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, ValidationInfo, field_validator, model_validator

from loreline.models import (
    ORIGINAL_VERSION,
    DiarizationConfig,
    OpenRouterRouting,
    ProviderKind,
)
from loreline.monitoring.alerts import AlertChannelType, AlertLevel


class LoginRequest(BaseModel):
    """Password login payload."""

    password: str


def _credential(value: str | None) -> str | None:
    """A credential as it will be sent, or None when there is none to send.

    Surrounding whitespace is stripped rather than trusted: a key is pasted, and
    a browser field, a password manager and a terminal all bring a trailing
    space or newline along with it. Every surface here spends the key on an HTTP
    header, and a header value cannot start or end with whitespace - h11 refuses
    to serialise one - so the untrimmed copy is not a credential at all, it is a
    request that cannot be built.

    Blank after trimming is therefore None, not "": three spaces typed into the
    key field used to save as a real key, mask itself as "•••" in the table and
    say "blank = keep current" on the way back in, while every request built
    from it died with ``Illegal header value b'Bearer    '``. Answering None
    makes that case identical to the empty one, which has always behaved
    correctly (no secret written, and the row honestly shows none).
    """
    trimmed = (value or "").strip()
    return trimmed or None


class ProviderCreate(BaseModel):
    """Create/update payload for an STT provider."""

    name: str
    kind: ProviderKind
    base_url: str | None = None
    # No `model`: a provider row serves every interaction its kind declares, so
    # it cannot hold one model. See ProviderConfig in src/loreline/models.py.
    favorite_models: list[str] = Field(default_factory=list[str])
    sample_rate: int = 16000
    language: str = "de"
    routing: OpenRouterRouting | None = None  # OpenRouter kind only
    enabled: bool = True
    api_key: str | None = Field(
        default=None,
        description="Optional API key set at create/update time; stored write-only.",
    )

    @field_validator("api_key")
    @classmethod
    def _usable_key_or_none(cls, value: str | None) -> str | None:
        """Normalise here, so the API is safe whichever client is calling.

        The routes store the key with a bare ``if body.api_key``, which is the
        right rule; it was simply being handed something truthy that was not a
        key. Fixing it at the schema covers create and update in one place, and
        covers a client that is not this app's own wizard.
        """
        return _credential(value)


class SecretWrite(BaseModel):
    """Write-only secret value for a provider's API key."""

    value: str

    @field_validator("value")
    @classmethod
    def _reject_a_blank_secret(cls, value: str) -> str:
        """Refuse a value that is not a credential, rather than storing it.

        This route means "store this key", and there is nothing to normalise a
        blank one into: writing it would leave the row reporting a stored
        secret it cannot authenticate with, and silently writing nothing while
        answering ``ok`` would be the same lie by another route. Deleting the
        key is a different request (DELETE the provider's secret is the
        provider delete), so this simply refuses.
        """
        credential = _credential(value)
        if credential is None:
            raise ValueError("an API key cannot be blank")
        return credential


class GlossaryWrite(BaseModel):
    """Replace a campaign glossary's terms."""

    terms: list[str] = Field(default_factory=list[str])


class StartSessionRequest(BaseModel):
    """Start a capture session."""

    primary_provider: str
    fallback_provider: str | None = None
    campaign_id: str | None = None
    device: int | str | None = None
    model: str = Field(min_length=1)
    """The model to transcribe with. Required, and the only place it is decided:
    a provider row carries no model any more, so there is nothing to fall back
    to and no way for a session to run on a model nobody picked. The Start
    button is disabled until the picker has one, so the UI cannot send a
    request this rejects."""
    fallback_model: str | None = None
    """Same, for the fallback provider - it has its own model list. Optional
    only because the fallback itself is; required as soon as one is named (see
    the validator below)."""
    diarization: DiarizationConfig = Field(default_factory=DiarizationConfig)
    use_glossary: bool = True
    """Feed the campaign glossary to the STT provider (as keyterms or a prompt).
    Defaults to on, which is what capture always did before the option existed;
    turn it off to hear what the provider makes of the audio unbiased."""

    @model_validator(mode="after")
    def _fallback_model_required_with_a_fallback(self) -> Self:
        """A named fallback provider needs its own model.

        The fallback has its own model list - it is a different vendor, often
        with a different transport - so the primary's choice cannot carry over.
        Left blank it would be a fallback that fails the moment it is needed,
        which is the worst time to find out.
        """
        if self.fallback_provider and not (self.fallback_model or "").strip():
            raise ValueError("fallback_model is required when a fallback_provider is set")
        return self


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
    recap_prompt: str = ""
    """Recap system prompt; blank means the built-in default (served filled in).

    A campaign's own ``recap_prompt`` wins over this one: the global default is
    how every table's recaps read, and a campaign that wants something else
    (another language, a voice, a length) says so on itself."""
    campaign_id: str = ""
    """The campaign the capture card starts a session in. Remembered here
    rather than in the browser, because it is the same answer at every screen
    that starts a session and it survives a reinstalled tablet."""
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
    model: str = Field(min_length=1)
    """Required: nothing else decides it. The provider row carries no model, and
    the summarizer is handed exactly what is recorded as the summary's model."""
    reasoning_effort: str | None = None
    """How hard a reasoning model should think. Only meaningful for a model
    that advertises support (ModelInfo.supports_reasoning); ignored otherwise,
    and dropped automatically if the endpoint rejects it."""
    version: str | None = None
    """Transcript version to summarize ("original" or a transcribe job id).
    None means the original, so a client that predates the field keeps working;
    an id no version answers to is a 404 rather than a quiet fallback, because
    summarizing the wrong transcript costs money and reads as if it worked."""


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
    """The model to re-transcribe with. Required for "transcribe" (see the
    validator below) and ignored for "diarize", which runs the diarizer rather
    than an STT provider - the same split ``provider_id`` has. It is what the
    job row records, so a stored version says which model actually produced it
    instead of naming whatever the provider row happened to hold."""
    target: str = ORIGINAL_VERSION
    """Transcript version a "diarize" job relabels ("original" or a transcribe job id)."""
    use_glossary: bool = True
    """Feed the campaign glossary to the STT provider, "transcribe" only.
    Defaults to on, matching what re-processing always did before the option
    existed; off produces a version comparable against a glossary-biased one."""

    @model_validator(mode="after")
    def _transcribe_needs_a_model(self) -> Self:
        """Re-transcription must name a model; a diarize job has no use for one."""
        if self.operation == "transcribe" and not (self.model or "").strip():
            raise ValueError('model is required for a "transcribe" job')
        return self


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


class VersionLogs(BaseModel):
    """One transcript version's stored log file."""

    session_id: str
    version: str
    logs: str


class CampaignWrite(BaseModel):
    """Create or rename a campaign.

    The name is the campaign, so a blank one is refused rather than stored: a
    row that renders as an empty cell in the picker is unpickable and
    indistinguishable from "no campaign", which is the one thing the list must
    be able to say.
    """

    name: str = Field(min_length=1)
    notes: str = ""
    recap_prompt: str = ""

    @field_validator("name")
    @classmethod
    def _a_name_is_not_whitespace(cls, value: str) -> str:
        name = value.strip()
        if not name:
            raise ValueError("a campaign needs a name")
        return name


class CampaignAssignment(BaseModel):
    """Which campaign a session belongs to; null takes it out of one."""

    campaign_id: str | None = None


class GlossaryAdd(BaseModel):
    """Names to append to a campaign's glossary, deduplicated on the way in."""

    terms: list[str] = Field(default_factory=list[str])


class GenerateRequest(BaseModel):
    """Run one of the generated texts: a recap, an extraction, a previously-on.

    Deliberately the same shape as :class:`SummarizeRequest`, minus the version
    for the campaign-level one: the three session actions are one dialog in the
    browser with a different verb on the button, and three request models that
    drift apart would make that dialog three dialogs again.
    """

    provider_id: str
    model: str = Field(min_length=1)
    reasoning_effort: str | None = None
    version: str | None = None
    """Transcript version to read ("original" or a transcribe job id); None
    means the original. Ignored by the campaign-level generation, which reads
    recaps rather than a transcript."""


class PreviouslyOnRequest(GenerateRequest):
    """Write the campaign's "previously on" from its last few sessions."""

    sessions: int = Field(default=3, ge=1, le=20)
    """How many of the campaign's most recent sessions to read. Three is the
    default because that is roughly what a table still remembers; the bound is
    there because the whole campaign in one prompt is a different (and much
    more expensive) request than the one this route answers."""


class OkResponse(BaseModel):
    """Generic success acknowledgement."""

    ok: bool = True


class LivenessResponse(BaseModel):
    """The unauthenticated liveness answer: this process is up, and no more.

    Its own model rather than a reuse of ``OkResponse`` because the two are
    read by different callers and mean different things: ``ok`` acknowledges a
    write to whoever made it, ``status`` is what an uptime check and the
    installer's start-up poll look at from outside. Deliberately carries
    nothing about the deployment; see ``/api/system/livez``.
    """

    status: Literal["ok"] = "ok"


def _absolute_http_url(value: str | None, label: str) -> str:
    """Return ``value`` trimmed if it is an absolute http(s) URL, else raise.

    An alert channel is the one piece of configuration whose failure is
    indistinguishable from nothing having gone wrong. A mistyped URL used to
    save, sit in the table looking exactly like a working channel, and swallow
    every alert it was set up to deliver. Nothing on the page grades a channel
    the way the provider table grades a provider, so the write path is where a
    URL that cannot possibly work has to be caught.

    "Absolute http(s)" is the whole test, on purpose: a scheme we can POST to
    and a host to POST it at. Anything stricter would reject configurations
    that work - a receiver that happens to be down at save time, a LAN name
    this box resolves and nothing else does, a port-only host.
    """
    text = (value or "").strip()
    parts = urlsplit(text)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ValueError(f"{label} must be an absolute http:// or https:// URL")
    return text


class AlertChannelWrite(BaseModel):
    """Create/update payload for one alert channel (token is write-only)."""

    type: AlertChannelType
    enabled: bool = True
    min_level: AlertLevel = AlertLevel.WARNING
    server: str = "https://ntfy.sh"
    topic: str | None = None
    chat_id: str | None = None
    url: str | None = Field(default=None, validate_default=True)
    """The webhook target. ``validate_default`` because a field validator is
    skipped for a field the request omits, and an omitted ``url`` is exactly the
    webhook-with-nowhere-to-post the validator below exists to refuse."""
    token: str | None = Field(
        default=None, description="Write-only token (Telegram bot / ntfy auth)."
    )

    # Field validators rather than the `model_validator(mode="after")` used
    # elsewhere in this file, for one reason: FastAPI echoes the rejected input
    # back in the 422 body, and a model-level error's input is the *whole*
    # payload - `token` included. A field-level error carries only the offending
    # string, so a bad URL cannot drag a write-only credential into the response
    # or into whatever logs it. `type` is declared first, so it is already
    # validated and visible in `info.data` by the time these run.

    @field_validator("server")
    @classmethod
    def _ntfy_server_must_be_a_url(cls, value: str, info: ValidationInfo) -> str:
        """An ntfy channel's server has to be somewhere we can actually POST."""
        if info.data.get("type") != "ntfy":
            return value  # carried but unused by the other channel types
        return _absolute_http_url(value, "ntfy server")

    @field_validator("url")
    @classmethod
    def _webhook_url_must_be_a_url(cls, value: str | None, info: ValidationInfo) -> str | None:
        """A webhook is nothing but its URL, so a blank one is rejected too.

        This also fails an update of a channel saved before the check existed,
        which is deliberate: the row is broken either way, and Edit or Delete
        both resolve it. Silently keeping it toggleable would preserve exactly
        the "looks configured, delivers nothing" state this exists to end.
        """
        if info.data.get("type") != "webhook":
            return value
        return _absolute_http_url(value, "webhook url")


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
    """Delivery outcome of a single channel test, and why it failed.

    ``detail`` is the only diagnosis an alert channel ever offers: nothing
    probes one periodically and the table carries no health column, so "Test
    failed" on its own leaves an operator guessing between a typo, a closed
    port and a rejected token. It holds the transport's own words or the status
    plus the vendor's sentence, bounded and with the channel's credential
    scrubbed out (see ``loreline.monitoring.alerts._scrub``). None on success:
    there is nothing to explain.
    """

    ok: bool
    detail: str | None = None


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
    """Current deployed revision: the commit, and the name git gives it."""

    # The full SHA, and nothing else - this is what UpdateResult's
    # previous_commit/new_commit already mean, they come from the same method,
    # and RollbackRequest above takes one back. An identifier, not a label.
    commit: str | None = None
    # `git describe --tags --always`: last reachable tag, distance, short SHA -
    # or a bare short SHA where no tag is reachable. The half a person reads.
    # Null where it is unknown, which is a Docker image built without the
    # revision baked in; the UI shows a dash rather than guessing.
    described: str | None = None
    # The commit HEAD was on before it last moved, from git's reflog, and the
    # name git gives that one. What the Roll back button offers, and what it
    # sends back as RollbackRequest.commit. Both null where there is nothing to
    # go back to: a checkout that has never been updated, or a container.
    previous_commit: str | None = None
    previous_described: str | None = None
    # Why this deployment cannot roll back at all, in a sentence the page shows
    # beside a disabled button; null where it can. Distinct from
    # previous_commit being null: a source checkout with nowhere to go back to
    # hides the button, a Docker deployment says why it is greyed out.
    rollback_unavailable: str | None = None
