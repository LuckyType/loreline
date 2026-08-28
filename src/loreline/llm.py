"""LLM helpers for on-demand session summaries.

Talks to any OpenAI-compatible chat endpoint (OpenAI cloud, Ollama, LM Studio,
vLLM, …) via ``POST /chat/completions``. A single connector covers them all;
only the ``base_url`` and API key differ.
"""

from __future__ import annotations

from collections.abc import Callable
from http import HTTPStatus
from typing import cast

import httpx

from loreline.logging import get_logger
from loreline.models import ProviderConfig

log = get_logger(__name__)

_DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-4o-mini"
_TIMEOUT_S = 120.0

# Built-in summary instructions. The settings UI exposes an editable copy of
# this (kv `action_defaults.summarize_prompt`); a blank stored value falls back
# here, so clearing the field is the reset-to-default gesture.
DEFAULT_SYSTEM_PROMPT = (
    "You are an assistant that writes concise, well-structured summaries of "
    "tabletop RPG session transcripts. Capture the key events, decisions, NPCs, "
    "locations and unresolved threads. Preserve speaker/character names where the "
    "transcript labels them. Write the summary in the same language as the transcript."
)

ClientFactory = Callable[[], httpx.AsyncClient]


class LLMError(Exception):
    """The upstream chat-completions call failed (bad model, bad key, rate limit, …)."""


def _client(
    config: ProviderConfig, api_key: str | None, factory: ClientFactory | None
) -> httpx.AsyncClient:
    if factory is not None:
        return factory()
    base_url = config.base_url or _DEFAULT_BASE_URL
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    return httpx.AsyncClient(base_url=base_url, headers=headers, timeout=_TIMEOUT_S)


async def summarize_transcript(
    *,
    config: ProviderConfig,
    api_key: str | None,
    model: str | None,
    transcript: str,
    system_prompt: str | None = None,
    client_factory: ClientFactory | None = None,
) -> str:
    """Summarize ``transcript`` via the provider's chat-completions endpoint.

    ``system_prompt`` overrides the built-in summary instructions; blank or
    None falls back to :data:`DEFAULT_SYSTEM_PROMPT`.

    Raises ``LLMError`` (never a bare ``httpx`` exception) on any upstream
    failure, carrying the provider's own error message when it has one - an
    invalid model id, a bad key, a rate limit, or a plain connection failure
    should all read as *why it failed*, not surface as an opaque 500.
    """
    chosen_model = model or config.model or DEFAULT_MODEL
    instructions = (system_prompt or "").strip() or DEFAULT_SYSTEM_PROMPT
    payload: dict[str, object] = {
        "model": chosen_model,
        "messages": [
            {"role": "system", "content": instructions},
            {"role": "user", "content": f"Summarize this session transcript:\n\n{transcript}"},
        ],
        "temperature": 0.3,
    }
    client = _client(config, api_key, client_factory)
    try:
        response = await _post_completion(client, payload)
        if _rejects_temperature(response):
            # Reasoning-class models (OpenAI's o-series, gpt-5+) fix temperature
            # at their default and reject any explicit value - retry once
            # without it rather than surfacing a summarize failure for
            # something the caller never controlled to begin with.
            del payload["temperature"]
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


def _rejects_temperature(response: httpx.Response) -> bool:
    """True if the model rejected ``temperature`` specifically (not some other 400)."""
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
    return cast("dict[str, object]", error).get("param") == "temperature"


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
