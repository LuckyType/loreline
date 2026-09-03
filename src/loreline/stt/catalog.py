"""List the models a provider offers, for the on-demand model pickers.

OpenAI-compatible endpoints (OpenAI cloud, OpenRouter, Speaches, Ollama, LM Studio,
…) expose ``GET /v1/models`` in one shape, which is fetched live from the
kind's ``catalog`` surface in capabilities.yaml; the other kinds' pickers are
the curated lists. (Deepgram and Gemini publish catalogues too, and the
staleness check reads them, but not in a shape this app offers models from.)
Best-effort: a failed live fetch falls back to the curated list (or an empty
list).

This module decides *where* a list comes from, never *which models are in it*.
That second question has exactly one answer, capabilities.yaml, read here through
:func:`loreline.capabilities.curated_models`. It used to have two: a ``_CURATED``
table lived here as well, and the pair had already drifted - it offered six
task-tuned Deepgram variants the yaml deliberately does not list, and it kept
gemini-3.5-transcribe-live out of every picker for a while after the yaml unhid
it, because nobody remembered the second gate.
"""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal, InvalidOperation
from typing import cast

import httpx

from loreline.capabilities import (
    Endpoint,
    catalog_for,
    curated_models,
    filter_models,
    is_realtime_model,
    supports_inline_diarization,
)
from loreline.logging import get_logger
from loreline.models import Interaction, ModelInfo, ModelPrice, ProviderKind

log = get_logger(__name__)

# Kinds whose catalogue body is OpenAI's ``{"data": [{"id": ...}]}`` shape,
# which is the one :func:`_parse_models` reads. Wire behaviour, not an
# address: where each catalogue lives, and how it is authenticated, is the
# kind's ``catalog`` surface in capabilities.yaml.
_OPENAI_SHAPED = frozenset(
    {ProviderKind.OPENAI, ProviderKind.OPENAI_COMPAT, ProviderKind.OPENROUTER}
)

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
    # Where the vendor publishes its list for this interaction: OpenRouter
    # splits its catalogue three ways (its chat and transcription sets are
    # disjoint, and video lives on its own endpoint entirely), OpenAI serves
    # one list for everything, the self-hosted server's list sits beside
    # whatever base the row names. None means nothing to fetch.
    catalogue = catalog_for(kind, interaction, base_url=base_url)
    if interaction is Interaction.VIDEO:
        # Bare ids for a picker; the rich per-model parameters come from
        # loreline.video.client.list_video_models, which the generate dialog
        # reads directly. No curated fallback: a kind with no video catalogue
        # generates no video.
        if catalogue is None:
            return []
        return await _fetch(catalogue, api_key, client_factory, event="models.video_fetch.failed")
    if kind in _OPENAI_SHAPED and catalogue is not None:
        live = await _fetch(catalogue, api_key, client_factory, event="models.fetch.failed")
        if live:
            narrowed = filter_models(
                live, kind=kind, interaction=interaction, strict=strict_filtering
            )
            return _annotate(narrowed, kind=kind, interaction=interaction)
    # The curated fallback, scoped to this interaction: Gemini's catalogue is
    # never fetched live, so an unscoped list here would offer a transcription
    # model to summarize with.
    curated = [ModelInfo(id=model_id) for model_id in curated_models(kind, interaction)]
    return _annotate(curated, kind=kind, interaction=interaction)


def _annotate(
    models: list[ModelInfo], *, kind: ProviderKind, interaction: Interaction
) -> list[ModelInfo]:
    """Stamp the capability flags the pickers read onto transcription entries.

    A live ``/models`` row says nothing about transport or diarization, so both
    come from the capability tables, per model rather than per kind: OpenAI's
    list mixes realtime and batch models, and OpenRouter's carries one model
    (grok-stt-1.0) whose inline diarization would otherwise go unadvertised.
    Other interactions have no such flags to stamp.
    """
    if interaction is not Interaction.TRANSCRIBE:
        return models
    return [
        model.model_copy(
            update={
                "realtime": is_realtime_model(kind, model.id),
                "inline_diarization": supports_inline_diarization(kind, model.id),
            }
        )
        for model in models
    ]


async def _fetch(
    catalogue: Endpoint,
    api_key: str | None,
    client_factory: ClientFactory | None,
    *,
    event: str,
) -> list[ModelInfo]:
    """One catalogue, read fail soft: a vendor that is down yields nothing."""
    factory = client_factory or (lambda: httpx.AsyncClient(timeout=15.0))
    try:
        async with factory() as client:
            response = await client.get(catalogue.url, headers=catalogue.request_headers(api_key))
            response.raise_for_status()
            return _parse_models(response.json())
    except (httpx.HTTPError, ValueError) as exc:
        log.warning(event, url=catalogue.url, error=str(exc))
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


def _is_transcription_model(item: dict[str, object]) -> bool:
    architecture = item.get("architecture")
    if not isinstance(architecture, dict):
        return False
    outputs = cast("dict[str, object]", architecture).get("output_modalities")
    if not isinstance(outputs, list):
        return False
    return "transcription" in cast("list[object]", outputs)


def _parse_model(item: dict[str, object]) -> ModelInfo | None:
    model_id = item.get("id")
    if not isinstance(model_id, str):
        return None
    pricing = item.get("pricing")
    # Transcription models are priced per unit of *audio*, not per token, and
    # OpenRouter does not say which unit in the catalogue: measured against the
    # live API, deepgram/nova-3's "0.0043" bills per minute while
    # nvidia/nemotron-3.5-asr's "0.00000333" bills per second. Nothing in the
    # payload distinguishes the two, so any figure shown here would be wrong by
    # a factor of 60 for some models. Reporting no price is the honest option;
    # OpenRouter's own model page is the place to check an audio rate.
    if _is_transcription_model(item):
        pricing = None
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
