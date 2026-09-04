"""LLM helpers for on-demand session summaries.

Talks to any OpenAI-compatible chat endpoint (OpenAI cloud, OpenRouter, Ollama,
LM Studio, vLLM, …) via ``POST /chat/completions``. A single connector covers
them all; what differs per kind (the base, the attribution headers, the probe
path) is the kind's ``summarize`` surface in capabilities.yaml, and the model
comes from the request (see :func:`summarize_transcript`).
"""

from __future__ import annotations

from collections.abc import Callable
from http import HTTPStatus
from typing import cast

import httpx
from openrouter.components.chatrequest import ChatRequestReasoning
from openrouter.components.providerpreferences import ProviderPreferences

from loreline.capabilities import Endpoint, kinds_for, surface_for
from loreline.health import error_body, error_detail
from loreline.logging import get_logger
from loreline.models import Interaction, OpenRouterRouting, ProviderConfig, ProviderKind

log = get_logger(__name__)

_TIMEOUT_S = 120.0
# Provider kinds that summarize, i.e. speak chat-completions rather than STT.
# Derived from the one capability table rather than re-listed here - see
# loreline.capabilities.kinds_for.
LLM_KINDS = kinds_for(Interaction.SUMMARIZE)

# Built-in summary instructions. The settings UI exposes an editable copy of
# this (kv `action_defaults.summarize_prompt`); a blank stored value falls back
# here, so clearing the field is the reset-to-default gesture.
DEFAULT_SYSTEM_PROMPT = (
    "You are an assistant that writes concise, well-structured summaries of "
    "tabletop RPG session transcripts. Capture the key events, decisions, NPCs, "
    "locations and unresolved threads. Preserve speaker/character names where the "
    "transcript labels them. Write the summary in the same language as the transcript."
)

# Reasoning-effort levels, in the order the pickers show them. Not hand-written:
# read off the OpenRouter SDK's generated request model, so the set tracks their
# OpenAPI spec instead of drifting from it. "none" disables reasoning for a
# model that would otherwise do it by default.
REASONING_EFFORTS: tuple[str, ...] = (
    "none",
    "minimal",
    "low",
    "medium",
    "high",
    "xhigh",
    "max",
)

ClientFactory = Callable[[], httpx.AsyncClient]


class LLMError(Exception):
    """The upstream chat-completions call failed (bad model, bad key, rate limit, …)."""


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
    # Assembled through the SDK's generated ProviderPreferences so the field
    # names and value literals are checked against OpenRouter's spec rather
    # than spelled out here. Only fields the GM moved off their default are
    # set, and `exclude_unset` keeps the rest out of the body.
    preferences = ProviderPreferences()
    if routing.sort is not None:
        preferences.sort = routing.sort  # pyright: ignore[reportAttributeAccessIssue]
    if routing.data_collection == "deny":
        preferences.data_collection = "deny"
    if routing.zdr:
        preferences.zdr = True
    payload = preferences.model_dump(exclude_unset=True, exclude_none=True)
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
        # Built through the SDK's generated model rather than a hand-written
        # dict: the nested-vs-flat distinction below is exactly the kind of
        # shape mistake an endpoint accepts silently, and this makes it a type
        # error instead. `exclude_unset` keeps the body to what we actually set.
        reasoning = ChatRequestReasoning(effort=effort)  # pyright: ignore[reportArgumentType]
        payload["reasoning"] = reasoning.model_dump(exclude_unset=True, exclude_none=True)
    else:
        payload["reasoning_effort"] = effort


def _chat_surface(config: ProviderConfig) -> Endpoint:
    """This row's chat endpoint: the kind's summarize surface, its base applied.

    Gemini is the reason this is per surface and not per vendor: its chat
    lives on Google's OpenAI-compatible shim (".../v1beta/openai", Bearer
    auth), a sibling of the native base the transcription connector posts to,
    and neither answers the other's requests.
    """
    return surface_for(config, Interaction.SUMMARIZE)


def _client(
    endpoint: Endpoint, api_key: str | None, factory: ClientFactory | None
) -> httpx.AsyncClient:
    if factory is not None:
        return factory()
    return httpx.AsyncClient(
        base_url=endpoint.url, headers=endpoint.request_headers(api_key), timeout=_TIMEOUT_S
    )


async def summarize_transcript(
    *,
    config: ProviderConfig,
    api_key: str | None,
    model: str,
    transcript: str,
    system_prompt: str | None = None,
    reasoning_effort: str | None = None,
    client_factory: ClientFactory | None = None,
) -> str:
    """Summarize ``transcript`` via the provider's chat-completions endpoint.

    ``model`` is required: the summarize route makes the caller choose one, so
    there is no second place that decides what ran. There used to be a chain
    here (request, else the provider row's model, else a constant), duplicated
    verbatim in the route so it could record the choice - two copies that could
    disagree about what the request actually used.

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
    instructions = (system_prompt or "").strip() or DEFAULT_SYSTEM_PROMPT
    payload: dict[str, object] = {
        "model": model,
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
    try:
        endpoint = _chat_surface(config)
    except ValueError as exc:
        # A self-hosted row with no base URL: nowhere to post, and the message
        # says what to configure.
        raise LLMError(str(exc)) from exc
    client = _client(endpoint, api_key, client_factory)
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
            raise LLMError(error_detail(response))
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
    payload = error_body(response)
    if payload is None:
        return False
    error = payload.get("error")
    if not isinstance(error, dict):
        return False
    return cast("dict[str, object]", error).get("param") == name


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
