"""List the models a provider offers, for the on-demand model pickers.

OpenAI-compatible endpoints (OpenAI cloud, OpenRouter, Speaches, Ollama, LM Studio,
…) expose ``GET /v1/models``; the others (Deepgram, AssemblyAI, Google, vosk) don't, so a
small curated catalog per kind is the fallback. Best-effort: a failed live fetch
falls back to the curated list (or an empty list).
"""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal, InvalidOperation
from typing import cast

import httpx

from loreline.capabilities import filter_models, supports_inline_diarization
from loreline.logging import get_logger
from loreline.models import Interaction, ModelInfo, ModelPrice, ProviderKind

log = get_logger(__name__)

_OPENAI_BASE = "https://api.openai.com/v1"
_OPENROUTER_BASE = "https://openrouter.ai/api/v1"
_LIVE_KINDS = {
    ProviderKind.OPENAI,
    ProviderKind.OPENAI_COMPAT,
    ProviderKind.OPENAI_CHAT,
    ProviderKind.OPENROUTER,
    ProviderKind.OPENROUTER_STT,
}

# OpenRouter serves a different catalogue per interaction, so the right one is
# fetched rather than filtering a mixed list after the fact - its chat and
# transcription model sets are disjoint, and video lives on its own endpoint
# entirely (see loreline.video.client.list_video_models).
_OPENROUTER_KINDS = {ProviderKind.OPENROUTER, ProviderKind.OPENROUTER_STT}
_OPENROUTER_TRANSCRIBE_QUERY = "?output_modalities=transcription"
# Kinds whose base URL is user-supplied (self-hostable), falling back to the kind's own cloud.
_CUSTOM_BASE_KINDS = {ProviderKind.OPENAI_COMPAT, ProviderKind.OPENAI_CHAT}

# Curated model lists for providers with no ``/v1/models`` endpoint to ask.
#
# Each entry is scoped to what *this app's connector for that kind* can actually
# use, which is narrower than the provider's full catalogue. Realtime and batch
# are not interchangeable: Deepgram serves Whisper batch-only while its own
# models stream, OpenAI's gpt-live-transcribe is realtime-only while whisper-1
# is batch-only, and Gemini's live transcription needs the Live API (a
# different transport this app does not implement). Listing a model the
# connector cannot drive just moves the failure to run time.
#
# Checked against each provider's own documentation on 2026-08-31 - see the
# per-kind notes. Re-check when a provider ships a generation; nothing here is
# derived automatically.
_CURATED: dict[ProviderKind, list[str]] = {
    # WebSocket streaming connector, so every entry must stream. Deepgram's
    # hosted Whisper models (whisper-tiny…whisper-large) are deliberately
    # absent: they are pre-recorded only.
    # https://developers.deepgram.com/docs/models-languages-overview
    ProviderKind.DEEPGRAM: [
        "flux-general-en",
        "flux-general-multi",
        "nova-3",
        "nova-3-general",
        "nova-3-medical",
        "nova-2",
        "nova-2-meeting",
        "nova-2-phonecall",
        "nova-2-conversationalai",
        "nova-2-video",
    ],
    # Universal-Streaming v3 `speech_model` values, verbatim from the streaming
    # docs' code samples. universal-3-5-pro is the endpoint's own default.
    # https://www.assemblyai.com/docs/streaming/universal-streaming
    ProviderKind.ASSEMBLYAI: [
        "universal-3-5-pro",
        "universal-streaming-english",
        "universal-streaming-multilingual",
    ],
    # Batch transcription via the Interactions API, which is what this app's
    # connector speaks. gemini-3.5-transcribe-live is intentionally absent: it
    # is reachable only through the Live API's WebSocket transport.
    # https://ai.google.dev/gemini-api/docs/transcribe
    ProviderKind.GEMINI: ["gemini-3.5-transcribe"],
    # Realtime transcription sessions only - the batch-only models
    # (gpt-transcribe, whisper-1, gpt-4o*-transcribe) belong to the
    # OPENAI_COMPAT kind instead. Note whisper-1 and the gpt-4o-*-transcribe
    # family were deprecated on 2026-08-26 (removal 2027-02-26).
    # https://developers.openai.com/api/docs/guides/realtime-transcription
    ProviderKind.OPENAI: ["gpt-live-transcribe", "gpt-realtime-whisper"],
    # Self-hosted: whatever the operator loaded, discoverable only from their
    # own server's /models.
    ProviderKind.VOSK: [],
}

# Kinds whose connector is a streaming transport, so their curated models are
# realtime-capable by construction (that is the filter applied above). Used to
# stamp ModelInfo.realtime for display; kinds absent here leave it None.
_STREAMING_KINDS = {
    ProviderKind.DEEPGRAM,
    ProviderKind.ASSEMBLYAI,
    ProviderKind.OPENAI,
}
_BATCH_KINDS = {ProviderKind.GEMINI}

ClientFactory = Callable[[], httpx.AsyncClient]


async def list_models(
    *,
    kind: ProviderKind,
    base_url: str | None,
    api_key: str | None,
    interaction: Interaction = Interaction.TRANSCRIBE,
    strict_filtering: bool = True,
    client_factory: ClientFactory | None = None,
) -> list[ModelInfo]:
    """Available models for a provider connection (live where possible).

    Scoped to ``interaction``: a transcription picker must never offer a chat
    or image model, which is exactly what an unscoped OpenAI ``/models`` dump
    used to allow. See :mod:`loreline.capabilities` for how that narrowing is
    decided per kind. ``strict_filtering=False`` turns off the guessed part of
    that narrowing (the name markers) while keeping the parts sourced from the
    provider's own metadata.

    Entries carry price and context length when the provider publishes them
    (OpenRouter does; plain OpenAI ``/models`` and the curated lists do not) -
    everything past ``id`` is optional, so a caller can always just read ids.
    """
    if kind in _LIVE_KINDS:
        live = await _fetch_openai_models(kind, base_url, api_key, interaction, client_factory)
        if live:
            return filter_models(live, kind=kind, interaction=interaction, strict=strict_filtering)
    realtime = True if kind in _STREAMING_KINDS else (False if kind in _BATCH_KINDS else None)
    return [
        ModelInfo(
            id=model_id,
            realtime=realtime,
            inline_diarization=supports_inline_diarization(kind, model_id),
        )
        for model_id in _CURATED.get(kind, [])
    ]


async def _fetch_openai_models(
    kind: ProviderKind,
    base_url: str | None,
    api_key: str | None,
    interaction: Interaction,
    client_factory: ClientFactory | None,
) -> list[ModelInfo]:
    default = _OPENROUTER_BASE if kind in _OPENROUTER_KINDS else _OPENAI_BASE
    base = (base_url or default) if kind in _CUSTOM_BASE_KINDS else default
    # OpenRouter answers the transcription catalogue only when asked for it;
    # its unfiltered /models is the (much larger, disjoint) chat catalogue.
    query = ""
    if kind in _OPENROUTER_KINDS and interaction is Interaction.TRANSCRIBE:
        query = _OPENROUTER_TRANSCRIBE_QUERY
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    factory = client_factory or (lambda: httpx.AsyncClient(timeout=15.0))
    try:
        async with factory() as client:
            response = await client.get(f"{base.rstrip('/')}/models{query}", headers=headers)
            response.raise_for_status()
            return _parse_models(response.json())
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("models.fetch.failed", kind=kind.value, error=str(exc))
        return []


# A price quoted per single token, scaled to the per-million figure people read.
_PER_MILLION = 1_000_000


def _usd_per_million(raw: object) -> float | None:
    """ "0.000003" (USD per token) -> 3.0 (USD per million tokens).

    Parsed as ``Decimal`` rather than ``float`` so the scaling is exact: the
    source values run to nine decimal places, where binary floating point
    starts printing 2.9999999999999996 at people. Anything unparseable (a
    missing key, an empty string, a future non-numeric marker) yields None -
    a price we cannot read must render as "unknown", never as free.
    """
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        return float(Decimal(raw) * _PER_MILLION)
    except (InvalidOperation, ValueError):
        return None


def _price(source: dict[str, object], min_prompt_tokens: object = None) -> ModelPrice | None:
    """A ModelPrice from a ``pricing`` (or ``pricing.overrides[]``) object, or
    None when it names neither an input nor an output price."""
    prompt = _usd_per_million(source.get("prompt"))
    completion = _usd_per_million(source.get("completion"))
    if prompt is None and completion is None:
        return None
    return ModelPrice(
        prompt=prompt,
        completion=completion,
        min_prompt_tokens=min_prompt_tokens if isinstance(min_prompt_tokens, int) else None,
    )


def _price_tiers(pricing: dict[str, object]) -> list[ModelPrice]:
    """The ``overrides`` ladder - prices that take over above a prompt-length
    threshold - cheapest threshold first. Empty for the vast majority of
    models, which price one way at any length."""
    raw = pricing.get("overrides")
    if not isinstance(raw, list):
        return []
    tiers: list[ModelPrice] = []
    for override in cast("list[object]", raw):
        if not isinstance(override, dict):
            continue
        entry = cast("dict[str, object]", override)
        tier = _price(entry, entry.get("min_prompt_tokens"))
        if tier is not None:
            tiers.append(tier)
    return sorted(tiers, key=lambda t: t.min_prompt_tokens or 0)


# Parameter names that mean "this model takes a reasoning-effort setting".
# OpenRouter publishes these per model in `supported_parameters`; both spellings
# appear across its catalogue, and either is enough to offer the control.
_REASONING_PARAMS = frozenset({"reasoning", "reasoning_effort"})


def _supports_reasoning(item: dict[str, object]) -> bool:
    params = item.get("supported_parameters")
    if not isinstance(params, list):
        return False
    return any(isinstance(p, str) and p in _REASONING_PARAMS for p in cast("list[object]", params))


def _parse_model(item: dict[str, object]) -> ModelInfo | None:
    model_id = item.get("id")
    if not isinstance(model_id, str):
        return None
    pricing = item.get("pricing")
    context_length = item.get("context_length")
    return ModelInfo(
        id=model_id,
        context_length=context_length if isinstance(context_length, int) else None,
        supports_reasoning=_supports_reasoning(item),
        pricing=_price(cast("dict[str, object]", pricing)) if isinstance(pricing, dict) else None,
        price_tiers=_price_tiers(cast("dict[str, object]", pricing))
        if isinstance(pricing, dict)
        else [],
    )


def _parse_models(payload: object) -> list[ModelInfo]:
    """Map an OpenAI ``/models`` body ({"data": [{"id": …}]}) to sorted models.

    Deliberately tolerant: an endpoint that answers with a bare list of id
    strings, or with rows missing every optional field, still yields a usable
    list - this runs against anything claiming OpenAI compatibility, not just
    the three endpoints tested here.
    """
    data: object = payload
    if isinstance(payload, dict):
        data = cast("dict[str, object]", payload).get("data", [])
    items = cast("list[object]", data) if isinstance(data, list) else []
    by_id: dict[str, ModelInfo] = {}
    for item in items:
        parsed: ModelInfo | None = None
        if isinstance(item, dict):
            parsed = _parse_model(cast("dict[str, object]", item))
        elif isinstance(item, str):
            parsed = ModelInfo(id=item)
        if parsed is not None:
            by_id.setdefault(parsed.id, parsed)
    return sorted(by_id.values(), key=lambda m: m.id)
