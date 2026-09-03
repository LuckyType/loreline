"""Tests for the OpenAI-compatible chat connector used for session summaries."""

from __future__ import annotations

import json
import re

import httpx
import pytest

from loreline.health import HealthStatus
from loreline.llm import (
    DEFAULT_SYSTEM_PROMPT,
    LLMError,
    chat_health,
    routing_payload,
    summarize_transcript,
)
from loreline.models import OpenRouterRouting, Protocol, ProviderConfig, ProviderKind

_BASE_URL = "http://llm:1234/v1"
# The injected test client keeps the base URL's path, so a probe of "/models"
# lands on "/v1/models".
_PATH_PREFIX = "/v1"


def _config() -> ProviderConfig:
    return ProviderConfig(
        id="l1",
        name="LLM",
        kind=ProviderKind.OPENAI_COMPAT,
        protocol=Protocol.HTTP_BATCH,
        base_url=_BASE_URL,
    )


def _openrouter_config() -> ProviderConfig:
    """OpenRouter as the wizard stores it - the kind carries the endpoint, so
    there is no ``base_url`` of its own."""
    return ProviderConfig(
        id="l2",
        name="OpenRouter",
        kind=ProviderKind.OPENROUTER,
        protocol=Protocol.HTTP_BATCH,
    )


def _gemini_config() -> ProviderConfig:
    """Gemini as the wizard stores it. No ``base_url``: the settings form shows
    that field only for a kind whose capabilities entry records none, so a
    cloud kind carries whatever llm.py defaults to."""
    return ProviderConfig(
        id="l3",
        name="Gemini",
        kind=ProviderKind.GEMINI,
        protocol=Protocol.HTTP_BATCH,
    )


def _client(transport: httpx.MockTransport) -> httpx.AsyncClient:
    # The injected client owns its base_url, just like the real one in llm.py.
    return httpx.AsyncClient(transport=transport, base_url=_BASE_URL)


async def test_summarize_transcript_posts_chat_completions() -> None:
    captured: dict[str, object] = {}

    def handle(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        body = json.loads(request.content)
        captured["model"] = body["model"]
        captured["messages"] = body["messages"]
        return httpx.Response(200, json={"choices": [{"message": {"content": " A summary. "}}]})

    transport = httpx.MockTransport(handle)
    out = await summarize_transcript(
        config=_config(),
        api_key="k",
        model="gpt-4o-mini",
        transcript="[00:00] GM: You enter the cave.",
        client_factory=lambda: _client(transport),
    )

    assert out == "A summary."  # trimmed
    assert str(captured["path"]).endswith("/chat/completions")
    assert captured["model"] == "gpt-4o-mini"
    messages = captured["messages"]
    assert isinstance(messages, list)
    assert messages[0]["role"] == "system"
    assert "You enter the cave." in messages[1]["content"]


async def test_summarize_custom_system_prompt_overrides_default() -> None:
    captured: dict[str, object] = {}

    def handle(request: httpx.Request) -> httpx.Response:
        captured["messages"] = json.loads(request.content)["messages"]
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    transport = httpx.MockTransport(handle)

    await summarize_transcript(
        config=_config(),
        api_key="k",
        model="m",
        transcript="t",
        system_prompt="Fasse in Stichpunkten auf Deutsch zusammen.",
        client_factory=lambda: _client(transport),
    )
    messages = captured["messages"]
    assert isinstance(messages, list)
    assert messages[0] == {
        "role": "system",
        "content": "Fasse in Stichpunkten auf Deutsch zusammen.",
    }

    # Blank/whitespace falls back to the built-in instructions.
    await summarize_transcript(
        config=_config(),
        api_key="k",
        model="m",
        transcript="t",
        system_prompt="   ",
        client_factory=lambda: _client(transport),
    )
    messages = captured["messages"]
    assert isinstance(messages, list)
    assert messages[0]["content"] == DEFAULT_SYSTEM_PROMPT


async def test_summarize_unexpected_payload_yields_empty() -> None:
    transport = httpx.MockTransport(lambda _r: httpx.Response(200, json={"oops": True}))
    out = await summarize_transcript(
        config=_config(),
        api_key=None,
        model="m",
        transcript="x",
        client_factory=lambda: _client(transport),
    )
    assert out == ""


async def test_summarize_retries_without_temperature_for_reasoning_models() -> None:
    calls: list[dict[str, object]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body)
        if "temperature" in body:
            return httpx.Response(
                400,
                json={
                    "error": {
                        "message": (
                            "Unsupported value: 'temperature' does not support 0.3 "
                            "with this model. Only the default (1) value is supported."
                        ),
                        "param": "temperature",
                        "code": "unsupported_value",
                    }
                },
            )
        return httpx.Response(200, json={"choices": [{"message": {"content": "Summary."}}]})

    transport = httpx.MockTransport(handle)
    out = await summarize_transcript(
        config=_config(),
        api_key="k",
        model="gpt-5.6-terra",
        transcript="x",
        client_factory=lambda: _client(transport),
    )

    assert out == "Summary."
    assert len(calls) == 2
    assert "temperature" in calls[0]
    assert "temperature" not in calls[1]


async def test_summarize_transcript_raises_llm_error_with_upstream_message() -> None:
    def handle(_r: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={"error": {"message": "The model `gpt-5.6-terra` does not exist."}},
        )

    transport = httpx.MockTransport(handle)
    with pytest.raises(LLMError, match=re.escape("gpt-5.6-terra")):
        await summarize_transcript(
            config=_config(),
            api_key="k",
            model="gpt-5.6-terra",
            transcript="x",
            client_factory=lambda: _client(transport),
        )


async def test_summarize_transcript_raises_llm_error_on_connect_failure() -> None:
    def boom(_r: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    transport = httpx.MockTransport(boom)
    with pytest.raises(LLMError):
        await summarize_transcript(
            config=_config(),
            api_key="k",
            model="gpt-4o-mini",
            transcript="x",
            client_factory=lambda: _client(transport),
        )


async def test_chat_health_ok_and_failure() -> None:
    ok_transport = httpx.MockTransport(lambda _r: httpx.Response(200, json={"data": []}))
    report = await chat_health(
        config=_config(),
        api_key="k",
        client_factory=lambda: _client(ok_transport),
    )
    assert report.status is HealthStatus.HEALTHY

    def boom(_r: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    report = await chat_health(
        config=_config(),
        api_key=None,
        client_factory=lambda: _client(httpx.MockTransport(boom)),
    )
    assert report.status is HealthStatus.UNREACHABLE


async def test_chat_health_rejects_a_bad_key_instead_of_passing_it() -> None:
    """The regression this whole grading exists for.

    ``GET /models`` used to be graded ``status_code < 500``, so both live
    answers below - Google's 400 on its OpenAI-compatible surface and OpenAI's
    own 401 - reported a completely invalid key as healthy. Both bodies are
    pinned from real calls, including Google's one-element-array envelope,
    which the bare ``/models`` route does *not* use but the sibling chat route
    does.
    """
    google = httpx.Response(
        400,
        json={"error": {"code": 400, "message": "Invalid Auth key.", "status": "INVALID_ARGUMENT"}},
    )
    openai = httpx.Response(
        401,
        json={
            "error": {
                "message": (
                    "Incorrect API key provided: sk-proj-****s000. You can find your API "
                    "key at https://platform.openai.com/account/api-keys."
                ),
                "type": "invalid_request_error",
                "param": None,
                "code": "invalid_api_key",
            }
        },
    )
    for response, expected in ((google, "Invalid Auth key."), (openai, "Incorrect API key")):
        report = await chat_health(
            config=_config(),
            api_key="bad",
            client_factory=lambda: _client(httpx.MockTransport(lambda _r: response)),  # noqa: B023
        )
        assert report.status is HealthStatus.UNAUTHORIZED
        assert report.detail is not None
        assert expected in report.detail


async def test_chat_health_treats_a_rate_limit_as_a_working_credential() -> None:
    """429 is the case a bare ``== 200`` would get wrong in the other direction.

    Being throttled means the key was recognised, so calling it broken would
    send a GM to replace a key that is fine. It is not healthy either: the
    provider cannot serve a session right now.
    """
    throttled = httpx.Response(429, json={"error": {"message": "Rate limit exceeded"}})
    report = await chat_health(
        config=_config(),
        api_key="k",
        client_factory=lambda: _client(httpx.MockTransport(lambda _r: throttled)),
    )
    assert report.status is HealthStatus.DEGRADED
    assert report.detail == "Rate limit exceeded"


async def test_chat_health_asks_openrouter_about_the_key_not_the_catalogue() -> None:
    """OpenRouter serves /models to anonymous callers, so it proves nothing.

    Verified live: 425 models come back with no Authorization header at all.
    /key is the route that actually answers "is this credential any good".
    """
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(200, json={"data": {"label": "test", "usage": 0}})

    config = _config().model_copy(update={"kind": ProviderKind.OPENROUTER})
    report = await chat_health(
        config=config,
        api_key="sk-or-v1-x",
        client_factory=lambda: _client(httpx.MockTransport(handler)),
    )
    assert seen == [f"{_PATH_PREFIX}/key"]
    assert report.status is HealthStatus.HEALTHY

    # And every other kind still asks the model list, which does check the key.
    seen.clear()
    report = await chat_health(
        config=_config(),
        api_key="k",
        client_factory=lambda: _client(httpx.MockTransport(handler)),
    )
    assert seen == [f"{_PATH_PREFIX}/models"]


async def test_openrouter_endpoint_attribution_headers_and_model_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An OpenRouter provider needs nothing configured beyond the model: it
    defaults to OpenRouter's endpoint and sends the two optional attribution
    headers. The model is not defaulted here any more - the summarize route
    requires one and passes it straight through, so what goes on the wire is
    exactly what the GM picked, in OpenRouter's ``vendor/model`` form."""
    seen: dict[str, str] = {}

    def handle(request: httpx.Request) -> httpx.Response:
        seen["model"] = json.loads(request.content)["model"]
        return httpx.Response(200, json={"choices": [{"message": {"content": "A summary."}}]})

    real_client = httpx.AsyncClient

    def fake_client(
        *,
        base_url: str,
        headers: dict[str, str],
        timeout: float,
    ) -> httpx.AsyncClient:
        # No client_factory here - the point is what llm.py builds itself.
        seen["base_url"] = base_url
        seen.update(headers)
        return real_client(
            base_url=base_url,
            headers=headers,
            timeout=timeout,
            transport=httpx.MockTransport(handle),
        )

    monkeypatch.setattr(httpx, "AsyncClient", fake_client)
    out = await summarize_transcript(
        config=_openrouter_config(), api_key="k", model="openai/gpt-5.6-luna", transcript="x"
    )

    assert out == "A summary."
    assert seen["base_url"] == "https://openrouter.ai/api/v1"
    assert seen["Authorization"] == "Bearer k"
    assert seen["HTTP-Referer"].startswith("https://")
    assert seen["X-Title"] == "Loreline"
    assert seen["model"] == "openai/gpt-5.6-luna"  # a bare OpenAI name is no id there


async def test_gemini_summarizes_through_googles_openai_compatible_base(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Google serves chat under ".../v1beta/openai", a sibling of the native
    ".../v1beta" the transcription connector posts to. One provider row cannot
    carry both, so the kind's default is what has to be right - and the
    "/openai" segment has to survive being joined with "/chat/completions".
    None of OpenRouter's attribution headers belong on it."""
    seen: dict[str, str] = {}

    def handle(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        return httpx.Response(200, json={"choices": [{"message": {"content": "A summary."}}]})

    real_client = httpx.AsyncClient

    def fake_client(
        *,
        base_url: str,
        headers: dict[str, str],
        timeout: float,
    ) -> httpx.AsyncClient:
        seen["base_url"] = base_url
        seen.update(headers)
        return real_client(
            base_url=base_url,
            headers=headers,
            timeout=timeout,
            transport=httpx.MockTransport(handle),
        )

    monkeypatch.setattr(httpx, "AsyncClient", fake_client)
    out = await summarize_transcript(
        config=_gemini_config(), api_key="k", model="gemini-3.5-flash", transcript="x"
    )

    assert out == "A summary."
    assert seen["base_url"] == "https://generativelanguage.googleapis.com/v1beta/openai"
    assert seen["path"] == "/v1beta/openai/chat/completions"
    assert seen["Authorization"] == "Bearer k"
    assert "HTTP-Referer" not in seen


async def test_error_message_survives_an_array_wrapped_error_envelope() -> None:
    """Google's compatible endpoint answers /chat/completions with
    ``[{"error": {...}}]`` where every other endpoint sends the bare object.
    Read literally, that has no "error" key at all, and every Gemini failure
    would reach the GM as "404 Not Found" with the reason discarded."""

    def handle(_r: httpx.Request) -> httpx.Response:
        return httpx.Response(
            404,
            json=[
                {
                    "error": {
                        "code": 404,
                        "message": "models/nope-9 is not found for API version v1main",
                        "status": "NOT_FOUND",
                    }
                }
            ],
        )

    transport = httpx.MockTransport(handle)
    with pytest.raises(LLMError, match=re.escape("models/nope-9 is not found")):
        await summarize_transcript(
            config=_gemini_config(),
            api_key="k",
            model="nope-9",
            transcript="x",
            client_factory=lambda: _client(transport),
        )


async def test_openrouter_routing_prefs_ride_along_as_the_provider_object() -> None:
    """The GM's routing choices reach OpenRouter as the body's ``provider``
    object - that is the only channel OpenRouter offers for "cheapest" and
    "nobody who stores my transcript"."""
    captured: dict[str, object] = {}

    def handle(request: httpx.Request) -> httpx.Response:
        captured["provider"] = json.loads(request.content).get("provider")
        return httpx.Response(200, json={"choices": [{"message": {"content": "A summary."}}]})

    config = _openrouter_config().model_copy(
        update={
            "routing": OpenRouterRouting(sort="price", data_collection="deny", zdr=True),
        }
    )
    transport = httpx.MockTransport(handle)
    await summarize_transcript(
        config=config,
        api_key="k",
        model="openai/gpt-5.6-luna",
        transcript="[00:00] GM: You enter the cave.",
        client_factory=lambda: _client(transport),
    )

    assert captured["provider"] == {"sort": "price", "data_collection": "deny", "zdr": True}


async def test_routing_omits_fields_left_at_their_default() -> None:
    """Only what the GM actually changed is sent. An ``provider`` object
    echoing OpenRouter's own defaults back at them would freeze today's
    behaviour into every request for no reason."""
    assert routing_payload(_openrouter_config()) is None
    assert (
        routing_payload(_openrouter_config().model_copy(update={"routing": OpenRouterRouting()}))
        is None
    )
    assert routing_payload(
        _openrouter_config().model_copy(update={"routing": OpenRouterRouting(sort="price")})
    ) == {"sort": "price"}
    assert routing_payload(
        _openrouter_config().model_copy(update={"routing": OpenRouterRouting(zdr=True)})
    ) == {"zdr": True}


async def test_routing_is_never_sent_to_a_plain_openai_compatible_endpoint() -> None:
    """``provider`` is an OpenRouter body extension. A stray one stored on an
    ``openai_compat`` config (Ollama, LM Studio, self-hosted) must not go out -
    a strict endpoint would reject the unknown field outright."""
    captured: dict[str, object] = {"seen": "unset"}

    def handle(request: httpx.Request) -> httpx.Response:
        captured["seen"] = json.loads(request.content).get("provider", "absent")
        return httpx.Response(200, json={"choices": [{"message": {"content": "A summary."}}]})

    config = _config().model_copy(update={"routing": OpenRouterRouting(sort="price", zdr=True)})
    assert routing_payload(config) is None

    transport = httpx.MockTransport(handle)
    await summarize_transcript(
        config=config,
        api_key="k",
        model="gpt-4o-mini",
        transcript="[00:00] GM: You enter the cave.",
        client_factory=lambda: _client(transport),
    )

    assert captured["seen"] == "absent"


class TestReasoningEffort:
    """The two gateways spell reasoning effort differently, and sending the
    wrong spelling is silently ignored rather than erroring - which would look
    exactly like the setting doing nothing."""

    async def test_openrouter_gets_the_nested_reasoning_object(self) -> None:
        captured: dict[str, object] = {}

        def handle(request: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(request.content)
            return httpx.Response(200, json={"choices": [{"message": {"content": "A summary."}}]})

        await summarize_transcript(
            config=_openrouter_config(),
            api_key="k",
            model="openai/gpt-5.6-luna",
            transcript="t",
            reasoning_effort="high",
            client_factory=lambda: _client(httpx.MockTransport(handle)),
        )
        body = captured["body"]
        assert isinstance(body, dict)
        assert body["reasoning"] == {"effort": "high"}
        assert "reasoning_effort" not in body

    async def test_openai_compatible_gets_the_flat_field(self) -> None:
        captured: dict[str, object] = {}

        def handle(request: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(request.content)
            return httpx.Response(200, json={"choices": [{"message": {"content": "A summary."}}]})

        await summarize_transcript(
            config=_config(),
            api_key="k",
            model="gpt-5.6",
            transcript="t",
            reasoning_effort="low",
            client_factory=lambda: _client(httpx.MockTransport(handle)),
        )
        body = captured["body"]
        assert isinstance(body, dict)
        assert body["reasoning_effort"] == "low"
        assert "reasoning" not in body

    async def test_no_effort_sends_no_reasoning_field_at_all(self) -> None:
        """A model that reasons by default must keep doing so when the GM never
        touched the setting."""
        captured: dict[str, object] = {}

        def handle(request: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(request.content)
            return httpx.Response(200, json={"choices": [{"message": {"content": "A summary."}}]})

        await summarize_transcript(
            config=_openrouter_config(),
            api_key="k",
            model="openai/gpt-5.6-luna",
            transcript="t",
            client_factory=lambda: _client(httpx.MockTransport(handle)),
        )
        body = captured["body"]
        assert isinstance(body, dict)
        assert "reasoning" not in body
        assert "reasoning_effort" not in body

    async def test_an_endpoint_that_rejects_the_field_still_summarizes(self) -> None:
        """A preference is not worth failing the summary over - same treatment
        an explicit temperature already gets."""
        calls: list[dict[str, object]] = []

        def handle(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            calls.append(body)
            if "reasoning_effort" in body:
                return httpx.Response(
                    400,
                    json={"error": {"message": "unsupported", "param": "reasoning_effort"}},
                )
            return httpx.Response(200, json={"choices": [{"message": {"content": "A summary."}}]})

        out = await summarize_transcript(
            config=_config(),
            api_key="k",
            model="some-model",
            transcript="t",
            reasoning_effort="high",
            client_factory=lambda: _client(httpx.MockTransport(handle)),
        )
        assert out == "A summary."
        assert len(calls) == 2  # rejected, then retried without it
        assert "reasoning_effort" not in calls[1]
