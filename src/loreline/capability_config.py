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
  key come from, is there a catalogue endpoint to call at runtime.
* model - which interactions this specific model serves.
* capability block - the per-interaction parameter surface: whether glossary
  biasing exists and under which request field, whether reasoning effort may be
  offered, which video durations are legal. This is what lets the UI hide a
  control instead of showing one that silently does nothing.
"""

from __future__ import annotations

import fnmatch
from pathlib import Path
from typing import Literal, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from loreline.models import Interaction, ProviderKind

CONFIG_PATH = Path(__file__).with_name("capabilities.yaml")


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
    serves both and the pair decides which connector runs. ``realtime`` is also
    what the Realtime badge reports and what gates a model for live capture.
    """

    realtime: bool = False
    batch: bool = True
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
    # typo here would silently stop guarding a combination that errors.
    _CONFLICTABLE = frozenset({"glossary", "inline_diarization", "word_timestamps"})
    # A conflict is a statement about a pair, so a group of one says nothing.
    _MIN_CONFLICT_GROUP = 2

    @model_validator(mode="after")
    def _at_least_one_transport(self) -> Self:
        if not (self.realtime or self.batch):
            raise ValueError("a transcription model must support realtime or batch")
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
    # Vendor-announced sunset, ISO date. A property of the model as a whole,
    # not of one capability: OpenAI is retiring whole transcription models, and
    # the Sora video models, on stated dates. Shown as a warning beside the
    # model rather than hiding it, because a GM mid-campaign should not lose
    # their model the day we notice the announcement.
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


class ProviderSpec(_Strict):
    """One vendor. Exactly one entry per vendor, capabilities as flags."""

    label: str
    hosting: Literal["cloud", "selfhosted"] = "cloud"
    # ``optional`` covers a self-hosted server that may or may not check a key.
    auth: Literal["api_key", "optional", "none"] = "api_key"
    key_url: str | None = None
    # None means the operator must supply one (self-hosted has no sensible
    # default and guessing localhost would be a lie).
    base_url: str | None = None
    # Runtime model discovery. A bare string when one endpoint serves every
    # interaction; a mapping when the vendor splits its catalogue, as OpenRouter
    # does across three disjoint lists. None means this file is the catalogue.
    catalog_endpoint: str | dict[Interaction, str] | None = None
    # False for a provider we allow for stored-audio work but never for a live
    # session. A policy switch, not a technical one: see the OpenRouter note in
    # the yaml.
    live_capture: bool = True
    interactions: list[Interaction]
    models: list[ModelSpec] = Field(default_factory=list[ModelSpec])
    model_patterns: list[ModelPattern] = Field(default_factory=list[ModelPattern])

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
