"""The model pickers as a projection of the one catalogue reader.

What a vendor body becomes is pinned in test_catalog_reader.py. What is pinned
here is the picker's own contract: which catalogue it asks for (the surface's
address and credential spelling, per kind and interaction), how a probe's rows
land in ``ModelInfo``, and that an unusable probe, or a catalogue the yaml
marks curated-only, yields the curated list and never a guess.
"""

from __future__ import annotations

import httpx
from test_catalog_reader import CHAT_BODY

from loreline import capabilities
from loreline.models import Interaction, ModelInfo, ProviderKind
from loreline.stt import catalog
from loreline.stt.catalog import list_models


def _factory(handler: httpx.MockTransport) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=handler)


def _ids(models: list[ModelInfo]) -> list[str]:
    return [m.id for m in models]


async def test_a_catalogue_marked_curated_only_is_never_fetched() -> None:
    """The reader parses Deepgram's list, and the staleness check reads it,
    but the yaml marks its ``catalog`` surface ``picker: false``: every
    task-tuned variant under two names is not a list to choose from. The
    picker must not even ask."""
    called: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        called.append(str(request.url))
        return httpx.Response(200, json={"stt": [{"name": "nova-3-medical"}]})

    models = await list_models(
        kind=ProviderKind.DEEPGRAM,
        base_url=None,
        api_key="k",
        client_factory=lambda: _factory(httpx.MockTransport(handle)),
    )
    assert called == []
    assert _ids(models) == capabilities.curated_models(
        ProviderKind.DEEPGRAM, Interaction.TRANSCRIBE
    )


async def test_gemini_offers_both_transports() -> None:
    """Gemini's catalogue is curated-only for the pickers, so the curated list
    is the only path into the transcribe picker. Both models belong in it now
    that the Live connector has been verified against the real service, and
    each carries the transport flag the picker badges."""
    models = await list_models(kind=ProviderKind.GEMINI, base_url=None, api_key=None)
    assert _ids(models) == ["gemini-3.5-transcribe", "gemini-3.5-transcribe-live"]
    assert [m.realtime for m in models] == [False, True]


async def test_gemini_summarize_picker_offers_chat_models_not_the_transcriber() -> None:
    """Gemini publishes no list the pickers read, so both its pickers fall back
    to a curated one, and they must not fall back to the same one: a summarize
    picker reading the transcription list would offer gemini-3.5-transcribe to
    write a summary with."""
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


async def test_picker_rows_carry_the_vendor_price_and_context() -> None:
    """Everything past ``id`` is optional and comes straight off the probe:
    the per-million price, its tiers cheapest threshold first, the context
    length and whether the model takes a reasoning effort. A curated row has
    nothing but its id, and must never read as free."""
    models = await list_models(
        kind=ProviderKind.OPENROUTER,
        base_url=None,
        api_key="k",
        interaction=Interaction.SUMMARIZE,
        client_factory=lambda: _factory(
            httpx.MockTransport(lambda _r: httpx.Response(200, json=CHAT_BODY))
        ),
    )
    by_id = {m.id: m for m in models}
    luna = by_id["openai/gpt-5.6-luna"]
    assert luna.context_length == 1000000 + 50000
    assert luna.supports_reasoning is True
    assert luna.pricing is not None
    assert (luna.pricing.prompt, luna.pricing.completion) == (3.0, 15.0)
    assert luna.price_tiers == []
    sonnet = by_id["anthropic/claude-sonnet-4.5"]
    assert sonnet.supports_reasoning is False
    assert [t.min_prompt_tokens for t in sonnet.price_tiers] == [200000, 500000]
    assert (sonnet.price_tiers[0].prompt, sonnet.price_tiers[0].completion) == (6.0, 22.5)

    curated = await list_models(kind=ProviderKind.DEEPGRAM, base_url=None, api_key=None)
    assert all(m.pricing is None and m.context_length is None for m in curated)


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


async def test_transcription_lists_carry_inline_diarization_flags() -> None:
    """The diarization flag is stamped per model on both paths, live and
    curated. Otherwise the picker refuses inline diarization for a model that
    supports it, or worse offers it for one that does not.
    """
    called: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        called.append(str(request.url))
        return httpx.Response(200, json={"models": [{"name": "models/some-unknown-asr"}]})

    models = await list_models(
        kind=ProviderKind.GEMINI,
        base_url=None,
        api_key="k",
        interaction=Interaction.TRANSCRIBE,
        client_factory=lambda: _factory(httpx.MockTransport(handle)),
    )
    by_id = {m.id: m for m in models}
    assert by_id["gemini-3.5-transcribe"].inline_diarization is True
    # Gemini's catalogue is curated-only for the pickers, so the list is never
    # asked for and an id the vendor volunteered cannot reach the picker.
    assert called == []
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


# Every interaction falls back to the curated catalogue when the probe is
# unusable; video included, since the yaml curates OpenRouter's video models
# the same way it curates its chat ones.
_FALLBACK_INTERACTIONS = tuple(Interaction)


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
