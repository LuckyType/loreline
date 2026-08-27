"""List the models a provider offers, for the on-demand model pickers.

OpenAI-compatible endpoints (OpenAI cloud, Speaches, Ollama, LM Studio, …) expose
``GET /v1/models``; the others (Deepgram, AssemblyAI, Google, vosk) don't, so a
small curated catalog per kind is the fallback. Best-effort: a failed live fetch
falls back to the curated list (or an empty list).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import cast

import httpx

from loreline.logging import get_logger
from loreline.models import ProviderKind

log = get_logger(__name__)

_OPENAI_BASE = "https://api.openai.com/v1"
_LIVE_KINDS = {ProviderKind.OPENAI, ProviderKind.OPENAI_COMPAT, ProviderKind.OPENAI_CHAT}
# Kinds whose base URL is user-supplied (self-hostable), falling back to OpenAI cloud.
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
) -> list[str]:
    """Available model ids for a provider connection (live where possible)."""
    if kind in _LIVE_KINDS:
        live = await _fetch_openai_models(kind, base_url, api_key, client_factory)
        if live:
            return live
    return list(_CURATED.get(kind, []))


async def _fetch_openai_models(
    kind: ProviderKind,
    base_url: str | None,
    api_key: str | None,
    client_factory: ClientFactory | None,
) -> list[str]:
    base = (base_url or _OPENAI_BASE) if kind in _CUSTOM_BASE_KINDS else _OPENAI_BASE
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    factory = client_factory or (lambda: httpx.AsyncClient(timeout=15.0))
    try:
        async with factory() as client:
            response = await client.get(f"{base.rstrip('/')}/models", headers=headers)
            response.raise_for_status()
            return _parse_model_ids(response.json())
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("models.fetch.failed", kind=kind.value, error=str(exc))
        return []


def _parse_model_ids(payload: object) -> list[str]:
    """Map an OpenAI ``/models`` body ({"data": [{"id": …}]}) to sorted ids."""
    data: object = payload
    if isinstance(payload, dict):
        data = cast("dict[str, object]", payload).get("data", [])
    items = cast("list[object]", data) if isinstance(data, list) else []
    ids: list[str] = []
    for item in items:
        if isinstance(item, dict):
            mid = cast("dict[str, object]", item).get("id")
            if isinstance(mid, str):
                ids.append(mid)
        elif isinstance(item, str):
            ids.append(item)
    return sorted(set(ids))
