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

from functools import lru_cache

from loreline.capability_config import CapabilityConfig, ProviderSpec
from loreline.capability_config import load as _load_config
from loreline.models import Interaction, ModelInfo, ProviderKind


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


def interactions_for(kind: ProviderKind) -> frozenset[Interaction]:
    """Interactions a provider kind can serve (empty for an unknown kind)."""
    spec = _provider(kind)
    return frozenset(spec.interactions) if spec else frozenset()


def supports(kind: ProviderKind, interaction: Interaction) -> bool:
    """Whether a provider kind can serve this interaction at all."""
    return interaction in interactions_for(kind)


def default_model(kind: ProviderKind, interaction: Interaction) -> str | None:
    """The model to use for this pair when the caller named none.

    Scoped by interaction on purpose. A provider row serves several at once -
    OpenRouter transcribes, summarizes and generates video - so "the model for
    this kind" is not a question with an answer, and the last attempt at one
    ("first non-hidden model of any interaction") handed a chat model to a
    transcription provider. This asks the narrower question the yaml can
    actually answer, next to the deprecation date that invalidates it.

    Only one caller has no model from anyone: the health probe behind
    POST /providers/{id}/test, whose websocket kinds name a model in the
    handshake. Every action route requires the GM to choose. None means this
    kind curates no catalogue (the self-hosted one), and the connector then
    sends no model at all.
    """
    spec = _provider(kind)
    return spec.default_model(interaction) if spec else None


def curates_a_catalogue(kind: ProviderKind) -> bool:
    """Whether this file lists models for a kind at all (any interaction).

    The difference between "we vouch for nothing here on purpose" and "we have
    nothing left". The self-hosted kind curates nothing by design, since its
    catalogue is whatever the operator installed, and a connector for it works
    perfectly well naming no model. A kind that does curate must always leave
    a default behind; if it does not, something is wrong with the file rather
    than with the request, and the caller should hear that instead of the
    vendor's complaint about a missing field.
    """
    spec = _provider(kind)
    return bool(spec and spec.models)


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
    Gemini publishes no list this app fetches, so its chat models exist only
    here. Empty for a kind that lists none, which is not the same as "offer
    everything": see :mod:`loreline.stt.catalog` for what that falls back to.
    """
    spec = _provider(kind)
    return [m.id for m in spec.models_for(interaction)] if spec else []


def _offered_transcribers(kind: ProviderKind) -> list[object]:
    """Transcription models this kind actually lists in a picker."""
    spec = _provider(kind)
    return list(spec.models_for(Interaction.TRANSCRIBE)) if spec else []


def _transcribe_annotations(kind: ProviderKind) -> list[object]:
    """Every transcription capability source, including glob patterns.

    Kinds whose catalogue is discovered at runtime (the self-hosted one) list
    no models at all, so their transports are declared by pattern. Reading only
    the model list would report such a kind as unable to transcribe.
    """
    spec = _provider(kind)
    return list(spec.annotations_for(Interaction.TRANSCRIBE)) if spec else []


def _streams_only(kind: ProviderKind) -> bool:
    """Whether every transcription model this kind offers is a streaming one.

    This is what settles two questions the per-model data alone cannot. For a
    model that supports *both* transports, it picks which connector runs:
    Deepgram's Nova streams while OpenAI's gpt-transcribe posts, which is the
    routing those configs have always had. Deepgram now has a batch connector
    too, but every model it *offers* streams, so Nova keeps the lower-latency
    transport and only a batch-only model reaches the other one. And for a model
    nobody has curated, this beats the name markers: an unrecognised Deepgram
    model is assumed to stream, because everything Deepgram offers here does.
    """
    offered = _offered_transcribers(kind)
    if not offered:
        return False
    return all(m.transcribe and m.transcribe.realtime for m in offered)  # type: ignore[attr-defined]


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


def kinds_with_inline_diarization() -> frozenset[ProviderKind]:
    """Provider kinds with at least one inline-diarization-capable model."""
    return frozenset(
        kind
        for kind in config().providers
        for m in _offered_transcribers(kind)
        if m.transcribe and m.transcribe.inline_diarization  # type: ignore[attr-defined]
    )


def is_realtime_model(kind: ProviderKind, model: str | None) -> bool:
    """Whether this provider+model pair transcribes over a streaming transport.

    This picks the connector for kinds that offer both transports, so it must
    answer for any model string, curated or not. None reaches here only for a
    kind that curates no catalogue, since :func:`default_model` has already
    answered for every other one; such a kind keeps the transport it has
    always had.
    """
    spec = _provider(kind)
    if spec is None:
        return False
    if model is None:
        # No model to resolve against: the kind lists none of its own and the
        # caller named none either. Fall back to the kind's own transport,
        # which is the connector such a config has always got.
        return supports_realtime(kind)
    entry = spec.find(model)
    caps = entry.transcribe if entry else None
    if caps is None:
        return _guess_transport(kind, model)
    if caps.realtime and caps.batch:
        # Curated for both. The kind decides, which keeps Deepgram's Nova on
        # the streaming connector and OpenAI's gpt-transcribe on the batch one.
        return _streams_only(kind)
    return caps.realtime


def _guess_transport(kind: ProviderKind, model: str) -> bool:
    """Transport for a model nobody has annotated."""
    if _streams_only(kind):
        return True
    if not supports_realtime(kind):
        return False
    # A kind that splits its catalogue across transports. Both such vendors put
    # the transport in the name, and misrouting a brand-new streaming model to
    # the batch endpoint would fail anyway, so the guess costs nothing over the
    # curated set alone.
    lowered = model.lower()
    return any(marker in lowered for marker in config().realtime_name_markers)


def supports_realtime(kind: ProviderKind) -> bool:
    """Whether this kind can transcribe over a streaming transport at all."""
    return any(m.transcribe and m.transcribe.realtime for m in _transcribe_annotations(kind))  # type: ignore[attr-defined]


def supports_batch(kind: ProviderKind) -> bool:
    """Whether this kind can transcribe by posting a complete utterance.

    Not the complement of :func:`supports_realtime`: OpenAI does both, keyed on
    the model (gpt-transcribe posts, gpt-live-transcribe streams).
    """
    return any(m.transcribe and m.transcribe.batch for m in _transcribe_annotations(kind))  # type: ignore[attr-defined]


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


def _interactions_by_kind() -> dict[ProviderKind, frozenset[Interaction]]:
    return {kind: interactions_for(kind) for kind in config().providers}


# Backwards-compatible view of the table that used to be defined here by hand.
# Derived from the yaml so there is still exactly one source of truth.
INTERACTIONS_BY_KIND: dict[ProviderKind, frozenset[Interaction]] = _interactions_by_kind()

# Kinds that can transcribe stored audio but must never drive a live capture.
LIVE_CAPTURE_EXCLUDED: frozenset[ProviderKind] = frozenset(
    kind
    for kind, spec in config().providers.items()
    if Interaction.TRANSCRIBE in spec.interactions and not spec.live_capture
)

# Kinds with at least one streaming transcription model. Not the same question
# as "can it drive a live session": loreline feeds every connector VAD-chunked
# utterances, so a batch connector works live too. Realtime is about latency
# within an utterance, and it is what the UI badges report.
REALTIME_KINDS: frozenset[ProviderKind] = frozenset(
    kind for kind in config().providers if supports_realtime(kind)
)
