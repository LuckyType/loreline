"""Tests for the provider model catalog (live /v1/models + curated fallback)."""

from __future__ import annotations

import httpx

from loreline.models import Interaction, ModelInfo, ProviderKind
from loreline.stt.catalog import list_models


def _factory(handler: httpx.MockTransport) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=handler)


def _ids(models: list[ModelInfo]) -> list[str]:
    return [m.id for m in models]


async def test_curated_fallback_for_non_openai() -> None:
    models = await list_models(kind=ProviderKind.DEEPGRAM, base_url=None, api_key=None)
    assert "nova-3" in _ids(models)  # curated catalog (no /v1/models endpoint)


async def test_gemini_live_model_is_hidden_until_verified() -> None:
    """gemini-3.5-transcribe-live has a connector, but one that has never run
    against the real service - the curated list is the gate that keeps it out
    of every picker until a verification run succeeds (see the comment on
    _CURATED in loreline.stt.catalog). Gemini is not fetched live, so this
    list is the only path into the transcribe catalogue."""
    models = await list_models(kind=ProviderKind.GEMINI, base_url=None, api_key=None)
    assert "gemini-3.5-transcribe" in _ids(models)
    assert "gemini-3.5-transcribe-live" not in _ids(models)


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
    assert _ids(models) == ["gpt-4o-transcribe", "whisper-1"]  # sorted + deduped


async def test_live_failure_falls_back_to_empty() -> None:
    transport = httpx.MockTransport(lambda _r: httpx.Response(500))
    models = await list_models(
        kind=ProviderKind.OPENAI_COMPAT,
        base_url="http://x",
        api_key=None,
        client_factory=lambda: _factory(transport),
    )
    assert models == []  # openai_compat has no curated list; a failed fetch yields nothing


async def test_live_openai_compatible_chat_models() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "llm"  # self-hosted base_url is honoured
        assert request.url.path.endswith("/models")
        return httpx.Response(200, json={"data": [{"id": "llama3"}, {"id": "qwen2.5"}]})

    transport = httpx.MockTransport(handle)
    models = await list_models(
        kind=ProviderKind.OPENAI_COMPAT,
        base_url="http://llm:1234/v1",
        api_key="k",
        client_factory=lambda: _factory(transport),
    )
    assert _ids(models) == ["llama3", "qwen2.5"]


async def test_live_openrouter_models() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "openrouter.ai"  # the kind brings its own base URL
        assert request.url.path.endswith("/models")
        return httpx.Response(
            200, json={"data": [{"id": "openai/gpt-4o"}, {"id": "anthropic/claude-sonnet-4.5"}]}
        )

    transport = httpx.MockTransport(handle)
    models = await list_models(
        kind=ProviderKind.OPENROUTER,
        base_url=None,
        api_key="k",
        client_factory=lambda: _factory(transport),
    )
    assert _ids(models) == ["anthropic/claude-sonnet-4.5", "openai/gpt-4o"]


async def test_openrouter_pricing_is_scaled_to_usd_per_million_tokens() -> None:
    """OpenRouter quotes USD per single token as a decimal string; the pickers
    show the per-million figure people actually compare. The scaling goes
    through ``Decimal``, so 0.000003 must land on exactly 3.0 - not the
    2.9999999999999996 a binary float multiply produces."""

    def handle(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "anthropic/claude-sonnet-4.5",
                        "context_length": 1000000,
                        "pricing": {"prompt": "0.000003", "completion": "0.000015"},
                    }
                ]
            },
        )

    models = await list_models(
        kind=ProviderKind.OPENROUTER,
        base_url=None,
        api_key="k",
        client_factory=lambda: _factory(httpx.MockTransport(handle)),
    )

    model = models[0]
    assert model.context_length == 1000000
    assert model.pricing is not None
    assert model.pricing.prompt == 3.0
    assert model.pricing.completion == 15.0
    assert model.price_tiers == []


async def test_price_overrides_become_tiers_ordered_by_threshold() -> None:
    """A long-context model reprices above a prompt-length threshold. The
    picker has to be able to say so - a transcript is exactly the kind of
    prompt that crosses one."""

    def handle(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "anthropic/claude-sonnet-4.5",
                        "pricing": {
                            "prompt": "0.000003",
                            "completion": "0.000015",
                            "overrides": [
                                {
                                    "min_prompt_tokens": 500000,
                                    "prompt": "0.000009",
                                    "completion": "0.00003",
                                },
                                {
                                    "min_prompt_tokens": 200000,
                                    "prompt": "0.000006",
                                    "completion": "0.0000225",
                                },
                            ],
                        },
                    }
                ]
            },
        )

    models = await list_models(
        kind=ProviderKind.OPENROUTER,
        base_url=None,
        api_key="k",
        client_factory=lambda: _factory(httpx.MockTransport(handle)),
    )

    tiers = models[0].price_tiers
    assert [t.min_prompt_tokens for t in tiers] == [200000, 500000]  # cheapest threshold first
    assert (tiers[0].prompt, tiers[0].completion) == (6.0, 22.5)


async def test_models_without_pricing_still_list_cleanly() -> None:
    """Plain OpenAI ``/models`` publishes no prices at all, and a curated
    entry has nothing but a name. Both must come back as usable rows rather
    than being dropped or defaulted to a price of zero."""

    def handle(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": [
                    {"id": "whisper-1"},
                    {"id": "whisper-weird", "pricing": {"prompt": "", "completion": None}},
                    {
                        "id": "whisper-unparseable",
                        "pricing": {"prompt": "free", "completion": "free"},
                    },
                    {"no_id": True},
                ]
            },
        )

    models = await list_models(
        kind=ProviderKind.OPENAI_COMPAT,
        base_url="http://stt:8000/v1",
        api_key="k",
        client_factory=lambda: _factory(httpx.MockTransport(handle)),
    )

    # The id-less row is skipped; the rest survive (all name-shaped as
    # transcription models, so the capability filter is a no-op here).
    assert _ids(models) == ["whisper-1", "whisper-unparseable", "whisper-weird"]
    assert all(m.pricing is None for m in models)  # never 0.0, which would read as free

    curated = await list_models(kind=ProviderKind.DEEPGRAM, base_url=None, api_key=None)
    assert all(m.pricing is None and m.context_length is None for m in curated)


async def test_transcription_models_report_no_price() -> None:
    """Audio models are priced per unit of audio, not per token, and the
    catalogue does not say which unit: measured against the live API,
    deepgram/nova-3's "0.0043" bills per minute while nvidia/nemotron-3.5-asr's
    "0.00000333" bills per second. Treating either as a per-token rate produced
    "$4300 / $0" in the picker. No price beats a wrong one.
    """

    def handle(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "deepgram/nova-3",
                        "architecture": {
                            "input_modalities": ["audio"],
                            "output_modalities": ["transcription"],
                        },
                        "pricing": {"prompt": "0.0043", "completion": "0"},
                    }
                ]
            },
        )

    models = await list_models(
        kind=ProviderKind.OPENROUTER,
        base_url=None,
        api_key="k",
        interaction=Interaction.TRANSCRIBE,
        client_factory=lambda: _factory(httpx.MockTransport(handle)),
    )
    assert models[0].pricing is None


async def test_chat_models_keep_their_per_token_price() -> None:
    """The suppression is scoped to audio: text models really are per-token,
    and $3/$15 per million is the figure people compare."""

    def handle(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "anthropic/claude-sonnet-4.5",
                        "architecture": {
                            "input_modalities": ["text"],
                            "output_modalities": ["text"],
                        },
                        "pricing": {"prompt": "0.000003", "completion": "0.000015"},
                    }
                ]
            },
        )

    models = await list_models(
        kind=ProviderKind.OPENROUTER,
        base_url=None,
        api_key="k",
        interaction=Interaction.SUMMARIZE,
        client_factory=lambda: _factory(httpx.MockTransport(handle)),
    )
    assert models[0].pricing is not None
    assert (models[0].pricing.prompt, models[0].pricing.completion) == (3.0, 15.0)


async def test_video_models_come_from_the_video_catalogue() -> None:
    """Video has its own endpoint. Falling through to the plain /models list
    served the chat catalogue for video generation, i.e. 400-odd text models
    offered to a picker that can only run a video model.
    """
    seen: dict[str, str] = {}

    def handle(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        return httpx.Response(200, json={"data": [{"id": "alibaba/wan-3.0"}]})

    models = await list_models(
        kind=ProviderKind.OPENROUTER,
        base_url=None,
        api_key="k",
        interaction=Interaction.VIDEO,
        client_factory=lambda: _factory(httpx.MockTransport(handle)),
    )
    assert seen["path"].endswith("/videos/models")
    assert [m.id for m in models] == ["alibaba/wan-3.0"]


async def test_video_models_are_empty_for_a_kind_that_cannot_generate_video() -> None:
    models = await list_models(
        kind=ProviderKind.DEEPGRAM,
        base_url=None,
        api_key="k",
        interaction=Interaction.VIDEO,
        client_factory=lambda: _factory(httpx.MockTransport(lambda _r: httpx.Response(200))),
    )
    assert models == []


async def test_openai_transcription_lists_live_and_stamps_transport_per_model() -> None:
    """The old workaround kept OpenAI's transcribe picker on a curated
    realtime-only list, because the kind-keyed registry could only ever build
    the Realtime connector. Now that the registry resolves per model, the live
    /models list is served whole - narrowed to transcription models - and each
    entry says which transport it rides."""

    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "api.openai.com"
        return httpx.Response(
            200,
            json={
                "data": [
                    {"id": "gpt-4o"},
                    {"id": "dall-e-3"},
                    {"id": "whisper-1"},
                    {"id": "gpt-transcribe"},
                    {"id": "gpt-live-transcribe"},
                ]
            },
        )

    transport = httpx.MockTransport(handle)
    models = await list_models(
        kind=ProviderKind.OPENAI,
        base_url=None,
        api_key="k",
        interaction=Interaction.TRANSCRIBE,
        client_factory=lambda: _factory(transport),
    )
    assert _ids(models) == ["gpt-live-transcribe", "gpt-transcribe", "whisper-1"]
    by_id = {m.id: m for m in models}
    assert by_id["gpt-live-transcribe"].realtime is True
    assert by_id["gpt-transcribe"].realtime is False
    assert by_id["whisper-1"].realtime is False


async def test_openai_transcription_falls_back_to_curated_when_the_fetch_fails() -> None:
    transport = httpx.MockTransport(lambda _r: httpx.Response(500))
    models = await list_models(
        kind=ProviderKind.OPENAI,
        base_url=None,
        api_key="k",
        interaction=Interaction.TRANSCRIBE,
        client_factory=lambda: _factory(transport),
    )
    # The fallback now spans both transports, since either connector can run.
    assert "gpt-realtime-whisper" in _ids(models)
    assert "gpt-transcribe" in _ids(models)


async def test_live_transcription_lists_carry_inline_diarization_flags() -> None:
    """grok-stt-1.0 is the one OpenRouter transcription model that returns
    speaker labels; the flag has to survive the live-fetch path or the picker
    refuses inline diarization for a model that supports it."""

    def handle(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"data": [{"id": "x-ai/grok-stt-1.0"}, {"id": "openai/whisper-large-v3-turbo"}]},
        )

    models = await list_models(
        kind=ProviderKind.OPENROUTER,
        base_url=None,
        api_key="k",
        interaction=Interaction.TRANSCRIBE,
        client_factory=lambda: _factory(httpx.MockTransport(handle)),
    )
    by_id = {m.id: m for m in models}
    assert by_id["x-ai/grok-stt-1.0"].inline_diarization is True
    assert by_id["openai/whisper-large-v3-turbo"].inline_diarization is False
