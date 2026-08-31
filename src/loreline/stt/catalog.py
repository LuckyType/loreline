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

from loreline.logging import get_logger
from loreline.models import ModelInfo, ModelPrice, ProviderKind

log = get_logger(__name__)

_OPENAI_BASE = "https://api.openai.com/v1"
_OPENROUTER_BASE = "https://openrouter.ai/api/v1"
_LIVE_KINDS = {
    ProviderKind.OPENAI,
    ProviderKind.OPENAI_COMPAT,
    ProviderKind.OPENAI_CHAT,
    ProviderKind.OPENROUTER,
}
# Kinds whose base URL is user-supplied (self-hostable), falling back to the kind's own cloud.
_CUSTOM_BASE_KINDS = {ProviderKind.OPENAI_COMPAT, ProviderKind.OPENAI_CHAT}

# Hardcoded fallbacks for providers without a /v1/models endpoint.
_CURATED: dict[ProviderKind, list[str]] = {
    ProviderKind.DEEPGRAM: ["nova-3", "nova-2", "nova-2-general", "nova-2-meeting", "enhanced"],
    ProviderKind.ASSEMBLYAI: ["universal"],
    ProviderKind.GOOGLE: ["chirp_2", "chirp", "latest_long", "latest_short", "telephony"],
    ProviderKind.VOSK: [],
}

ClientFactory = Callable[[], httpx.AsyncClient]


async def list_models(
    *,
    kind: ProviderKind,
    base_url: str | None,
    api_key: str | None,
    client_factory: ClientFactory | None = None,
) -> list[ModelInfo]:
    """Available models for a provider connection (live where possible).

    Entries carry price and context length when the provider publishes them
    (OpenRouter does; plain OpenAI ``/models`` and the curated lists do not) -
    everything past ``id`` is optional, so a caller can always just read ids.
    """
    if kind in _LIVE_KINDS:
        live = await _fetch_openai_models(kind, base_url, api_key, client_factory)
        if live:
            return live
    return [ModelInfo(id=model_id) for model_id in _CURATED.get(kind, [])]


async def _fetch_openai_models(
    kind: ProviderKind,
    base_url: str | None,
    api_key: str | None,
    client_factory: ClientFactory | None,
) -> list[ModelInfo]:
    default = _OPENROUTER_BASE if kind is ProviderKind.OPENROUTER else _OPENAI_BASE
    base = (base_url or default) if kind in _CUSTOM_BASE_KINDS else default
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    factory = client_factory or (lambda: httpx.AsyncClient(timeout=15.0))
    try:
        async with factory() as client:
            response = await client.get(f"{base.rstrip('/')}/models", headers=headers)
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


def _parse_model(item: dict[str, object]) -> ModelInfo | None:
    model_id = item.get("id")
    if not isinstance(model_id, str):
        return None
    pricing = item.get("pricing")
    context_length = item.get("context_length")
    return ModelInfo(
        id=model_id,
        context_length=context_length if isinstance(context_length, int) else None,
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
