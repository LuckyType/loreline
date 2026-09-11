"""LLM helpers for on-demand session summaries.

Talks to any OpenAI-compatible chat endpoint (OpenAI cloud, OpenRouter, Ollama,
LM Studio, vLLM, …) via ``POST /chat/completions``. A single connector covers
them all; what differs per kind (the base, the attribution headers, the probe
path) is the kind's ``summarize`` surface in capabilities.yaml, and the model
comes from the request (see :func:`summarize_transcript`).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from http import HTTPStatus
from typing import cast

import httpx
from openrouter.components.chatrequest import ChatRequestReasoning
from openrouter.components.providerpreferences import ProviderPreferences
from pydantic import ValidationError

from loreline.capabilities import Endpoint, surface_for
from loreline.health import error_body, error_detail
from loreline.logging import get_logger
from loreline.models import (
    Interaction,
    OpenRouterRouting,
    ProviderConfig,
    ProviderKind,
    SessionExtraction,
)

log = get_logger(__name__)

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

# What a player wants to be told a week later, which is not what a GM wants in
# a summary: no meta commentary about the recording, no headings listing "NPCs"
# and "Locations", just what the party did and what is still open, in the
# language they played in. Overridable globally (kv `action_defaults.recap_prompt`)
# and per campaign (`campaigns.recap_prompt`), with the same blank-means-default
# rule the summary prompt has.
DEFAULT_RECAP_PROMPT = (
    "You write the recap of a tabletop RPG session for the players who were "
    "there. Write in the past tense, in the same language as the transcript, "
    "as flowing prose rather than a list. Cover what the party did, what they "
    "learned, who they met and what is still unresolved. Use the names the "
    "transcript uses. Do not mention the transcript, the recording or yourself, "
    "and do not add advice or speculation. Aim for 200 to 400 words."
)

# The extraction prompt asks for JSON and nothing else. Models answer it with a
# code fence anyway, which is why the parser strips one (see
# :func:`parse_extraction`) instead of the prompt insisting harder.
EXTRACTION_PROMPT = (
    "You extract the named things from a tabletop RPG session transcript. "
    "Answer with a single JSON object and nothing else: no prose, no code "
    "fence, no explanation. The object has exactly these keys:\n"
    '{"characters": [{"name": str, "kind": "pc" | "npc", "notes": str}], '
    '"places": [{"name": str, "notes": str}], '
    '"items": [{"name": str, "notes": str}], '
    '"factions": [{"name": str, "notes": str}], '
    '"quests": [{"title": str, "status": str, "notes": str}], '
    '"decisions": [str]}\n'
    "Use the spelling the transcript uses. Keep each note to one sentence, in "
    "the transcript's language. A key with nothing to report is an empty list."
)

# The one for the start of the next session: shorter than a recap, and about
# the campaign rather than about one evening.
DEFAULT_PREVIOUSLY_ON_PROMPT = (
    'You write the "previously on" that opens the next session of a tabletop '
    "RPG campaign, from the recaps of the sessions before it. Write in the past "
    "tense, in the same language as the recaps, as a single short paragraph of "
    "at most 150 words that a GM can read aloud at the table. Carry only what "
    "the players need to pick the story back up: where they are, what they were "
    "doing and what is still open. Do not mention the recaps or yourself."
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
    instruction: str = "Summarize this session transcript:",
    cast_line: str = "",
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

    ``instruction`` is the line the transcript is handed over with. It exists
    because this function is no longer only the summarizer: a recap and an
    extraction are the same call with different instructions, and a body that
    opens "Summarize this session transcript" while the system prompt asks for
    JSON is a contradiction the model has to resolve on its own.

    ``cast_line`` is the one line naming the campaign's player characters (see
    :func:`loreline.web.generation.describe_cast`), appended to whichever
    instructions won rather than replacing them. Appended here and not by the
    caller because the fallback above it is here: a route that joined the two
    itself would have to restate "blank means the built-in prompt" to avoid
    sending the cast line as the entire system prompt. Blank adds nothing, so a
    campaign with no cast produces the request it always produced.

    ``reasoning_effort`` is sent only for a model that advertises support (the
    caller checks; see ModelInfo.supports_reasoning) and is dropped on retry if
    the endpoint rejects it anyway.

    Raises ``LLMError`` (never a bare ``httpx`` exception) on any upstream
    failure, carrying the provider's own error message when it has one - an
    invalid model id, a bad key, a rate limit, or a plain connection failure
    should all read as *why it failed*, not surface as an opaque 500.
    """
    instructions = (system_prompt or "").strip() or DEFAULT_SYSTEM_PROMPT
    if cast_line.strip():
        instructions = f"{instructions}\n\n{cast_line.strip()}"
    payload: dict[str, object] = {
        "model": model,
        "messages": [
            {"role": "system", "content": instructions},
            {"role": "user", "content": f"{instruction}\n\n{transcript}"},
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


_EXTRACTION_INSTRUCTION = "Extract the named things from this session transcript:"


def parse_extraction(answer: str) -> SessionExtraction:
    """Read a model's answer as a :class:`SessionExtraction`, leniently.

    "JSON only" is an instruction, not a guarantee. The same prompt comes back
    bare from one model, inside a ```json fence from the next, and with a
    sentence of throat-clearing in front of the object from a third, and all
    three of those are answers that contain everything asked for. So the fence
    is stripped and the outermost braces are what is parsed, before pydantic
    gets an opinion.

    Raises ``ValueError`` for an answer no object could be found in, or one
    whose object does not fit the schema. The caller retries once with the
    message appended (see :func:`extract_entities`) - a model told what it got
    wrong usually fixes it, and a second failure is worth reporting rather than
    grinding on.
    """
    text = answer.strip()
    if text.startswith("```"):
        # ```json ... ``` - drop the first line and whatever closing fence is
        # left, rather than regexing for the language tag.
        text = text.split("\n", 1)[-1] if "\n" in text else ""
        if text.rstrip().endswith("```"):
            text = text.rstrip()[: -len("```")]
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("the answer contained no JSON object")
    try:
        payload = json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise ValueError(f"the answer was not valid JSON: {exc}") from exc
    try:
        return SessionExtraction.model_validate(payload)
    except ValidationError as exc:
        raise ValueError(f"the JSON did not match the schema: {exc}") from exc


async def extract_entities(
    *,
    config: ProviderConfig,
    api_key: str | None,
    model: str,
    transcript: str,
    cast_line: str = "",
    reasoning_effort: str | None = None,
    client_factory: ClientFactory | None = None,
) -> SessionExtraction:
    """Extract the session's named things as structured data, retrying once.

    The retry is the whole design. A model that answers with prose around the
    object, a trailing comma or ``"kind": "Player"`` has understood the task
    and failed the format, and telling it exactly what was wrong fixes it far
    more often than not - while failing outright would charge the GM for a run
    that produced nothing readable. Two attempts and no more: a model that
    cannot produce the schema twice is not going to on the third try, and the
    error names what it did instead.

    ``cast_line`` is passed through to both attempts. This is the generation it
    helps most: ``kind`` is ``pc`` or ``npc`` and nothing in a transcript says
    which, so without it the model sorts the party by how much they talked.
    """
    attempt = await summarize_transcript(
        config=config,
        api_key=api_key,
        model=model,
        transcript=transcript,
        system_prompt=EXTRACTION_PROMPT,
        instruction=_EXTRACTION_INSTRUCTION,
        cast_line=cast_line,
        reasoning_effort=reasoning_effort,
        client_factory=client_factory,
    )
    try:
        return parse_extraction(attempt)
    except ValueError as exc:
        # Bound outside the handler: Python clears the name at the end of the
        # except block, and the retry below is what needs the sentence.
        complaint = str(exc)
    log.info("llm.extract.retry", reason=complaint)
    retry = await summarize_transcript(
        config=config,
        api_key=api_key,
        model=model,
        transcript=transcript,
        system_prompt=EXTRACTION_PROMPT,
        instruction=(
            f"{_EXTRACTION_INSTRUCTION}\n\nYour previous answer could not be read: "
            f"{complaint}\nAnswer again with the JSON object alone."
        ),
        cast_line=cast_line,
        reasoning_effort=reasoning_effort,
        client_factory=client_factory,
    )
    try:
        return parse_extraction(retry)
    except ValueError as second:
        raise LLMError(f"the model did not answer with usable JSON: {second}") from second
