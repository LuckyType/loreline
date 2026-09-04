"""Which provider kinds can serve which interaction, and which models qualify.

Every answer here is derived from ``capabilities.yaml``; this module is the
typed, cached read side of that file. It exists because the pickers used to
offer anything a provider's ``/models`` endpoint returned: OpenAI's lists
``dall-e-3`` and ``tts-1`` alongside ``whisper-1``, so a GM could pick an image
model to transcribe with and only find out when the job failed.

Until now the same facts were also written by hand in TypeScript so the browser
could filter its pickers, and the two copies had drifted. The yaml is now the
one place they are written, and the frontend reads it over ``/api/capabilities``
rather than restating it.

The filtering deliberately prefers a provider's own metadata over guesswork.
OpenRouter publishes modalities per model, so its lists are filtered by fetching
the right catalogue rather than by pattern-matching names. Only OpenAI-style
endpoints, whose ``/models`` carries no capability information at all, fall back
to the curated name markers in the yaml - and even then a filter that would
empty the list is discarded rather than trusted (see :func:`filter_models`),
because a self-hosted server may legitimately name its models in a way nobody
has seen. Hiding a model the operator installed is a worse failure than showing
one extra.

An unlisted model is *unknown*, never *unsupported*. Callers that gate a feature
on a capability treat unknown as "do not promise it" (diarization), while
callers that gate availability treat unknown as "allow it" (model filtering),
which is what keeps this file from becoming a gate on what an operator may run.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from urllib.parse import urlencode

from loreline.capability_config import (
    CapabilityConfig,
    HealthProbe,
    ModelPattern,
    ModelSpec,
    ProviderSpec,
    Surface,
    Transport,
)
from loreline.capability_config import load as _load_config
from loreline.models import Interaction, ModelInfo, ProviderConfig, ProviderKind


@lru_cache(maxsize=1)
def config() -> CapabilityConfig:
    """The parsed capability file, read once per process.

    Cached because every picker render asks it several questions. Tests that
    need a different file call :func:`reload`.
    """
    return _load_config()


def reload() -> CapabilityConfig:
    """Drop the cached config and read the file again."""
    config.cache_clear()
    return config()


def _provider(kind: ProviderKind) -> ProviderSpec | None:
    return config().provider(kind)


@dataclass(frozen=True, slots=True)
class Endpoint:
    """A surface after the provider row had its say: the address is final.

    What a connector builds its client on. ``url`` is never None here, which
    is the whole difference from :class:`Surface`: a surface may leave the
    address to the operator, an endpoint is one that has it.
    """

    url: str
    surface: Surface

    @property
    def health(self) -> HealthProbe | None:
        """What the health probe asks here, if the surface says; see HealthProbe."""
        return self.surface.health

    def request_headers(self, api_key: str | None) -> dict[str, str]:
        """The credential in this surface's spelling, plus its fixed headers."""
        return self.surface.request_headers(api_key)

    def url_with_key(self, api_key: str | None) -> str:
        """The address with the credential in its query string, where that is
        how the surface authenticates (Gemini's Live socket); else the address."""
        query = self.surface.auth.query(api_key)
        if not query:
            return self.url
        separator = "&" if "?" in self.url else "?"
        return f"{self.url}{separator}{urlencode(query)}"


def surface(
    kind: ProviderKind, interaction: Interaction, transport: Transport | None = None
) -> Surface | None:
    """The declared surface for one kind, interaction and transport, or None.

    The declaration as written, before any provider row's override. Callers
    building a client want :func:`surface_for`; this is for the readers that
    have no row in hand.
    """
    spec = _provider(kind)
    return spec.surface(interaction, transport) if spec else None


def surface_for(
    config: ProviderConfig, interaction: Interaction, transport: Transport | None = None
) -> Endpoint:
    """The endpoint a provider row reaches for one interaction and transport.

    Applies the row's ``base_url`` where the surface allows it (see
    :meth:`Surface.resolve` for the transport rule), and raises ``ValueError``
    when there is nothing to call: the kind declares no such surface, or the
    surface is one only the operator can locate and the row names no address.
    Raising is right here: a connector with nowhere to post is a configuration
    error, and the message names the missing piece rather than letting a
    request fail against a URL nobody chose.
    """
    spec = _provider(config.kind)
    declared = spec.surface(interaction, transport) if spec else None
    if spec is None or declared is None:
        where = f"{interaction.value} over {transport}" if transport else interaction.value
        raise ValueError(f"{config.kind.value} declares no surface for {where}")
    url = declared.resolve(config.base_url)
    if url is None:
        raise ValueError(f"{spec.label} needs a base URL to {interaction.value}")
    return Endpoint(url, declared)


def catalog_for(
    kind: ProviderKind, interaction: Interaction, *, base_url: str | None = None
) -> Endpoint | None:
    """Where a kind's own model list for an interaction is read, or None.

    None is a normal answer, not an error: the vendor publishes no list for
    this interaction, or the list lives on a server whose address only the
    operator knows and ``base_url`` did not supply it. Callers fall back to
    the curated models in the file.
    """
    spec = _provider(kind)
    declared = spec.catalog(interaction) if spec else None
    if declared is None:
        return None
    url = declared.resolve(base_url)
    return Endpoint(url, declared) if url else None


def interactions_for(kind: ProviderKind) -> frozenset[Interaction]:
    """Interactions a provider kind can serve (empty for an unknown kind)."""
    spec = _provider(kind)
    return frozenset(spec.interactions) if spec else frozenset()


def supports(kind: ProviderKind, interaction: Interaction) -> bool:
    """Whether a provider kind can serve this interaction at all."""
    return interaction in interactions_for(kind)


def requires_api_key(kind: ProviderKind) -> bool:
    """Whether a probe or a request without a key is certain to be refused.

    False for the self-hosted kind (``auth: optional``: the operator's server
    may or may not check one) and for anything this file does not know, which
    keeps the caller on the "ask the endpoint" path rather than pre-judging it.
    Used by the health probe to answer "no key stored" without a network call -
    see :func:`loreline.health.missing_credential` for why that matters beyond
    saving a round trip.
    """
    spec = _provider(kind)
    return spec is not None and spec.auth == "api_key"


def default_model(kind: ProviderKind, interaction: Interaction) -> str | None:
    """The model to use for this pair when the caller named none.

    Scoped by interaction on purpose. A provider row serves several at once -
    OpenRouter transcribes, summarizes and generates video - so "the model for
    this kind" is not a question with an answer, and the last attempt at one
    ("first non-hidden model of any interaction") handed a chat model to a
    transcription provider. This asks the narrower question the yaml can
    actually answer, next to the deprecation date that invalidates it.

    No connector is handed this any more: every action route requires the GM
    to choose, and the health probe asks a surface rather than a model. What
    the marker still decides is the kind's house transport (the surface the
    probe asks, the connector an unannotated model is routed to; see
    :func:`is_realtime_model`) and, through :func:`default_diarizing_model`,
    which model a diarizing pass runs. None means this kind curates no
    catalogue (the self-hosted one).
    """
    spec = _provider(kind)
    return spec.default_model(interaction) if spec else None


def default_diarizing_model(kind: ProviderKind) -> str | None:
    """The model to run when the caller needs speaker labels, not just text.

    A different question from :func:`default_model`, which picks for
    transcription and on OpenAI picks gpt-transcribe - a model that returns no
    speakers at all, so a diarization pass cannot inherit it. This asks which
    of the offered models this file says returns speakers.

    Answered without depending on list order: the interaction default when it
    diarizes (Deepgram, AssemblyAI and Gemini all do with theirs), else the one
    model that does. Several candidates with the default not among them is
    ambiguous rather than defaultable, and returns None so the caller names one
    - the guard test in tests/unit/test_capabilities.py fails the moment a kind
    lands in that state, which is the point at which a human has to choose.
    """
    spec = _provider(kind)
    if spec is None:
        return None
    candidates = [
        m.id
        for m in spec.models_for(Interaction.TRANSCRIBE)
        if m.transcribe and m.transcribe.inline_diarization
    ]
    preferred = spec.default_model(Interaction.TRANSCRIBE)
    if preferred in candidates:
        return preferred
    return candidates[0] if len(candidates) == 1 else None


def kinds_for(interaction: Interaction) -> frozenset[ProviderKind]:
    """Every provider kind that can serve an interaction."""
    return frozenset(k for k in config().providers if supports(k, interaction))


def curated_models(kind: ProviderKind, interaction: Interaction) -> list[str]:
    """Model ids this file offers for one interaction, hidden entries excluded.

    The fallback catalogue for a kind whose models are not discovered live -
    Gemini publishes no list the pickers read, so its chat models exist only
    here. Empty for a kind that lists none, which is not the same as "offer
    everything": see :mod:`loreline.stt.catalog` for what that falls back to.

    This is the *only* gate on what a picker offers. A model listed here and
    not hidden is offered; anything else is not. The second gate that used to
    stand beside it (``_CURATED`` in :mod:`loreline.stt.catalog`) is gone: two
    lists meant a model could be curated in one and withheld by the other, and
    which one won depended on the code path.
    """
    spec = _provider(kind)
    return [m.id for m in spec.models_for(interaction)] if spec else []


def _transcribe_annotations(kind: ProviderKind) -> list[ModelSpec | ModelPattern]:
    """Every transcription capability source, including glob patterns.

    Kinds whose catalogue is discovered at runtime (the self-hosted one) list
    no models at all, so their transports are declared by pattern. Reading only
    the model list would report such a kind as unable to transcribe.
    """
    spec = _provider(kind)
    return list(spec.annotations_for(Interaction.TRANSCRIBE)) if spec else []


def _default_transport(kind: ProviderKind) -> bool:
    """Whether the model this kind falls back to runs on the streaming connector.

    The answer for a request that names no model, or names one nothing here
    annotates. A kind that curates a catalogue always marks exactly one
    transcription default (the loader enforces it), and that model's own
    declared transport is the closest thing to a house style this file has. A
    kind that curates nothing has no house style, and batch is the transport
    such a config has always got.
    """
    spec = _provider(kind)
    if spec is None:
        return False
    chosen = spec.default_model(Interaction.TRANSCRIBE)
    entry = spec.find(chosen) if chosen else None
    caps = entry.transcribe if entry else None
    return bool(caps and caps.prefers_realtime)


def supports_inline_diarization(kind: ProviderKind, model: str | None) -> bool:
    """Whether "Inline (from STT)" yields real speakers for this pair.

    False for an unknown or unset model: offering a diarization mode that
    silently produces no speakers is worse than not offering it, and a model
    nobody has curated is exactly the case we cannot vouch for. This is the
    intersection of two things - the provider must offer diarization for that
    model, AND this repo's connector must extract it - so the yaml records the
    intersection, not the vendor's claim. OpenRouter is the clearest case:
    x-ai/grok-stt-1.0's own description advertises diarization, but the gateway
    exposes no knob for it and returns no speaker structure.
    """
    if not model:
        return False
    spec = _provider(kind)
    if spec is None:
        return False
    entry = spec.find(model)
    return bool(entry and entry.transcribe and entry.transcribe.inline_diarization)


def is_realtime_model(kind: ProviderKind, model: str | None) -> bool:
    """Whether this provider+model pair transcribes over a streaming transport.

    This picks the connector for kinds that offer both transports, so it must
    answer for any model string, curated or not, and for None, which is the
    kind's own default transport.

    A curated model answers for itself: its single transport, or, when it
    serves both, the ``prefer`` written beside them. Nothing consults the
    sibling list, so hiding or unhiding one model never reroutes another - the
    guard test in tests/unit/test_capabilities.py pins that.
    """
    spec = _provider(kind)
    if spec is None:
        return False
    if model is None:
        # No model to resolve against: the self-hosted kind's connector runs
        # with none, and the health probe asks which transport a kind's own
        # default runs on. Answer with that default's transport rather than
        # "can this kind stream at all", so an unset model and the model that
        # would actually run cannot disagree.
        return _default_transport(kind)
    entry = spec.find(model)
    caps = entry.transcribe if entry else None
    if caps is None:
        return _guess_transport(kind, model)
    # Curated: the model's own transport, or, when it serves both, the
    # preference written beside them. Nothing here consults the sibling list,
    # so hiding or unhiding another model cannot reroute this one.
    return caps.prefers_realtime


def _guess_transport(kind: ProviderKind, model: str) -> bool:
    """Transport for a model nobody has annotated."""
    if not supports_realtime(kind):
        return False
    # A kind that can stream at all. Vendors that split a catalogue across
    # transports put the transport in the name, and misrouting a brand-new
    # streaming model to the batch endpoint would fail anyway, so the marker
    # costs nothing over the curated set alone. Catalogues rot; this is the
    # mechanism for that, and it is deliberately separate from the curated
    # answer above.
    lowered = model.lower()
    if any(marker in lowered for marker in config().realtime_name_markers):
        return True
    # No marker either. Follow the model this kind would have picked for
    # itself: an unrecognised Deepgram id streams because nova-3 does.
    return _default_transport(kind)


def supports_realtime(kind: ProviderKind) -> bool:
    """Whether this kind can transcribe over a streaming transport at all."""
    return any(m.transcribe and m.transcribe.realtime for m in _transcribe_annotations(kind))


def supports_live_capture(kind: ProviderKind) -> bool:
    """Whether a kind may drive a live capture session, not just re-processing.

    OpenRouter is the one exclusion, and it is policy rather than capability:
    its transcription API is a single request/response file upload, and
    loreline *could* drive it live, since capture posts one VAD-chunked
    utterance at a time exactly as the self-hosted kind does. A cloud round
    trip per utterance during play is simply the wrong trade when purpose-built
    streaming backends exist. Re-processing stored audio has no such deadline.
    """
    spec = _provider(kind)
    if spec is None:
        return False
    return Interaction.TRANSCRIBE in spec.interactions and spec.live_capture


def _looks_like_transcription(model_id: str) -> bool:
    lowered = model_id.lower()
    return any(marker in lowered for marker in config().transcribe_name_markers)


def filter_models(
    models: list[ModelInfo],
    *,
    kind: ProviderKind,
    interaction: Interaction,
    strict: bool = True,
) -> list[ModelInfo]:
    """Narrow a fetched model list to those that can serve ``interaction``.

    Only applies to the OpenAI-style catalogues that mix every capability into
    one ``/models`` response. Everywhere else the list is already scoped -
    OpenRouter is fetched from a modality-specific endpoint, and the curated
    per-kind lists contain nothing but transcription models - so it passes
    straight through.

    ``strict=False`` disables the narrowing entirely: the markers are a
    hand-maintained guess, and a model released tomorrow, or an endpoint nobody
    has seen, will not match them. The setting behind this
    (``ActionDefaults.strict_model_filtering``, on by default) is the escape
    hatch that keeps this file from becoming a gate on what the operator runs.

    Even when strict, a filter that would remove *everything* is discarded and
    the unfiltered list returned. That case means the markers simply do not
    recognise this server's naming, and an empty picker would strand an
    operator whose models are perfectly good.
    """
    if not strict:
        return models
    if interaction is not Interaction.TRANSCRIBE:
        return models
    if kind not in (ProviderKind.OPENAI, ProviderKind.OPENAI_COMPAT):
        return models
    matching = [m for m in models if _looks_like_transcription(m.id)]
    return matching or models
