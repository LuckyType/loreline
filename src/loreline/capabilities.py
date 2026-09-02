"""Which provider kinds can serve which interaction, and which models qualify.

The single source of truth for "is this provider+model combination possible at
all". It exists because the pickers used to offer anything a provider's
``/models`` endpoint returned: OpenAI's lists ``dall-e-3`` and ``tts-1``
alongside ``whisper-1``, so a GM could pick an image model to transcribe with
and only find out when the job failed.

Two layers:

* :data:`INTERACTIONS_BY_KIND` - a hand-maintained table of what each kind is
  *for*. Small, closed, and the thing to edit when a kind gains an ability.
* :func:`filter_models` - narrows a fetched model list to the ones that can
  actually serve an interaction.

The filtering deliberately prefers the provider's own metadata over guesswork.
OpenRouter publishes modalities per model, so its lists are filtered by fetching
the right catalogue rather than by pattern-matching names. Only OpenAI-style
endpoints, whose ``/models`` carries no capability information at all, fall back
to the curated name markers below - and even then a filter that would empty the
list is discarded rather than trusted (see :func:`filter_models`), because a
self-hosted server may legitimately name its models in a way this file has never
seen. Hiding a model the operator installed is a worse failure than showing one
extra.
"""

from __future__ import annotations

from loreline.models import Interaction, ModelInfo, ProviderKind

# What each provider kind is for. A kind absent from a value here is never
# offered for that interaction anywhere in the UI or accepted by the API.
INTERACTIONS_BY_KIND: dict[ProviderKind, frozenset[Interaction]] = {
    ProviderKind.DEEPGRAM: frozenset({Interaction.TRANSCRIBE}),
    ProviderKind.ASSEMBLYAI: frozenset({Interaction.TRANSCRIBE}),
    ProviderKind.GEMINI: frozenset({Interaction.TRANSCRIBE}),
    ProviderKind.VOSK: frozenset({Interaction.TRANSCRIBE}),
    # One entry per vendor rather than one per role. A provider that can do
    # several things says so here, and every picker is scoped by interaction
    # (see filter_models and the model catalogues), so a single stored
    # ProviderConfig can serve all of them without offering a chat model for
    # transcription.
    ProviderKind.OPENAI: frozenset({Interaction.TRANSCRIBE, Interaction.SUMMARIZE}),
    ProviderKind.OPENAI_COMPAT: frozenset({Interaction.TRANSCRIBE, Interaction.SUMMARIZE}),
    ProviderKind.OPENROUTER: frozenset(
        {Interaction.TRANSCRIBE, Interaction.SUMMARIZE, Interaction.VIDEO}
    ),
}

# Kinds that can transcribe stored audio but must never drive a live capture.
#
# OpenRouter's transcription API is a single request/response file upload with
# no streaming, realtime or websocket mode of any kind (confirmed against their
# SDK, the live model metadata, and their own docs). Loreline *could* still
# drive it live, since capture posts one VAD-chunked utterance at a time exactly
# as the OPENAI_COMPAT kind does - so this is a deliberate policy choice, not a
# technical impossibility: a cloud round trip per utterance during play is the
# wrong trade when purpose-built streaming backends (Deepgram, AssemblyAI,
# OpenAI Realtime) exist. Re-processing stored audio has no such deadline.
LIVE_CAPTURE_EXCLUDED: frozenset[ProviderKind] = frozenset({ProviderKind.OPENROUTER})

# Substrings identifying a transcription model on an endpoint that publishes no
# capability metadata (OpenAI's cloud ``/models``, and self-hosted Speaches /
# whisper.cpp / faster-whisper servers). Hand-maintained from the model names
# those endpoints actually serve - "whisper-1", "gpt-4o-transcribe",
# "Systran/faster-whisper-large-v3", "nvidia/parakeet-tdt", "distil-whisper".
# Used only as a fallback, and only when it does not empty the list.
_TRANSCRIBE_NAME_MARKERS = ("whisper", "transcribe", "parakeet", "asr", "stt", "voxtral", "nova")


# Models whose transcript this app can read *speaker labels* out of - i.e. for
# which "Inline (from STT)" diarization actually produces speakers.
#
# This is the intersection of two things, and both matter: the provider must
# offer diarization for that model, AND this repo's connector must extract it.
# Only Deepgram and Gemini satisfy both today (see the `speaker=` assignments in
# stt/backends/deepgram.py and stt/backends/gemini.py); every other connector
# drops speaker information even where the provider has it.
#
# Checked against provider documentation on 2026-09-02:
# - https://developers.deepgram.com/docs/diarization - Nova-1/2/3, enhanced and
#   base support `diarize`; Whisper explicitly does not, and Flux does not list
#   it among its supported parameters.
# - https://www.assemblyai.com/docs/streaming/label-speakers-and-separate-channels
#   - `speaker_labels: true` works on all three streaming models.
#
# Not listed, and why (openai_compat): Speaches exposes speaker *embeddings*
# (POST /v1/audio/speech/embedding, 512-d vectors) but no diarization and no
# speaker labels on the transcription response - a caller must cluster them
# itself. That makes it a candidate source for the *remote* diarizer path, not
# inline STT diarization.
#
# - https://openrouter.ai/x-ai/grok-stt-1.0 - "transcription with word-level
#   timestamps, optional speaker diarization, and multichannel audio". It is
#   the only model in OpenRouter's transcription catalogue that advertises
#   diarization, and it needs no request flag: the labels simply appear on the
#   verbose_json body's words/segments, which the OpenAI-compatible connector
#   now parses.
_INLINE_DIARIZATION_MODELS: dict[ProviderKind, frozenset[str]] = {
    ProviderKind.DEEPGRAM: frozenset(
        {
            "nova-3",
            "nova-3-general",
            "nova-3-medical",
            "nova-2",
            "nova-2-meeting",
            "nova-2-phonecall",
            "nova-2-conversationalai",
            "nova-2-video",
        }
    ),
    ProviderKind.ASSEMBLYAI: frozenset(
        {
            "universal-3-5-pro",
            "universal-streaming-english",
            "universal-streaming-multilingual",
        }
    ),
    ProviderKind.GEMINI: frozenset({"gemini-3.5-transcribe"}),
    ProviderKind.OPENROUTER: frozenset({"x-ai/grok-stt-1.0"}),
}


def supports_inline_diarization(kind: ProviderKind, model: str | None) -> bool:
    """Whether "Inline (from STT)" yields real speakers for this provider+model.

    False for an unknown or unset model: offering a diarization mode that
    silently produces no speakers is worse than not offering it, and a model
    nobody has curated here is exactly the case we cannot vouch for.
    """
    if not model:
        return False
    return model in _INLINE_DIARIZATION_MODELS.get(kind, frozenset())


def kinds_with_inline_diarization() -> frozenset[ProviderKind]:
    """Provider kinds that have at least one inline-diarization-capable model."""
    return frozenset(_INLINE_DIARIZATION_MODELS)


def interactions_for(kind: ProviderKind) -> frozenset[Interaction]:
    """Interactions a provider kind can serve (empty for an unknown kind)."""
    return INTERACTIONS_BY_KIND.get(kind, frozenset())


def supports(kind: ProviderKind, interaction: Interaction) -> bool:
    """Whether a provider kind can serve this interaction at all."""
    return interaction in interactions_for(kind)


def kinds_for(interaction: Interaction) -> frozenset[ProviderKind]:
    """Every provider kind that can serve an interaction."""
    return frozenset(k for k, v in INTERACTIONS_BY_KIND.items() if interaction in v)


# Kinds with a streaming transcription connector, i.e. one that emits
# transcript updates *within* an utterance rather than one result per utterance.
#
# This is not the same question as "can it drive a live session": loreline feeds
# every connector VAD-chunked utterances, so a batch connector works live too
# (that is how the self-hosted OPENAI_COMPAT kind has always run). Realtime is
# about latency within an utterance, and it is what the UI badges report.
#
# Membership here says "at least one model streams", not "every model does":
# OpenAI also serves batch-only transcription models (whisper-1,
# gpt-transcribe), which is why connector selection is per model, not per kind
# (see is_realtime_model and loreline.stt.registry).
REALTIME_KINDS: frozenset[ProviderKind] = frozenset(
    {ProviderKind.DEEPGRAM, ProviderKind.ASSEMBLYAI, ProviderKind.OPENAI}
)

# Kinds whose every offered transcription model streams. Deepgram's hosted
# Whisper models are batch-only but deliberately not offered (see the curated
# list in loreline.stt.catalog), so within this app the whole kind streams.
_REALTIME_ONLY_KINDS: frozenset[ProviderKind] = frozenset(
    {ProviderKind.DEEPGRAM, ProviderKind.ASSEMBLYAI}
)

# For kinds that split their catalogue across two transports: the models that
# ride the streaming one. A kind absent here is single-transport, so its models
# need no per-model classification. Gemini is listed even though this app has
# no Live API connector yet: classifying gemini-3.5-transcribe-live as
# streaming makes the registry refuse it with a message that says what is
# missing, instead of posting it to the batch endpoint and surfacing whatever
# error Google returns for a transport mismatch.
#
# Checked against provider documentation on 2026-09-02:
# - https://developers.openai.com/api/docs/guides/realtime-transcription
# - https://ai.google.dev/gemini-api/docs/transcribe
_REALTIME_MODELS: dict[ProviderKind, frozenset[str]] = {
    ProviderKind.OPENAI: frozenset({"gpt-live-transcribe", "gpt-realtime-whisper"}),
    ProviderKind.GEMINI: frozenset({"gemini-3.5-transcribe-live"}),
}

# Name fallback for a mixed-transport kind's model that nobody has curated
# above yet: both vendors put the transport in the name ("live", "realtime"),
# and misrouting a brand-new streaming model to the batch endpoint would fail
# anyway, so the guess costs nothing over the curated set alone.
_REALTIME_NAME_MARKERS = ("live", "realtime")


def is_realtime_model(kind: ProviderKind, model: str | None) -> bool:
    """Whether this provider+model pair transcribes over a streaming transport.

    This is what picks the connector for kinds that offer both transports, so
    it must answer for any model string, curated or not. An unset model keeps
    the kind's historical default connector: OpenAI configs predating per-model
    resolution have always run the Realtime session.
    """
    if kind in _REALTIME_ONLY_KINDS:
        return True
    known = _REALTIME_MODELS.get(kind)
    if known is None:
        return False
    if model is None:
        return kind in REALTIME_KINDS
    if model in known:
        return True
    lowered = model.lower()
    return any(marker in lowered for marker in _REALTIME_NAME_MARKERS)


def supports_realtime(kind: ProviderKind) -> bool:
    """Whether this kind can transcribe over a streaming transport at all."""
    return kind in REALTIME_KINDS


def supports_batch(kind: ProviderKind) -> bool:
    """Whether this kind can transcribe by posting a complete utterance.

    Not the complement of supports_realtime: OpenAI does both, keyed on the
    model (whisper-1 and gpt-transcribe post, gpt-live-transcribe streams).
    """
    return supports(kind, Interaction.TRANSCRIBE) and kind not in _REALTIME_ONLY_KINDS


def supports_live_capture(kind: ProviderKind) -> bool:
    """Whether a kind may drive a live capture session (not just re-processing)."""
    return supports(kind, Interaction.TRANSCRIBE) and kind not in LIVE_CAPTURE_EXCLUDED


def _looks_like_transcription(model_id: str) -> bool:
    lowered = model_id.lower()
    return any(marker in lowered for marker in _TRANSCRIBE_NAME_MARKERS)


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

    ``strict=False`` disables the narrowing entirely: the markers below are a
    hand-maintained guess, and a model released tomorrow, or an endpoint nobody
    here has seen, will not match them. The setting behind this
    (``ActionDefaults.strict_model_filtering``, on by default) is the escape
    hatch that keeps this file from becoming a gate on what the operator is
    allowed to run.

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
