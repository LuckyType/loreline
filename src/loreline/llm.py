"""LLM helpers for on-demand session summaries.

Talks to any OpenAI-compatible chat endpoint (OpenAI cloud, OpenRouter, Ollama,
LM Studio, vLLM, …) via ``POST /chat/completions``. A single connector covers
them all; only the ``base_url``, API key and default model differ per kind.
"""

from __future__ import annotations

from collections.abc import Callable
from http import HTTPStatus
from typing import cast

import httpx

from loreline.capabilities import kinds_for
from loreline.logging import get_logger
from loreline.models import Interaction, OpenRouterRouting, ProviderConfig, ProviderKind

log = get_logger(__name__)

_DEFAULT_BASE_URL = "https://api.openai.com/v1"
_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_MODEL = "gpt-4o-mini"
_OPENROUTER_DEFAULT_MODEL = "openai/gpt-4o-mini"
_TIMEOUT_S = 120.0

# Provider kinds that summarize, i.e. speak chat-completions rather than STT.
# Derived from the one capability table rather than re-listed here - see
# loreline.capabilities.INTERACTIONS_BY_KIND.
LLM_KINDS = kinds_for(Interaction.SUMMARIZE)

# OpenRouter credits the calling app on its public leaderboards through these
# two optional headers; they are meaningless to every other endpoint, so they
# only go out for that kind.
_OPENROUTER_HEADERS = {
    "HTTP-Referer": "https://github.com/LuckyType/loreline",
    "X-Title": "Loreline",
}

# Built-in summary instructions. The settings UI exposes an editable copy of
# this (kv `action_defaults.summarize_prompt`); a blank stored value falls back
# here, so clearing the field is the reset-to-default gesture.
DEFAULT_SYSTEM_PROMPT = (
    "You are an assistant that writes concise, well-structured summaries of "
    "tabletop RPG session transcripts. Capture the key events, decisions, NPCs, "
    "locations and unresolved threads. Preserve speaker/character names where the "
    "transcript labels them. Write the summary in the same language as the transcript."
)

# Reasoning-effort levels, in the order the pickers show them. Sourced from
# OpenRouter's reasoning docs, which accept exactly these; OpenAI's Responses
# API uses the same vocabulary. "none" disables reasoning for a model that
# would otherwise do it by default.
REASONING_EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max")

ClientFactory = Callable[[], httpx.AsyncClient]


class LLMError(Exception):
    """The upstream chat-completions call failed (bad model, bad key, rate limit, …)."""


def default_base_url(kind: ProviderKind) -> str:
    """Endpoint an LLM kind talks to when its config names no ``base_url``."""
    return _OPENROUTER_BASE_URL if kind is ProviderKind.OPENROUTER else _DEFAULT_BASE_URL


def default_model(kind: ProviderKind) -> str:
    """Model an LLM kind summarizes with when neither call nor config picks one.

    OpenRouter addresses models as ``vendor/model``, so a bare OpenAI model name
    is not a valid id there.
    """
    return _OPENROUTER_DEFAULT_MODEL if kind is ProviderKind.OPENROUTER else DEFAULT_MODEL


def routing_payload(config: ProviderConfig) -> dict[str, object] | None:
    """OpenRouter's ``provider`` routing object for this config, or None.

    Only fields the GM actually moved off their default are emitted, and the
    whole object is dropped when none were: an empty ``provider`` object would
    pin today's OpenRouter defaults into every request, which is precisely
    what a config that says nothing should *not* do.

    Returns None for every non-OpenRouter kind - ``provider`` is an OpenRouter
    body extension, and a plain OpenAI-compatible endpoint has no idea what to
    do with it.
    """
    if config.kind is not ProviderKind.OPENROUTER or config.routing is None:
        return None
    routing: OpenRouterRouting = config.routing
    payload: dict[str, object] = {}
    if routing.sort is not None:
        payload["sort"] = routing.sort
    if routing.data_collection == "deny":
        payload["data_collection"] = "deny"
    if routing.zdr:
        payload["zdr"] = True
    return payload or None


def apply_reasoning_effort(
    payload: dict[str, object], kind: ProviderKind, effort: str | None
) -> None:
    """Add the reasoning-effort request field, in the shape this kind expects.

    The two gateways spell it differently, and sending the wrong spelling is
    silently ignored rather than erroring - which would look exactly like the
    setting not working:

    * OpenRouter takes a nested object, ``{"reasoning": {"effort": "high"}}``.
    * Everything else OpenAI-compatible takes top-level ``reasoning_effort``,
      the convention vLLM/LM Studio and friends implement.

    A server that rejects the field outright is handled the same way an
    explicit ``temperature`` already is - see ``_rejects_parameter`` and the
    retry in :func:`summarize_transcript`.
    """
    if not effort:
        return
    if kind is ProviderKind.OPENROUTER:
        payload["reasoning"] = {"effort": effort}
    else:
        payload["reasoning_effort"] = effort


def _client(
    config: ProviderConfig, api_key: str | None, factory: ClientFactory | None
) -> httpx.AsyncClient:
    if factory is not None:
        return factory()
    base_url = config.base_url or default_base_url(config.kind)
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    if config.kind is ProviderKind.OPENROUTER:
        headers.update(_OPENROUTER_HEADERS)
    return httpx.AsyncClient(base_url=base_url, headers=headers, timeout=_TIMEOUT_S)


async def summarize_transcript(
    *,
    config: ProviderConfig,
    api_key: str | None,
    model: str | None,
    transcript: str,
    system_prompt: str | None = None,
    reasoning_effort: str | None = None,
    client_factory: ClientFactory | None = None,
) -> str:
    """Summarize ``transcript`` via the provider's chat-completions endpoint.

    ``system_prompt`` overrides the built-in summary instructions; blank or
    None falls back to :data:`DEFAULT_SYSTEM_PROMPT`.

    ``reasoning_effort`` is sent only for a model that advertises support (the
    caller checks; see ModelInfo.supports_reasoning) and is dropped on retry if
    the endpoint rejects it anyway.

    Raises ``LLMError`` (never a bare ``httpx`` exception) on any upstream
    failure, carrying the provider's own error message when it has one - an
    invalid model id, a bad key, a rate limit, or a plain connection failure
    should all read as *why it failed*, not surface as an opaque 500.
    """
    chosen_model = model or config.model or default_model(config.kind)
    instructions = (system_prompt or "").strip() or DEFAULT_SYSTEM_PROMPT
    payload: dict[str, object] = {
        "model": chosen_model,
        "messages": [
            {"role": "system", "content": instructions},
            {"role": "user", "content": f"Summarize this session transcript:\n\n{transcript}"},
        ],
        "temperature": 0.3,
    }
    apply_reasoning_effort(payload, config.kind, reasoning_effort)
    routing = routing_payload(config)
    if routing is not None:
        payload["provider"] = routing
    client = _client(config, api_key, client_factory)
    try:
        response = await _post_completion(client, payload)
        if _rejects_parameter(response, "temperature"):
            # Reasoning-class models (OpenAI's o-series, gpt-5+) fix temperature
            # at their default and reject any explicit value - retry once
            # without it rather than surfacing a summarize failure for
            # something the caller never controlled to begin with.
            del payload["temperature"]
            response = await _post_completion(client, payload)
        for field in ("reasoning", "reasoning_effort"):
            # Same treatment for an endpoint that rejects the reasoning field:
            # the effort is a preference, not worth failing the summary over.
            if field in payload and _rejects_parameter(response, field):
                del payload[field]
                response = await _post_completion(client, payload)
        if response.status_code >= HTTPStatus.BAD_REQUEST:
            raise LLMError(_error_detail(response))
        return _parse_completion(response.json())
    finally:
        await client.aclose()


async def _post_completion(client: httpx.AsyncClient, payload: dict[str, object]) -> httpx.Response:
    try:
        return await client.post("/chat/completions", json=payload)
    except httpx.HTTPError as exc:
        raise LLMError(f"could not reach {client.base_url}: {exc}") from exc


def _rejects_parameter(response: httpx.Response, name: str) -> bool:
    """True if the model rejected this specific parameter (not some other 400)."""
    if response.status_code != HTTPStatus.BAD_REQUEST:
        return False
    try:
        payload = response.json()
    except ValueError:
        return False
    if not isinstance(payload, dict):
        return False
    error = cast("dict[str, object]", payload).get("error")
    if not isinstance(error, dict):
        return False
    return cast("dict[str, object]", error).get("param") == name


def _error_detail(response: httpx.Response) -> str:
    """Pull the message out of an OpenAI-compatible error body, else fall back."""
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        error = cast("dict[str, object]", payload).get("error")
        if isinstance(error, dict):
            message = cast("dict[str, object]", error).get("message")
            if isinstance(message, str) and message:
                return message
        elif isinstance(error, str) and error:
            return error
    return f"{response.status_code} {response.reason_phrase}"


async def chat_health(
    *,
    config: ProviderConfig,
    api_key: str | None,
    client_factory: ClientFactory | None = None,
) -> bool:
    """Reachability probe for an LLM provider (``GET /models``)."""
    client = _client(config, api_key, client_factory)
    try:
        response = await client.get("/models")
        return response.status_code < HTTPStatus.INTERNAL_SERVER_ERROR
    except httpx.HTTPError:
        return False
    finally:
        await client.aclose()


def _parse_completion(payload: object) -> str:
    """Extract ``choices[0].message.content`` from a chat-completions body."""
    if isinstance(payload, dict):
        choices = cast("dict[str, object]", payload).get("choices")
        if isinstance(choices, list) and choices:
            first = cast("list[object]", choices)[0]
            if isinstance(first, dict):
                message = cast("dict[str, object]", first).get("message")
                if isinstance(message, dict):
                    content = cast("dict[str, object]", message).get("content")
                    if isinstance(content, str):
                        return content.strip()
    log.warning("llm.summary.unexpected_payload")
    return ""
