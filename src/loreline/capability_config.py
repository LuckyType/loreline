"""Schema and loader for capabilities.yaml, the compatible-models source of truth.

Why a data file at all: every "can this provider do that" fact used to live in
hand-written Python tables (loreline.capabilities) *and* be mirrored by hand a
second time in TypeScript, because the pickers need the same answers in the
browser. Two copies of a table that changes whenever a vendor ships a model is
one copy too many, and they had already drifted. capabilities.yaml is now the
only place these facts are written down; Python reads it here, and the frontend
reads the same data over one endpoint instead of re-declaring it.

What this file is NOT: a gate on what exists. The live provider APIs stay
authoritative for which models are actually reachable today. This config is the
*annotation* layer - what a model can do, and which of them we vouch for - plus
a fallback catalogue for the providers that publish no machine-readable list.
When it goes stale the app must keep working and say so, never refuse. That is
why every consumer treats a missing entry as "unknown, allow it" rather than
"unsupported, hide it", and why the staleness checks fail soft.

Three levels, matching the three questions the UI asks:

* provider - what is this vendor for, can it drive a live capture, where does a
  key come from, and how each of its surfaces is reached: the URL and the auth
  scheme per interaction (and per transport for transcription), plus the
  catalogue to call at runtime, if it publishes one.
* model - which interactions this specific model serves.
* capability block - the per-interaction parameter surface: whether glossary
  biasing exists and under which request field, whether reasoning effort may be
  offered, which video durations are legal. This is what lets the UI hide a
  control instead of showing one that silently does nothing.
"""

from __future__ import annotations

import fnmatch
from collections.abc import Iterable
from enum import StrEnum
from pathlib import Path
from typing import Literal, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from loreline.models import Interaction, ProviderKind

CONFIG_PATH = Path(__file__).with_name("capabilities.yaml")

# The transcription toggles a conflict group may name, in the order a request
# gives them up. A conflict says a model refuses a combination; it does not say
# which half to keep, and somebody has to decide, once, for every provider,
# rather than per connector.
#
# The glossary comes first because it is the only one of the three the GM
# switches on for a specific job, and because on a tabletop transcript the
# invented character and place names are the payload: a run that spells
# "Drakonia" right and labels the speaker "Speaker 1" is worth more than the
# reverse. Inline diarization comes next, being visible in the transcript, and
# word timestamps last, being an internal alignment aid (see
# loreline.diarization.merge, which falls back to labelling a whole utterance
# when they are absent).
#
# Honouring a toggle in name only is the failure this ordering exists to avoid:
# the UI offered "Use glossary", the connector sent the terms alongside a
# feature the vendor refuses, and every utterance died with a 400.
CONFLICT_PRECEDENCE = ("glossary", "inline_diarization", "word_timestamps")

# The two ways audio reaches a model. A string literal rather than an enum
# because it is spelled in the yaml as a key (``surfaces.transcribe.batch``)
# and as a value (``prefer: batch``), and both read best as the bare word.
Transport = Literal["realtime", "batch"]

# The placeholder an operator-supplied base URL is spliced into, for a surface
# that lives on a server only the provider row can name.
BASE_URL_PLACEHOLDER = "{base_url}"
_SOCKET_SCHEMES = ("ws://", "wss://")
_HTTP_SCHEMES = ("http://", "https://")


class _Strict(BaseModel):
    """Reject unknown keys everywhere.

    A typo in a capability name would otherwise read as "capability absent" and
    quietly hide a control, which is the exact failure this file exists to stop.
    Better to fail loudly at startup and in CI.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)


class GlossarySupport(_Strict):
    """Whether keyword biasing exists, and what the request field is called.

    Every vendor spells this differently (Deepgram ``keyterm``, AssemblyAI
    ``keyterms_prompt``, OpenAI-compatible ``prompt``, Gemini a vocabulary
    list), and at least one - OpenRouter's transcription API - has no such
    field at all. Recording the field name here is what tells the connector how
    to send the glossary, and ``supported: false`` is what tells the UI to
    disable the "Use glossary" checkbox rather than leave a control that does
    nothing.
    """

    supported: bool = False
    field: str | None = None
    # Vendors cap this three different ways and we cannot flatten them: a term
    # count (Deepgram "keywords", 100), a token budget across the whole list
    # (Deepgram "keyterm", 500 tokens; whisper-1's prompt, 224), or a per-term
    # character limit (AssemblyAI streaming, 50). All may be null, meaning the
    # vendor documents no ceiling.
    max_terms: int | None = None
    max_tokens: int | None = None
    max_term_chars: int | None = None
    # AssemblyAI caps the same model differently depending on which API it is
    # called through - universal-3-5-pro takes 1000 terms async but 100 over
    # streaming - so the streaming ceiling is an override rather than a second
    # model entry. Null means the transport makes no difference.
    max_terms_realtime: int | None = None

    @model_validator(mode="after")
    def _field_required_when_supported(self) -> Self:
        if self.supported and not self.field:
            raise ValueError("glossary.supported requires a request field name")
        limits = (self.max_terms, self.max_tokens, self.max_term_chars, self.max_terms_realtime)
        if not self.supported and any(limit is not None for limit in limits):
            raise ValueError("glossary limits set on a model that does not support one")
        return self

    def max_terms_for(self, *, realtime: bool) -> int | None:
        """The term ceiling on one transport, honouring a streaming override."""
        if realtime and self.max_terms_realtime is not None:
            return self.max_terms_realtime
        return self.max_terms


class TranscribeCapabilities(_Strict):
    """Per-model transcription surface.

    ``realtime`` and ``batch`` are transports, not a single either/or: OpenAI
    serves both and ``prefer`` then says which connector runs. ``realtime`` is
    also what the Realtime badge reports and what gates a model for live
    capture.
    """

    realtime: bool = False
    batch: bool = True
    # Which connector runs when this model serves both transports. Required
    # exactly then, and meaningless otherwise, because a single-transport model
    # has nothing to choose between.
    #
    # This used to be derived instead: "do all non-hidden models of this kind
    # stream". That made a model's routing a property of its siblings, so
    # unhiding a batch-only model moved every dual-transport model of the same
    # vendor onto the batch connector - and the yaml told maintainers that
    # unhiding was the whole release step, which it demonstrably was not. The
    # preference is a fact about the model, so it is written on the model, and
    # the validator below means a new dual-transport entry cannot be added
    # without stating it.
    prefer: Transport | None = None
    inline_diarization: bool = False
    glossary: GlossarySupport = GlossarySupport()
    word_timestamps: bool = False
    languages: Literal["single", "multi", "codeswitch"] = "multi"
    # Set only when the model is restricted to a known list, as English-only
    # whisper checkpoints are. Empty means "whatever the vendor supports".
    language_codes: list[str] = Field(default_factory=list[str])
    # Features this model cannot combine, as groups that may not be enabled
    # together. Gemini is the reason: it rejects a request outright when
    # custom_vocabulary is sent alongside diarization or word timestamps, so
    # "supported" is true for each individually and false for the pair. A flat
    # set of booleans cannot say that, and the picker needs to know in order to
    # grey out the second toggle once the first is on.
    conflicts: list[list[str]] = Field(default_factory=list[list[str]])

    # The toggles a conflict group may name. Anything else is a typo, and a
    # typo here would silently stop guarding a combination that errors. Derived
    # from the precedence order so the two cannot drift: a feature that can
    # conflict is a feature the resolver has to know how to rank.
    _CONFLICTABLE = frozenset(CONFLICT_PRECEDENCE)
    # A conflict is a statement about a pair, so a group of one says nothing.
    _MIN_CONFLICT_GROUP = 2

    @model_validator(mode="after")
    def _at_least_one_transport(self) -> Self:
        if not (self.realtime or self.batch):
            raise ValueError("a transcription model must support realtime or batch")
        return self

    @model_validator(mode="after")
    def _dual_transport_states_its_preference(self) -> Self:
        """Both transports means the file has to say which one runs.

        Failing here is the point: routing that nobody wrote down gets derived
        from something else, and the something else was the sibling list.
        """
        both = self.realtime and self.batch
        if both and self.prefer is None:
            raise ValueError(
                "a model serving both transports must set prefer: realtime or prefer: batch"
            )
        if not both and self.prefer is not None:
            raise ValueError(
                "prefer is only meaningful when both realtime and batch are true; "
                "a single-transport model already has its answer"
            )
        return self

    @model_validator(mode="after")
    def _conflicts_name_real_features(self) -> Self:
        for group in self.conflicts:
            if len(group) < self._MIN_CONFLICT_GROUP:
                raise ValueError("a conflict group needs at least two features")
            unknown = sorted(set(group) - self._CONFLICTABLE)
            if unknown:
                known = ", ".join(sorted(self._CONFLICTABLE))
                raise ValueError(
                    f"unknown feature(s) in conflicts: {', '.join(unknown)} (known: {known})"
                )
        return self

    @property
    def prefers_realtime(self) -> bool:
        """Whether this model's chosen connector is the streaming one.

        The single transport for a model that serves one, the declared
        preference for a model that serves both. This is what the router asks;
        ``realtime`` alone answers "can it stream", which is the badge's
        question and not the connector's.
        """
        if self.realtime and self.batch:
            return self.prefer == "realtime"
        return self.realtime

    def conflicts_with(self, feature: str) -> frozenset[str]:
        """Features that cannot be enabled alongside ``feature``.

        The UI greys these out once ``feature`` is on, rather than letting the
        request reach a vendor that rejects the combination.
        """
        blocked: set[str] = set()
        for group in self.conflicts:
            if feature in group:
                blocked.update(group)
        return frozenset(blocked - {feature})

    def resolve_conflicts(self, requested: Iterable[str]) -> frozenset[str]:
        """The subset of ``requested`` this model will accept in one request.

        Answers the question the connectors actually have, which
        ``conflicts_with`` alone cannot: given everything a request would turn
        on, what may still be sent. Features are taken in
        ``CONFLICT_PRECEDENCE`` order and one is kept unless something already
        kept conflicts with it, so the answer does not depend on the order the
        caller happened to build its set in.

        A model that declares no conflicts gets everything it asked for back,
        which is every model but Gemini's batch transcriber today. That is the
        point of resolving through the config rather than in a connector: the
        rule is enforced wherever it is declared, and nowhere else.
        """
        wanted = set(requested)
        kept: set[str] = set()
        for feature in CONFLICT_PRECEDENCE:
            if feature in wanted and not (self.conflicts_with(feature) & kept):
                kept.add(feature)
        # Anything the precedence list does not name cannot appear in a
        # conflict group (the validator above sees to that), so it passes
        # through untouched rather than being silently dropped.
        return frozenset(kept | (wanted - self._CONFLICTABLE))


class ReasoningSupport(_Strict):
    """Whether a reasoning effort selector may be offered, and with which values.

    The accepted values differ per vendor and per model generation, so they are
    listed rather than assumed. An empty list with ``supported: true`` is a
    contradiction and rejected.
    """

    supported: bool = False
    # Some models refuse to have reasoning turned off: OpenRouter marks these
    # ``mandatory`` and they reject effort "none" outright. The picker must
    # then omit "none" rather than offer a value that fails the request.
    mandatory: bool = False
    # Empty while ``supported`` is true is meaningful, not an oversight: the
    # model reasons but exposes no discrete effort levels (always-on, or a
    # token budget instead). The UI shows the effort dropdown only when this
    # list has entries, so such a model gets reasoning without a dead control.
    efforts: list[str] = Field(default_factory=list[str])

    @model_validator(mode="after")
    def _efforts_imply_support(self) -> Self:
        if self.efforts and not self.supported:
            raise ValueError("reasoning.efforts listed but reasoning.supported is false")
        if self.mandatory and not self.supported:
            raise ValueError("reasoning.mandatory set but reasoning.supported is false")
        if self.mandatory and "none" in self.efforts:
            raise ValueError('reasoning.mandatory contradicts an effort value of "none"')
        return self

    def selectable_efforts(self) -> list[str]:
        """Effort values the picker may offer.

        Drops "none" for a model that requires reasoning, so the dropdown
        cannot produce a request the vendor rejects.
        """
        if self.mandatory:
            return [e for e in self.efforts if e != "none"]
        return list(self.efforts)


class LlmCapabilities(_Strict):
    """Per-model summarization surface.

    ``temperature`` is here because it is not cosmetic: several reasoning
    models reject the parameter outright, and sending it fails the request. The
    summarizer reads this instead of catching the error after the fact.
    ``system_prompt`` likewise decides whether our instructions go in a system
    message or get folded into the user turn.
    """

    reasoning: ReasoningSupport = ReasoningSupport()
    context_length: int | None = None
    max_output_tokens: int | None = None
    system_prompt: bool = True
    temperature: bool = True


class VideoCapabilities(_Strict):
    """Per-model video generation surface.

    These lists populate the generate-video modal directly. Offering a duration
    the model rejects wastes a paid job and a minute of waiting, so the modal
    only ever shows what is listed here.
    """

    durations: list[int] = Field(default_factory=list[int])
    resolutions: list[str] = Field(default_factory=list[str])
    aspect_ratios: list[str] = Field(default_factory=list[str])
    # None where the vendor publishes no answer, which is not the same as
    # "no audio": the modal says nothing rather than promising silence.
    audio: bool | None = None
    image_input: bool = False
    # Vendors state the prompt ceiling in different units - Veo documents
    # tokens, others characters - so both exist and either may be null. The
    # modal warns on whichever is set.
    prompt_max_chars: int | None = None
    prompt_max_tokens: int | None = None


class ModelSpec(_Strict):
    """One curated model, with a capability block per interaction it serves."""

    id: str
    label: str | None = None
    interactions: list[Interaction]
    # Present but not offered. The model stays fully described and a config
    # naming it explicitly still routes correctly, but no picker lists it and
    # no kind-level capability is derived from it. This is the gate for a
    # connector that is written but unverified against the real API: flipping
    # one flag here is the whole release step.
    hidden: bool = False
    # The model this file vouches for when a connector must name one and
    # nobody chose: the health probe in POST /providers/{id}/test is the only
    # caller left, since every action route now requires a model. Marked here,
    # per model, rather than derived from list order, because "first entry
    # wins" is silently wrong the moment someone reorders the list, and it is
    # scoped by interaction because one provider row serves several: an
    # OpenRouter entry that transcribes, summarizes and generates video has no
    # single correct model. A model marked here defaults for every interaction
    # it declares, which is why the validator below refuses two defaults for
    # the same one. It replaces the _DEFAULT_MODEL constants the connectors
    # used to carry, all four of which had gone stale (whisper-1 deprecated,
    # nova-2 on the legacy keyword field, gpt-4o-mini absent from this file
    # entirely) - which is precisely the hand-maintained drift this file
    # exists to end, so the default now lives next to the deprecation date
    # that invalidates it.
    default: bool = False
    # Vendor-announced sunset, ISO date. A property of the model as a whole,
    # not of one capability: OpenAI is retiring whole transcription models on
    # stated dates. Shown as a warning beside the model rather than hiding it,
    # because a GM mid-campaign should not lose their model the day we notice
    # the announcement.
    deprecated: str | None = None
    transcribe: TranscribeCapabilities | None = None
    llm: LlmCapabilities | None = None
    video: VideoCapabilities | None = None

    @model_validator(mode="after")
    def _blocks_match_interactions(self) -> Self:
        """Every declared interaction needs its block, and vice versa.

        Catches the two ways an entry goes wrong by hand: claiming an
        interaction with no parameters recorded for it, and leaving a stale
        block behind after removing an interaction.
        """
        pairs = (
            (Interaction.TRANSCRIBE, "transcribe", self.transcribe),
            (Interaction.SUMMARIZE, "llm", self.llm),
            (Interaction.VIDEO, "video", self.video),
        )
        for interaction, name, block in pairs:
            declared = interaction in self.interactions
            if declared and block is None:
                raise ValueError(
                    f"model {self.id!r} declares {interaction.value} but has no {name} block"
                )
            if block is not None and not declared:
                raise ValueError(
                    f"model {self.id!r} has a {name} block but does not declare {interaction.value}"
                )
        if not self.interactions:
            raise ValueError(f"model {self.id!r} declares no interactions")
        return self

    @model_validator(mode="after")
    def _default_is_one_we_would_offer(self) -> Self:
        """A default may be neither hidden nor retiring.

        Both would be a default nobody can see is wrong: a hidden model is one
        whose connector is unverified, and a dated model is one the vendor has
        announced it will stop serving. Failing here is the point - adding a
        sunset date to the marked model is exactly the moment someone has to
        choose the next one, which is what the constants this replaces never
        forced anyone to do.
        """
        if not self.default:
            return self
        if self.hidden:
            raise ValueError(f"model {self.id!r} is hidden and cannot be a default")
        if self.deprecated:
            raise ValueError(
                f"model {self.id!r} retires on {self.deprecated} and cannot be a default; "
                "mark a current model instead"
            )
        return self

    def capabilities_for(
        self, interaction: Interaction
    ) -> TranscribeCapabilities | LlmCapabilities | VideoCapabilities | None:
        """The capability block for one interaction, or None if unsupported."""
        if interaction is Interaction.TRANSCRIBE:
            return self.transcribe
        if interaction is Interaction.SUMMARIZE:
            return self.llm
        if interaction is Interaction.VIDEO:
            return self.video
        return None


class ModelPattern(_Strict):
    """Capability annotation for models matched by glob rather than exact id.

    Exists for the self-hosted kind, whose catalogue is whatever the operator
    installed. We cannot enumerate ``Systran/faster-whisper-large-v3`` and every
    sibling, but ``*whisper*`` tells us it does word timestamps, takes a
    ``prompt``, and cannot diarize - which is enough to render the right
    controls.
    """

    match: str
    interactions: list[Interaction] = Field(default_factory=list[Interaction])
    transcribe: TranscribeCapabilities | None = None
    llm: LlmCapabilities | None = None
    video: VideoCapabilities | None = None

    def matches(self, model_id: str) -> bool:
        return fnmatch.fnmatch(model_id.lower(), self.match.lower())


class AuthScheme(StrEnum):
    """How a surface wants the credential spelled on the wire.

    A data value, so the yaml can say it and one place can render it. Every
    vendor here spells it differently: Deepgram wants ``Authorization: Token``,
    AssemblyAI the bare key with no scheme at all, Google its own header on the
    native surface and a query parameter on the Live socket, and everything
    OpenAI-compatible a Bearer token. Sending the wrong spelling is a 401 that
    reads exactly like a bad key, which is why the spelling travels with the
    surface rather than being remembered per connector.
    """

    BEARER = "bearer"
    TOKEN_HEADER = "token_header"
    RAW_HEADER = "raw_header"
    GOOG_HEADER = "goog_header"
    QUERY_KEY = "query_key"
    NONE = "none"

    def headers(self, api_key: str | None) -> dict[str, str]:
        """The request headers that carry ``api_key``, or none without one."""
        if not api_key:
            return {}
        if self is AuthScheme.BEARER:
            return {"Authorization": f"Bearer {api_key}"}
        if self is AuthScheme.TOKEN_HEADER:
            return {"Authorization": f"Token {api_key}"}
        if self is AuthScheme.RAW_HEADER:
            return {"Authorization": api_key}
        if self is AuthScheme.GOOG_HEADER:
            return {"x-goog-api-key": api_key}
        return {}

    def query(self, api_key: str | None) -> dict[str, str]:
        """The query parameters that carry ``api_key``: only the Live socket's."""
        if self is AuthScheme.QUERY_KEY and api_key:
            return {"key": api_key}
        return {}


class HealthProbe(_Strict):
    """The cheap question a health probe asks of one surface, as data.

    For an HTTP surface, ``path`` (and ``params``): a read that exercises the
    credential and costs nothing. Declared only where the obvious ``/models``
    is the wrong question: it is public on OpenRouter and Deepgram, so grading
    it would call any key healthy, and AssemblyAI has no such route at all. A
    surface that declares none is asked ``/models``.

    For a socket surface, ``frame``: the first message to send once the
    socket is open, for a vendor that waits for the client to speak
    (Deepgram, whose ``CloseStream`` makes it answer and hang up at once). A
    vendor that greets first (AssemblyAI) declares none, and the probe reads
    the greeting. Static data only: a frame that needs a model or a session's
    settings is a session question, not a health one, and the handshake has
    already tested the credential by the time it would be sent.
    """

    path: str | None = None
    params: dict[str, str] = Field(default_factory=dict[str, str])
    frame: dict[str, object] | None = None

    @model_validator(mode="after")
    def _asks_something(self) -> Self:
        if self.path is None and self.frame is None:
            raise ValueError("a health block must name a path or a frame")
        if self.path is not None and not self.path.startswith("/"):
            raise ValueError(f"health path {self.path!r} must be relative to the surface url")
        if self.params and self.path is None:
            raise ValueError("health params need a path to go with")
        return self


class Surface(_Strict):
    """How to reach one vendor for one interaction over one transport.

    ``url`` is the address a connector builds on: a base for an HTTP surface
    (the connector appends its paths), the socket URL for a streaming one, or
    the whole address for a catalogue. It is absolute, except on a surface an
    operator has to point somewhere themselves: then it is null, or a template
    around ``{base_url}``, and ``overridable`` must say so.

    ``overridable`` is whether a provider row's stored ``base_url`` may replace
    this address. The replacement honours the transport: a WebSocket URL is
    applied to a socket surface and dropped by an HTTP one (a row whose
    streaming connector shipped first has always carried the socket address,
    and handing that to an HTTP client fails every request), and the reverse.
    """

    url: str | None = None
    auth: AuthScheme = AuthScheme.BEARER
    overridable: bool = False
    # Headers every request to this surface carries besides the credential:
    # OpenRouter's leaderboard attribution is the one case.
    headers: dict[str, str] = Field(default_factory=dict[str, str])
    # What the health probe asks of this surface (see HealthProbe). A bare
    # string is the HTTP path, which is the common case in the yaml.
    health: HealthProbe | None = None
    # A catalogue that answers without a credential, so a CI run with no
    # secrets can still read it. A key is still sent when one is around.
    public: bool = False
    # Whether a model picker offers this catalogue live. False marks a list
    # that is read to check the curated models (the staleness check) but never
    # to replace them in a picker: the vendor's list is not fit to choose from
    # as published, and the reason is written beside the surface in the yaml.
    picker: bool = True

    @model_validator(mode="after")
    def _absolute_unless_operator_supplied(self) -> Self:
        if self.url is None or BASE_URL_PLACEHOLDER in self.url:
            if not self.overridable:
                raise ValueError(
                    "a surface without a fixed url must be overridable: "
                    "only the provider row can say where it is"
                )
        elif not self.url.lower().startswith(_SOCKET_SCHEMES + _HTTP_SCHEMES):
            raise ValueError(f"surface url {self.url!r} must be absolute (http(s) or ws(s))")
        return self

    @field_validator("health", mode="before")
    @classmethod
    def _path_shorthand(cls, value: object) -> object:
        return {"path": value} if isinstance(value, str) else value

    @model_validator(mode="after")
    def _health_matches_the_transport(self) -> Self:
        """A frame is a socket's question and a path an HTTP surface's."""
        if self.health is None:
            return self
        if self.socket and self.health.path is not None:
            raise ValueError("a socket surface is probed with a frame, not a path")
        if not self.socket and self.health.frame is not None:
            raise ValueError("an HTTP surface is probed at a path, not with a frame")
        return self

    @property
    def socket(self) -> bool:
        """Whether this surface is a WebSocket, as opposed to HTTP."""
        return bool(self.url) and self.url.lower().startswith(_SOCKET_SCHEMES)

    def resolve(self, base_url: str | None) -> str | None:
        """The effective address once a provider row's ``base_url`` had its say.

        None means there is nothing to call: the surface has no address of its
        own and the row supplied none, or supplied one for the other transport.
        """
        override = base_url if base_url and self.overridable else None
        if override and override.lower().startswith(_SOCKET_SCHEMES) == self.socket:
            if self.url and BASE_URL_PLACEHOLDER in self.url:
                return self.url.replace(BASE_URL_PLACEHOLDER, override.rstrip("/"))
            return override
        if self.url is None or BASE_URL_PLACEHOLDER in self.url:
            return None
        return self.url

    def request_headers(self, api_key: str | None) -> dict[str, str]:
        """The credential in this surface's spelling, plus its fixed headers."""
        return {**self.auth.headers(api_key), **self.headers}


class TranscribeSurfaces(_Strict):
    """A vendor's transcription surfaces, one per transport it serves."""

    realtime: Surface | None = None
    batch: Surface | None = None

    @model_validator(mode="after")
    def _at_least_one_transport(self) -> Self:
        if self.realtime is None and self.batch is None:
            raise ValueError("transcribe surfaces must name at least one transport")
        return self

    def for_transport(self, transport: Transport) -> Surface | None:
        return self.realtime if transport == "realtime" else self.batch


class Surfaces(_Strict):
    """Every address a vendor is reached at, keyed the way requests are routed.

    Transcription is keyed by transport because the two differ in host,
    scheme and sometimes auth for one vendor; the other interactions have one
    surface each. ``catalog`` is where the runtime picker and the staleness
    check read the vendor's own model list: one surface when it serves every
    interaction, a mapping when the vendor splits it (OpenRouter's three
    disjoint lists), absent when this file is the catalogue.
    """

    transcribe: TranscribeSurfaces | None = None
    summarize: Surface | None = None
    video: Surface | None = None
    catalog: Surface | dict[Interaction, Surface] | None = None

    def for_interaction(self, interaction: Interaction) -> Surface | TranscribeSurfaces | None:
        if interaction is Interaction.TRANSCRIBE:
            return self.transcribe
        if interaction is Interaction.SUMMARIZE:
            return self.summarize
        return self.video

    def catalog_for(self, interaction: Interaction) -> Surface | None:
        if isinstance(self.catalog, Surface):
            return self.catalog
        return self.catalog.get(interaction) if self.catalog else None


class ProviderSpec(_Strict):
    """One vendor. Exactly one entry per vendor, capabilities as flags."""

    label: str
    hosting: Literal["cloud", "selfhosted"] = "cloud"
    # ``optional`` covers a self-hosted server that may or may not check a key.
    auth: Literal["api_key", "optional", "none"] = "api_key"
    key_url: str | None = None
    # Environment variables a CI run would find this vendor's key in, for the
    # staleness check, which runs without stored providers.
    key_env: list[str] = Field(default_factory=list[str])
    # Where this vendor is reached, per interaction and transport. This is the
    # only place an endpoint or an auth scheme is written down; the connectors
    # read it through loreline.capabilities.surface_for.
    surfaces: Surfaces
    # False for a provider we allow for stored-audio work but never for a live
    # session. A policy switch, not a technical one: see the OpenRouter note in
    # the yaml.
    live_capture: bool = True
    interactions: list[Interaction]
    models: list[ModelSpec] = Field(default_factory=list[ModelSpec])
    model_patterns: list[ModelPattern] = Field(default_factory=list[ModelPattern])

    @model_validator(mode="after")
    def _surfaces_match_interactions(self) -> Self:
        """Every declared interaction is reachable, and nothing else is.

        A declared interaction with no surface would fail at request time with
        a connector that has nowhere to go; a surface for an interaction the
        provider no longer declares is the stale block left behind by an edit.
        """
        for interaction in Interaction:
            declared = interaction in self.interactions
            present = self.surfaces.for_interaction(interaction) is not None
            if declared and not present:
                raise ValueError(
                    f"provider {self.label!r} declares {interaction.value} "
                    f"but has no surfaces.{interaction.value} entry"
                )
            if present and not declared:
                raise ValueError(
                    f"provider {self.label!r} has a surfaces.{interaction.value} entry "
                    f"but does not declare {interaction.value}"
                )
        catalog = self.surfaces.catalog
        if isinstance(catalog, dict):
            extra = sorted(i.value for i in catalog if i not in self.interactions)
            if extra:
                raise ValueError(
                    f"provider {self.label!r} lists a catalog for {', '.join(extra)}, "
                    "which it does not offer"
                )
        return self

    @model_validator(mode="after")
    def _every_declared_transport_has_a_surface(self) -> Self:
        """A model may not claim a transport its vendor has no address for.

        Hidden models count too: a config naming one explicitly still routes,
        and it must route somewhere real.
        """
        surfaces = self.surfaces.transcribe
        entries: list[ModelSpec | ModelPattern] = [*self.models, *self.model_patterns]
        for entry in entries:
            caps = entry.transcribe
            if caps is None:
                continue
            name = entry.id if isinstance(entry, ModelSpec) else entry.match
            served_by: tuple[tuple[Transport, bool], ...] = (
                ("realtime", caps.realtime),
                ("batch", caps.batch),
            )
            for transport, served in served_by:
                if served and (surfaces is None or surfaces.for_transport(transport) is None):
                    raise ValueError(
                        f"model {name!r} transcribes over {transport} but provider "
                        f"{self.label!r} declares no surfaces.transcribe.{transport}"
                    )
        return self

    @model_validator(mode="after")
    def _models_stay_within_provider_interactions(self) -> Self:
        declared = set(self.interactions)
        if not declared:
            raise ValueError(f"provider {self.label!r} declares no interactions")
        for model in self.models:
            extra = set(model.interactions) - declared
            if extra:
                names = ", ".join(sorted(i.value for i in extra))
                raise ValueError(
                    f"model {model.id!r} declares {names}, "
                    f"which provider {self.label!r} does not offer"
                )
        if self.auth == "api_key" and self.hosting == "cloud" and not self.key_url:
            raise ValueError(f"cloud provider {self.label!r} needs a key_url for the setup wizard")
        return self

    @model_validator(mode="after")
    def _one_default_per_offered_interaction(self) -> Self:
        """Exactly one default wherever this provider offers models at all.

        Two would make the answer depend on list order, which is the failure
        the marker exists to avoid; none would leave a connector with no model
        to name. A kind whose catalogue is discovered at runtime lists no
        models and therefore marks none: the self-hosted one is the case where
        any default we could write down would be a guess about someone else's
        server, and its connector needs none (its health probe is
        ``GET /models``, and every action route now carries a chosen model).
        """
        for interaction in self.interactions:
            offered = self.models_for(interaction)
            if not offered:
                continue
            defaults = [m.id for m in offered if m.default]
            if len(defaults) != 1:
                found = ", ".join(defaults) or "none"
                raise ValueError(
                    f"provider {self.label!r} must mark exactly one {interaction.value} "
                    f"model as default (found: {found})"
                )
        return self

    def find(self, model_id: str) -> ModelSpec | ModelPattern | None:
        """Curated entry for a model id, by exact match then by glob.

        Returns None for a model we have never annotated, which callers must
        read as "unknown", never as "unsupported".
        """
        for model in self.models:
            if model.id == model_id:
                return model
        # First match wins, so model_patterns is ordered most specific first:
        # "*whisper*.en" has to be listed above "*whisper*" or the broader glob
        # swallows the English-only checkpoints and mislabels their languages.
        for pattern in self.model_patterns:
            if pattern.matches(model_id):
                return pattern
        return None

    def models_for(self, interaction: Interaction) -> list[ModelSpec]:
        """Models offered for an interaction. Hidden entries are excluded."""
        return [m for m in self.models if interaction in m.interactions and not m.hidden]

    def default_model(self, interaction: Interaction) -> str | None:
        """The model marked default for an interaction, or None.

        None means this provider curates no catalogue for it (the self-hosted
        kind), never "pick something plausible": a connector that gets None
        omits the model and lets the endpoint apply its own, which is the only
        honest answer for a server whose models we have never seen.
        """
        return next((m.id for m in self.models_for(interaction) if m.default), None)

    def annotations_for(self, interaction: Interaction) -> list[ModelSpec | ModelPattern]:
        """Every capability source for an interaction, models and patterns.

        Patterns are included because a provider whose catalogue is discovered
        at runtime lists no models, and reading only the model list would
        report it as unable to do anything. Hidden models are excluded: an
        unverified model must not grant its kind a capability.
        """
        entries: list[ModelSpec | ModelPattern] = [
            m for m in self.models if interaction in m.interactions and not m.hidden
        ]
        entries.extend(p for p in self.model_patterns if interaction in p.interactions)
        return entries

    def surface(
        self, interaction: Interaction, transport: Transport | None = None
    ) -> Surface | None:
        """The declared surface for one interaction, and transport if transcribing.

        None means this vendor is not reached that way at all, which the
        validators above make the same statement as "does not declare it".
        """
        block = self.surfaces.for_interaction(interaction)
        if isinstance(block, TranscribeSurfaces):
            return block.for_transport(transport) if transport else None
        return block

    def catalog(self, interaction: Interaction) -> Surface | None:
        """Where the vendor's own model list for an interaction is read, or None."""
        if interaction not in self.interactions:
            return None
        return self.surfaces.catalog_for(interaction)


class CapabilityConfig(_Strict):
    """The whole file."""

    version: int
    providers: dict[ProviderKind, ProviderSpec]
    # Fallback name matching for catalogues that publish no capability metadata
    # at all (OpenAI's mixed /models, a self-hosted server's list). Kept as data
    # so adding a marker is a yaml edit, not a code change.
    transcribe_name_markers: list[str] = Field(default_factory=list[str])
    realtime_name_markers: list[str] = Field(default_factory=list[str])

    @model_validator(mode="after")
    def _every_kind_covered(self) -> Self:
        """Fail if a ProviderKind has no entry.

        A kind the code can construct but this file never describes would fall
        through every capability lookup as "unknown" and quietly offer the
        wrong controls. Adding a kind must mean describing it.
        """
        missing = sorted(k.value for k in ProviderKind if k not in self.providers)
        if missing:
            raise ValueError(
                f"capabilities.yaml has no entry for provider kind(s): {', '.join(missing)}"
            )
        return self

    def provider(self, kind: ProviderKind) -> ProviderSpec | None:
        return self.providers.get(kind)


def load(path: Path | None = None) -> CapabilityConfig:
    """Parse and validate the capability config.

    Raises on a malformed file rather than degrading: a broken capabilities.yaml
    means every picker in the app is wrong, and that should stop startup and CI,
    not ship. Staleness against a live vendor catalogue is the opposite case and
    is handled separately, fail soft, by the checks in loreline.staleness.
    """
    source = path or CONFIG_PATH
    raw = yaml.safe_load(source.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{source} must contain a mapping at the top level")
    return CapabilityConfig.model_validate(raw)
