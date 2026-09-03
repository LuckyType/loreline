"""Tests for the provider model catalog (live /v1/models + curated fallback)."""

from __future__ import annotations

import httpx

from loreline import capabilities
from loreline.models import Interaction, ModelInfo, ProviderKind
from loreline.stt import catalog
from loreline.stt.catalog import list_models


def _factory(handler: httpx.MockTransport) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=handler)


def _ids(models: list[ModelInfo]) -> list[str]:
    return [m.id for m in models]


async def test_curated_fallback_for_non_openai() -> None:
    models = await list_models(kind=ProviderKind.DEEPGRAM, base_url=None, api_key=None)
    assert "nova-3" in _ids(models)  # curated catalog (no /v1/models endpoint)


async def test_gemini_offers_both_transports() -> None:
    """Gemini is not fetched live, so the curated list is the only path into
    the transcribe catalogue. Both models belong in it now that the Live
    connector has been verified against the real service, and each carries the
    transport flag the picker badges."""
    models = await list_models(kind=ProviderKind.GEMINI, base_url=None, api_key=None)
    assert _ids(models) == ["gemini-3.5-transcribe", "gemini-3.5-transcribe-live"]
    assert [m.realtime for m in models] == [False, True]


async def test_gemini_summarize_picker_offers_chat_models_not_the_transcriber() -> None:
    """Gemini publishes no list this app fetches, so both its pickers fall back
    to a curated one - and they must not fall back to the same one. The
    transcription table in stt.catalog knows nothing about chat, so a summarize
    picker reading it would offer gemini-3.5-transcribe to write a summary
    with."""
    models = await list_models(
        kind=ProviderKind.GEMINI,
        base_url=None,
        api_key=None,
        interaction=Interaction.SUMMARIZE,
    )
    assert "gemini-3.5-flash" in _ids(models)
    assert "gemini-3.5-transcribe" not in _ids(models)


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
    """The diarization flag has to survive the live-fetch path.

    Otherwise the picker refuses inline diarization for a model that supports
    it, or worse offers it for one that does not.
    """

    def handle(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"data": [{"id": "gemini-3.5-transcribe"}, {"id": "some-unknown-asr"}]},
        )

    models = await list_models(
        kind=ProviderKind.GEMINI,
        base_url=None,
        api_key="k",
        interaction=Interaction.TRANSCRIBE,
        client_factory=lambda: _factory(httpx.MockTransport(handle)),
    )
    by_id = {m.id: m for m in models}
    assert by_id["gemini-3.5-transcribe"].inline_diarization is True
    # This kind publishes no usable catalogue, so the curated list stands and
    # an id the server volunteered is not smuggled into the picker.
    assert "some-unknown-asr" not in by_id


async def test_openrouter_transcription_never_claims_diarization() -> None:
    """Including for the model whose own description advertises it.

    x-ai/grok-stt-1.0's OpenRouter page claims "optional speaker diarization",
    and this repo believed it. The gateway exposes no diarization parameter and
    returns no speaker structure, so the claim does not survive contact with
    the API and the flag must stay false through the live-fetch path.
    """

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
    assert all(m.inline_diarization is False for m in models)


# Interactions that fall back to the curated catalogue when no live list is
# available. Video is not one of them: its catalogue lives on a separate
# endpoint (see _fetch_video_models), and a failed fetch there yields nothing
# rather than falling through.
_FALLBACK_INTERACTIONS = (Interaction.TRANSCRIBE, Interaction.SUMMARIZE)


def _dead_client() -> httpx.AsyncClient:
    """A client whose every request fails, so the curated path is what runs."""
    return _factory(httpx.MockTransport(lambda _r: httpx.Response(500)))


async def _offered(kind: ProviderKind, interaction: Interaction) -> list[str]:
    models = await list_models(
        kind=kind,
        base_url="http://unreachable",
        api_key="k",
        interaction=interaction,
        client_factory=_dead_client,
    )
    return _ids(models)


async def test_the_curated_catalogue_has_exactly_one_gate() -> None:
    """capabilities.yaml decides what a picker offers, and nothing else does.

    This module used to hold a second list, ``_CURATED``, and the two had
    already drifted: it offered six task-tuned Deepgram variants the yaml
    deliberately does not list, and it kept gemini-3.5-transcribe-live out of
    every picker for a while after the yaml unhid it, because nobody
    remembered there was a second gate to edit. Asserting equality for every
    kind and every interaction is what makes that unreintroducible - any list
    here that adds or withholds an id fails this.
    """
    for kind in ProviderKind:
        for interaction in _FALLBACK_INTERACTIONS:
            assert await _offered(kind, interaction) == capabilities.curated_models(
                kind, interaction
            ), f"{kind.value}/{interaction.value} disagrees with capabilities.yaml"


def test_this_module_keeps_no_model_list_of_its_own() -> None:
    """The structural half of the guard above.

    A second list only becomes a gate once something reads it, and adding a
    reader back is a one-line accident. This fails at the list instead: no
    constant in loreline.stt.catalog may name a model capabilities.yaml
    curates, whatever it is called.
    """
    curated = {
        model.id for spec in capabilities.config().providers.values() for model in spec.models
    }
    offenders = {
        name: sorted(curated & _strings(value))
        for name, value in vars(catalog).items()
        if not name.startswith("__") and curated & _strings(value)
    }
    assert not offenders, f"model ids listed outside capabilities.yaml: {offenders}"


def _strings(value: object) -> set[str]:
    """Every string reachable in a module-level constant, containers included."""
    if isinstance(value, str):
        return {value}
    if isinstance(value, dict):
        items: list[object] = [*value.keys(), *value.values()]  # type: ignore[dict-item]
        return {s for item in items for s in _strings(item)}
    if isinstance(value, list | tuple | set | frozenset):
        return {s for item in value for s in _strings(item)}  # type: ignore[union-attr]
    return set()


async def test_hidden_models_are_never_offered() -> None:
    """`hidden` means fully described, never in a picker, still routable.

    Two models ship behind it: connectors written against the documented
    request shapes but never run against the real API, for want of a key. They
    must stay out of every list a GM can pick from, while a config naming one
    explicitly still resolves to its connector - which is how the verification
    run gets switched on without a code change.
    """
    for kind, spec in capabilities.config().providers.items():
        hidden = {model.id for model in spec.models if model.hidden}
        if not hidden:
            continue
        for interaction in _FALLBACK_INTERACTIONS:
            assert not hidden & set(await _offered(kind, interaction))

    assert capabilities.curated_models(ProviderKind.DEEPGRAM, Interaction.TRANSCRIBE) == [
        "nova-3",
        "nova-2",
        "flux-general-en",
        "flux-general-multi",
    ]  # not whisper-large
    assert "universal-2" not in capabilities.curated_models(
        ProviderKind.ASSEMBLYAI, Interaction.TRANSCRIBE
    )


async def test_a_curated_list_never_crosses_interactions() -> None:
    """A transcribe picker must not offer a chat model, or the reverse.

    Both have happened here: an unscoped OpenAI /models dump offered dall-e-3
    to transcribe with, and a summarize picker falling through to the
    transcription table offered gemini-3.5-transcribe to write a summary with.
    """
    for kind, spec in capabilities.config().providers.items():
        declared = {model.id: set(model.interactions) for model in spec.models}
        for interaction in _FALLBACK_INTERACTIONS:
            for model_id in await _offered(kind, interaction):
                assert interaction in declared[model_id], (
                    f"{kind.value} offers {model_id} for {interaction.value}"
                )
