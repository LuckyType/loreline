"""Tests for the OpenAI-compatible chat connector used for session summaries."""

from __future__ import annotations

import json
import re

import httpx
import pytest

from loreline.llm import DEFAULT_SYSTEM_PROMPT, LLMError, chat_health, summarize_transcript
from loreline.models import Protocol, ProviderConfig, ProviderKind

_BASE_URL = "http://llm:1234/v1"


def _config() -> ProviderConfig:
    return ProviderConfig(
        id="l1",
        name="LLM",
        kind=ProviderKind.OPENAI_CHAT,
        protocol=Protocol.HTTP_BATCH,
        base_url=_BASE_URL,
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
        model=None,
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
    assert await chat_health(
        config=_config(),
        api_key="k",
        client_factory=lambda: _client(ok_transport),
    )

    def boom(_r: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    assert not await chat_health(
        config=_config(),
        api_key=None,
        client_factory=lambda: _client(httpx.MockTransport(boom)),
    )
