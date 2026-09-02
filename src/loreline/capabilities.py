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
    ProviderKind.OPENAI: frozenset({Interaction.TRANSCRIBE}),
    ProviderKind.OPENAI_COMPAT: frozenset({Interaction.TRANSCRIBE}),
    ProviderKind.ASSEMBLYAI: frozenset({Interaction.TRANSCRIBE}),
    ProviderKind.GEMINI: frozenset({Interaction.TRANSCRIBE}),
    ProviderKind.VOSK: frozenset({Interaction.TRANSCRIBE}),
    ProviderKind.OPENAI_CHAT: frozenset({Interaction.SUMMARIZE}),
    ProviderKind.OPENROUTER: frozenset({Interaction.SUMMARIZE, Interaction.VIDEO}),
    ProviderKind.OPENROUTER_STT: frozenset({Interaction.TRANSCRIBE}),
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
LIVE_CAPTURE_EXCLUDED: frozenset[ProviderKind] = frozenset({ProviderKind.OPENROUTER_STT})

# Substrings identifying a transcription model on an endpoint that publishes no
# capability metadata (OpenAI's cloud ``/models``, and self-hosted Speaches /
# whisper.cpp / faster-whisper servers). Hand-maintained from the model names
# those endpoints actually serve - "whisper-1", "gpt-4o-transcribe",
# "Systran/faster-whisper-large-v3", "nvidia/parakeet-tdt", "distil-whisper".
# Used only as a fallback, and only when it does not empty the list.
_TRANSCRIBE_NAME_MARKERS = ("whisper", "transcribe", "parakeet", "asr", "stt", "voxtral", "nova")


def interactions_for(kind: ProviderKind) -> frozenset[Interaction]:
    """Interactions a provider kind can serve (empty for an unknown kind)."""
    return INTERACTIONS_BY_KIND.get(kind, frozenset())


def supports(kind: ProviderKind, interaction: Interaction) -> bool:
    """Whether a provider kind can serve this interaction at all."""
    return interaction in interactions_for(kind)


def kinds_for(interaction: Interaction) -> frozenset[ProviderKind]:
    """Every provider kind that can serve an interaction."""
    return frozenset(k for k, v in INTERACTIONS_BY_KIND.items() if interaction in v)


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
