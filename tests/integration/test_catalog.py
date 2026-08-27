"""Tests for the provider model catalog (live /v1/models + curated fallback)."""

from __future__ import annotations

import httpx

from loreline.models import ProviderKind
from loreline.stt.catalog import list_models


def _factory(handler: httpx.MockTransport) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=handler)


async def test_curated_fallback_for_non_openai() -> None:
    models = await list_models(kind=ProviderKind.DEEPGRAM, base_url=None, api_key=None)
    assert "nova-3" in models  # curated catalog (no /v1/models endpoint)


async def test_live_openai_compatible_models() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/models")
        assert request.headers.get("Authorization") == "Bearer k"
        return httpx.Response(
            200, json={"data": [{"id": "whisper-1"}, {"id": "gpt-4o-transcribe"}]}
        )

    transport = httpx.MockTransport(handle)
    models = await list_models(
        kind=ProviderKind.OPENAI_COMPAT,
        base_url="http://speaches:8000/v1",
        api_key="k",
        client_factory=lambda: _factory(transport),
    )
    assert models == ["gpt-4o-transcribe", "whisper-1"]  # sorted + deduped


async def test_live_failure_falls_back_to_empty() -> None:
    transport = httpx.MockTransport(lambda _r: httpx.Response(500))
    models = await list_models(
        kind=ProviderKind.OPENAI_COMPAT,
        base_url="http://x",
        api_key=None,
        client_factory=lambda: _factory(transport),
    )
    assert models == []  # openai_compat has no curated list; a failed fetch yields nothing


async def test_live_openai_chat_models() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "llm"  # self-hosted base_url is honoured
        assert request.url.path.endswith("/models")
        return httpx.Response(200, json={"data": [{"id": "llama3"}, {"id": "qwen2.5"}]})

    transport = httpx.MockTransport(handle)
    models = await list_models(
        kind=ProviderKind.OPENAI_CHAT,
        base_url="http://llm:1234/v1",
        api_key="k",
        client_factory=lambda: _factory(transport),
    )
    assert models == ["llama3", "qwen2.5"]
