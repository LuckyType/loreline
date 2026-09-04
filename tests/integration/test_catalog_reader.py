"""The one vendor catalogue reader: every vendor payload, every failure mode.

One parser suite per vendor body, each against a frozen response trimmed to
the fields the app reads, and one probe test per status. The three projections
(the pickers, the video dialog, the staleness gate) are tested at their own
interfaces; what is pinned here is what a body becomes and what a failure
becomes.

No test here touches a real vendor: every response is served by an
``httpx.MockTransport``, including the ones that fail. That is deliberate for
more than speed. The behaviour worth guarding is what happens when a vendor is
down, slow, rate limiting, or has quietly changed the shape of its response,
and none of those can be arranged against a live API on demand.
"""

from __future__ import annotations

from datetime import date

import httpx
import pytest

from loreline.capabilities import config
from loreline.catalog import CatalogProbe, CatalogStatus, VendorPrice, probe
from loreline.models import Interaction, ProviderKind

# One realistic OpenRouter chat row, trimmed to the fields the app reads, and
# one transcription row from the same schema (``?output_modalities=
# transcription`` serves the same shape).
CHAT_BODY: dict[str, object] = {
    "data": [
        {
            "id": "openai/gpt-5.6-luna",
            "name": "OpenAI: GPT-5.6 Luna",
            "context_length": 1050000,
            "top_provider": {"context_length": 1050000, "max_completion_tokens": 128000},
            "supported_parameters": ["max_tokens", "reasoning", "response_format"],
            "reasoning": {
                "mandatory": False,
                "supported_efforts": ["max", "xhigh", "high", "medium", "low", "none"],
                "default_effort": "medium",
            },
            "pricing": {"prompt": "0.000003", "completion": "0.000015"},
            "expiration_date": None,
        },
        {
            "id": "anthropic/claude-sonnet-4.5",
            "architecture": {"input_modalities": ["text"], "output_modalities": ["text"]},
            "supported_parameters": ["temperature", "max_tokens"],
            "pricing": {
                "prompt": "0.000003",
                "completion": "0.000015",
                "overrides": [
                    {"min_prompt_tokens": 500000, "prompt": "0.000009", "completion": "0.00003"},
                    {"min_prompt_tokens": 200000, "prompt": "0.000006", "completion": "0.0000225"},
                ],
            },
            "expiration_date": "2098-12-31",
        },
    ]
}

TRANSCRIBE_BODY: dict[str, object] = {
    "data": [
        {
            "id": "deepgram/nova-3",
            "architecture": {"input_modalities": ["audio"], "output_modalities": ["transcription"]},
            "pricing": {"prompt": "0.0043", "completion": "0"},
        }
    ]
}

VIDEO_BODY: dict[str, object] = {
    "data": [
        {
            "id": "openai/sora-2-pro",
            "supported_durations": [4, 8, 12],
            "supported_resolutions": ["720p", "1080p"],
            "supported_aspect_ratios": ["16:9", "9:16"],
            "supported_frame_images": None,
            "generate_audio": None,
        },
        {
            "id": "alibaba/wan-3.0",
            "name": "Wan 3.0",
            "supported_durations": [4, 8],
            "supported_resolutions": ["480p", "720p"],
            "supported_aspect_ratios": ["16:9", "9:16"],
            "generate_audio": True,
            "seed": True,
        },
    ]
}

OPENAI_BODY: dict[str, object] = {
    "data": [
        {"id": "gpt-4o", "object": "model", "owned_by": "openai"},
        {"id": "whisper-1", "object": "model", "owned_by": "openai-internal"},
        {"id": "gpt-transcribe", "shutdown_date": "2027-03-01"},
        {"id": "whisper-weird", "pricing": {"prompt": "", "completion": None}},
        {"no_id": True},
    ]
}

DEEPGRAM_BODY: dict[str, object] = {
    "stt": [{"name": "nova-3", "canonical_name": "nova-3-general"}],
    "tts": [{"name": "aura-2"}],
}

GEMINI_BODY: dict[str, object] = {
    "models": [{"name": "models/gemini-3.5-transcribe"}],
    "nextPageToken": "more",
}


def _factory(handler: httpx.MockTransport) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=handler)


def serving(body: object, status: int = 200) -> httpx.MockTransport:
    return httpx.MockTransport(lambda _r: httpx.Response(status, json=body))


async def _probe(
    transport: httpx.MockTransport,
    *,
    kind: ProviderKind = ProviderKind.OPENROUTER,
    interaction: Interaction = Interaction.SUMMARIZE,
    api_key: str | None = None,
    base_url: str | None = None,
) -> CatalogProbe:
    return await probe(
        kind,
        interaction,
        api_key=api_key,
        base_url=base_url,
        client_factory=lambda: _factory(transport),
    )


# --------------------------------------------------------------------------
# OpenRouter, chat and transcription lists
# --------------------------------------------------------------------------


async def test_the_chat_catalogue_yields_the_facts_the_yaml_mirrors() -> None:
    result = await _probe(serving(CHAT_BODY))
    assert result.status is CatalogStatus.OK
    model = result.find("openai/gpt-5.6-luna")
    assert model is not None
    assert model.name == "OpenAI: GPT-5.6 Luna"
    assert (model.context_length, model.max_output_tokens) == (1050000, 128000)
    # temperature is absent from supported_parameters, so the vendor is saying
    # this model does not take it, which is what the yaml records for Luna.
    assert model.temperature is False
    assert model.reasoning is not None
    assert model.reasoning.mandatory is False
    assert "none" in model.reasoning.efforts
    assert model.accepts_reasoning_effort is True
    assert model.publishes_reasoning is True
    assert model.retires_on is None


async def test_the_chat_catalogue_scales_prices_to_usd_per_million_tokens() -> None:
    """OpenRouter quotes USD per single token as a decimal string; the pickers
    show the per-million figure people actually compare. The scaling goes
    through ``Decimal``, so 0.000003 must land on exactly 3.0, not the
    2.9999999999999996 a binary float multiply produces. The overrides ladder
    becomes tiers ordered cheapest threshold first, since a transcript is
    exactly the kind of prompt that crosses one."""
    result = await _probe(serving(CHAT_BODY))
    luna = result.find("openai/gpt-5.6-luna")
    sonnet = result.find("anthropic/claude-sonnet-4.5")
    assert luna is not None and sonnet is not None
    assert luna.pricing == VendorPrice(prompt=3.0, completion=15.0)
    assert luna.price_tiers == ()
    assert [t.min_prompt_tokens for t in sonnet.price_tiers] == [200000, 500000]
    assert (sonnet.price_tiers[0].prompt, sonnet.price_tiers[0].completion) == (6.0, 22.5)
    assert sonnet.accepts_reasoning_effort is False
    assert sonnet.retires_on == date(2098, 12, 31)


async def test_the_transcription_list_reports_no_price() -> None:
    """Audio models are priced per unit of audio, not per token, and the
    catalogue does not say which unit: measured against the live API,
    deepgram/nova-3's "0.0043" bills per minute while nvidia/nemotron-3.5-asr's
    "0.00000333" bills per second. Treating either as a per-token rate produced
    "$4300 / $0" in the picker. No price beats a wrong one."""
    result = await _probe(serving(TRANSCRIBE_BODY), interaction=Interaction.TRANSCRIBE)
    model = result.find("deepgram/nova-3")
    assert model is not None
    assert model.pricing is None


# --------------------------------------------------------------------------
# OpenRouter, video list
# --------------------------------------------------------------------------


async def test_the_video_catalogue_keeps_null_apart_from_false() -> None:
    """``generate_audio: null`` is the vendor declining to answer, while
    ``supported_frame_images: null`` is it saying this model takes no frame
    image (which is where the yaml's image_input: false came from). The video
    dialog flattens the first to False for its form; the reader must not, or
    every model with an unstated audio capability would look like drift."""
    result = await _probe(serving(VIDEO_BODY), interaction=Interaction.VIDEO)
    sora = result.find("openai/sora-2-pro")
    assert sora is not None and sora.video is not None
    assert sora.video.audio is None
    assert sora.video.seed is None
    assert sora.video.image_input is False
    assert sora.video.durations == (4, 8, 12)
    assert sora.name is None
    wan = result.find("alibaba/wan-3.0")
    assert wan is not None and wan.video is not None
    assert (wan.name, wan.video.audio, wan.video.seed) == ("Wan 3.0", True, True)
    # Absent entirely: the vendor published no value, not an empty list.
    assert wan.video.image_input is None
    assert wan.video.sizes is None


# --------------------------------------------------------------------------
# OpenAI, and anything claiming compatibility
# --------------------------------------------------------------------------


async def test_the_openai_catalogue_carries_ids_and_shutdown_dates_only() -> None:
    result = await _probe(
        serving(OPENAI_BODY),
        kind=ProviderKind.OPENAI,
        interaction=Interaction.TRANSCRIBE,
        api_key="k",
    )
    assert result.status is CatalogStatus.OK
    assert [m.id for m in result.models] == [
        "gpt-4o",
        "whisper-1",
        "gpt-transcribe",
        "whisper-weird",
    ]
    dated = result.find("gpt-transcribe")
    assert dated is not None and dated.retires_on == date(2027, 3, 1)
    plain = result.find("whisper-1")
    assert plain is not None
    assert plain.publishes_reasoning is False
    assert (plain.context_length, plain.pricing, plain.reasoning) == (None, None, None)
    # Prices are never read from this shape, even where a row carries junk.
    weird = result.find("whisper-weird")
    assert weird is not None and weird.pricing is None


async def test_a_self_hosted_server_may_answer_with_a_bare_list_of_ids() -> None:
    """This runs against anything claiming OpenAI compatibility, and some
    servers list nothing but names."""
    result = await _probe(
        serving(["whisper-1", "parakeet"]),
        kind=ProviderKind.OPENAI_COMPAT,
        interaction=Interaction.TRANSCRIBE,
        base_url="http://speaches:8000/v1",
    )
    assert result.status is CatalogStatus.OK
    assert result.endpoint == "http://speaches:8000/v1/models"
    assert [m.id for m in result.models] == ["whisper-1", "parakeet"]


# --------------------------------------------------------------------------
# Deepgram and Gemini
# --------------------------------------------------------------------------


async def test_deepgram_is_read_from_its_stt_half_under_both_names() -> None:
    result = await _probe(
        serving(DEEPGRAM_BODY),
        kind=ProviderKind.DEEPGRAM,
        interaction=Interaction.TRANSCRIBE,
        api_key="k",
    )
    assert result.status is CatalogStatus.OK
    assert result.lists("nova-3") and result.lists("nova-3-general")
    assert not result.lists("aura-2")


async def test_gemini_ids_lose_their_prefix_and_a_paged_answer_is_partial() -> None:
    """Gemini pages its catalogue. A page proves what it contains and nothing
    about what it does not, so the flag travels with the answer."""
    result = await _probe(
        serving(GEMINI_BODY),
        kind=ProviderKind.GEMINI,
        interaction=Interaction.TRANSCRIBE,
        api_key="k",
    )
    assert result.status is CatalogStatus.OK
    assert result.partial is True
    assert result.lists("gemini-3.5-transcribe")


# --------------------------------------------------------------------------
# The request itself: the surface's address and credential spelling
# --------------------------------------------------------------------------


async def test_the_public_catalogue_is_read_without_a_key() -> None:
    """Verified against the live endpoint on 2026-09-02: OpenRouter's
    /api/v1/models answers unauthenticated, which is what lets the CI check run
    with no secret at all."""
    seen: list[str | None] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("Authorization"))
        return httpx.Response(200, json=CHAT_BODY)

    result = await _probe(httpx.MockTransport(handle))
    assert result.status is CatalogStatus.OK
    assert seen == [None]


@pytest.mark.parametrize(
    ("kind", "interaction", "url", "header"),
    [
        (
            ProviderKind.DEEPGRAM,
            Interaction.TRANSCRIBE,
            "https://api.deepgram.com/v1/models",
            ("Authorization", "Token k"),
        ),
        (
            ProviderKind.GEMINI,
            Interaction.SUMMARIZE,
            "https://generativelanguage.googleapis.com/v1beta/models",
            ("x-goog-api-key", "k"),
        ),
        (
            ProviderKind.OPENAI,
            Interaction.SUMMARIZE,
            "https://api.openai.com/v1/models",
            ("Authorization", "Bearer k"),
        ),
        (
            ProviderKind.OPENROUTER,
            Interaction.VIDEO,
            "https://openrouter.ai/api/v1/videos/models",
            ("Authorization", "Bearer k"),
        ),
    ],
)
async def test_the_key_is_spelled_the_way_the_surface_declares(
    kind: ProviderKind, interaction: Interaction, url: str, header: tuple[str, str]
) -> None:
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"data": []})

    await _probe(httpx.MockTransport(handle), kind=kind, interaction=interaction, api_key="k")
    assert [str(r.url) for r in seen] == [url]
    assert seen[0].headers.get(header[0]) == header[1]


# --------------------------------------------------------------------------
# Every way a vendor can fail. None may raise, none may claim a model is gone.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "error",
    [
        httpx.ConnectError("connection refused"),
        httpx.ReadTimeout("timed out"),
        httpx.RemoteProtocolError("server disconnected"),
    ],
)
async def test_a_vendor_that_cannot_be_reached_is_a_status_not_an_exception(
    error: httpx.HTTPError,
) -> None:
    def handle(_request: httpx.Request) -> httpx.Response:
        raise error

    result = await _probe(httpx.MockTransport(handle))
    assert result.status is CatalogStatus.UNREACHABLE
    assert result.usable is False
    assert result.models == ()
    assert "could not check" in result.detail


@pytest.mark.parametrize("status", [401, 429, 500, 503])
async def test_an_http_error_is_a_status_not_an_exception(status: int) -> None:
    """A rate limit and an outage look the same from here, and neither may fail
    a build."""
    result = await _probe(serving({"data": []}, status=status))
    assert result.status is CatalogStatus.UNREACHABLE


async def test_a_body_that_is_not_json_is_unreadable() -> None:
    transport = httpx.MockTransport(lambda _r: httpx.Response(200, text="<html>maintenance</html>"))
    result = await _probe(transport)
    assert result.status is CatalogStatus.UNREADABLE
    assert result.models == ()


async def test_a_response_shape_that_moved_is_unreadable() -> None:
    """The envelope key changing is the failure mode this whole status exists
    for: the request succeeded, so nothing else would notice."""
    result = await _probe(serving({"models": [{"id": "openai/gpt-5.6-luna"}]}))
    assert result.status is CatalogStatus.UNREADABLE
    assert "no data list" in result.detail


async def test_rows_that_carry_no_ids_are_unreadable() -> None:
    result = await _probe(serving({"data": [{"slug": "openai/gpt-5.6-luna"}]}))
    assert result.status is CatalogStatus.UNREADABLE


async def test_an_empty_catalogue_is_unreadable_not_an_empty_world() -> None:
    """The single most dangerous response there is. No vendor here serves an
    empty list, so parsing one means the shape moved, and reading it as truth
    would report every curated model as retired at once."""
    result = await _probe(serving({"data": []}))
    assert result.status is CatalogStatus.UNREADABLE
    assert result.usable is False


async def test_a_vendor_needing_a_key_is_skipped_without_calling_it() -> None:
    called: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        called.append(str(request.url))
        return httpx.Response(200, json={"data": []})

    result = await _probe(
        httpx.MockTransport(handle), kind=ProviderKind.OPENAI, interaction=Interaction.TRANSCRIBE
    )
    assert result.status is CatalogStatus.NO_CREDENTIALS
    assert "OPENAI_API_KEY" in result.detail
    assert called == []


async def test_a_vendor_with_no_catalogue_at_all_is_skipped() -> None:
    result = await _probe(
        serving({"data": []}),
        kind=ProviderKind.ASSEMBLYAI,
        interaction=Interaction.TRANSCRIBE,
        api_key="k",
    )
    assert result.status is CatalogStatus.NO_CATALOGUE


async def test_the_self_hosted_kind_says_whose_server_it_would_need() -> None:
    """Its endpoint is a template over a base URL only the operator knows, so
    a probe with no row cannot call it and says so rather than reporting
    nothing."""
    result = await _probe(
        serving({"data": []}),
        kind=ProviderKind.OPENAI_COMPAT,
        interaction=Interaction.TRANSCRIBE,
    )
    assert result.status is CatalogStatus.NO_CATALOGUE
    assert "operator" in result.detail


def test_the_shipped_file_declares_which_catalogues_a_picker_may_read() -> None:
    """The gate on live listing is data beside the surface, not a kind set in
    the picker: Deepgram's and Gemini's lists are read to check the curated
    models and never offered as published."""
    live = {
        kind.value
        for kind, spec in config().providers.items()
        for interaction in spec.interactions
        if (catalog := spec.catalog(interaction)) is not None and catalog.picker
    }
    checked_only = {
        kind.value
        for kind, spec in config().providers.items()
        for interaction in spec.interactions
        if (catalog := spec.catalog(interaction)) is not None and not catalog.picker
    }
    assert live == {"openai", "openai_compat", "openrouter"}
    assert checked_only == {"deepgram", "gemini"}
