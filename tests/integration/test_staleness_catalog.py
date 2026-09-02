"""Fetching and reading vendor catalogues, with every failure mode exercised.

No test here touches a real vendor: every response is served by an
``httpx.MockTransport``, including the ones that fail. That is deliberate for
more than speed. The behaviour worth guarding is what happens when a vendor is
down, slow, rate limiting, or has quietly changed the shape of its response,
and none of those can be arranged against a live API on demand.
"""

from __future__ import annotations

import httpx
import pytest

from loreline.capabilities import config
from loreline.models import Interaction, ProviderKind
from loreline.staleness.catalog import CatalogStatus, probe
from loreline.staleness.check import gather_probes, run_check
from loreline.staleness.compare import compare

# One realistic OpenRouter chat row, trimmed to the fields the check reads.
_CHAT_BODY: dict[str, object] = {
    "data": [
        {
            "id": "openai/gpt-5.6-luna",
            "context_length": 1050000,
            "top_provider": {"context_length": 1050000, "max_completion_tokens": 128000},
            "supported_parameters": ["max_tokens", "reasoning", "response_format"],
            "reasoning": {
                "mandatory": False,
                "supported_efforts": ["max", "xhigh", "high", "medium", "low", "none"],
                "default_effort": "medium",
            },
            "expiration_date": None,
        }
    ]
}

_VIDEO_BODY: dict[str, object] = {
    "data": [
        {
            "id": "openai/sora-2-pro",
            "supported_durations": [4, 8, 12],
            "supported_resolutions": ["720p", "1080p"],
            "supported_aspect_ratios": ["16:9", "9:16"],
            "supported_frame_images": None,
            "generate_audio": None,
        }
    ]
}


def _factory(handler: httpx.MockTransport) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=handler)


def _serving(body: object, status: int = 200) -> httpx.MockTransport:
    return httpx.MockTransport(lambda _r: httpx.Response(status, json=body))


def _spec(kind: ProviderKind = ProviderKind.OPENROUTER):  # type: ignore[no-untyped-def]
    spec = config().provider(kind)
    assert spec is not None
    return spec


async def _probe(
    transport: httpx.MockTransport,
    *,
    kind: ProviderKind = ProviderKind.OPENROUTER,
    interaction: Interaction = Interaction.SUMMARIZE,
    api_key: str | None = None,
):  # type: ignore[no-untyped-def]
    return await probe(
        kind,
        interaction,
        spec=_spec(kind),
        api_key=api_key,
        client_factory=lambda: _factory(transport),
    )


# --------------------------------------------------------------------------
# Reading a catalogue that answered
# --------------------------------------------------------------------------


async def test_the_chat_catalogue_yields_the_facts_the_yaml_mirrors() -> None:
    result = await _probe(_serving(_CHAT_BODY))
    assert result.status is CatalogStatus.OK
    model = result.find("openai/gpt-5.6-luna")
    assert model is not None
    assert (model.context_length, model.max_output_tokens) == (1050000, 128000)
    # temperature is absent from supported_parameters, so the vendor is saying
    # this model does not take it - which is what the yaml records for Luna.
    assert model.temperature is False
    assert model.reasoning is not None
    assert model.reasoning.mandatory is False
    assert "none" in model.reasoning.efforts


async def test_the_video_catalogue_keeps_null_apart_from_false() -> None:
    """``generate_audio: null`` is the vendor declining to answer, while
    ``supported_frame_images: null`` is it saying this model takes no frame
    image (which is where the yaml's image_input: false came from). The runtime
    parser flattens the first to False; this one must not, or every model with
    an unstated audio capability would look like drift."""
    result = await _probe(_serving(_VIDEO_BODY), interaction=Interaction.VIDEO)
    model = result.find("openai/sora-2-pro")
    assert model is not None and model.video is not None
    assert model.video.audio is None
    assert model.video.image_input is False
    assert model.video.durations == (4, 8, 12)


async def test_the_public_catalogue_is_read_without_a_key() -> None:
    """Verified against the live endpoint on 2026-09-02: OpenRouter's
    /api/v1/models answers unauthenticated, which is what lets the CI check run
    with no secret at all."""
    seen: list[str | None] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("Authorization"))
        return httpx.Response(200, json=_CHAT_BODY)

    result = await _probe(httpx.MockTransport(handle))
    assert result.status is CatalogStatus.OK
    assert seen == [None]


async def test_a_paginated_answer_is_flagged_as_partial() -> None:
    """Gemini pages its catalogue. A page proves what it contains and nothing
    about what it does not, so the flag travels with the answer."""
    body = {"models": [{"name": "models/gemini-3.5-transcribe"}], "nextPageToken": "more"}
    result = await _probe(
        _serving(body), kind=ProviderKind.GEMINI, interaction=Interaction.TRANSCRIBE, api_key="k"
    )
    assert result.status is CatalogStatus.OK
    assert result.partial is True
    assert result.lists("gemini-3.5-transcribe")  # the "models/" prefix is stripped


async def test_deepgram_is_read_from_its_stt_half_under_both_names() -> None:
    body = {
        "stt": [{"name": "nova-3", "canonical_name": "nova-3-general"}],
        "tts": [{"name": "aura-2"}],
    }
    result = await _probe(
        _serving(body), kind=ProviderKind.DEEPGRAM, interaction=Interaction.TRANSCRIBE, api_key="k"
    )
    assert result.status is CatalogStatus.OK
    assert result.lists("nova-3") and result.lists("nova-3-general")
    assert not result.lists("aura-2")


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
    assert result.models == ()
    assert "could not check" in result.detail


@pytest.mark.parametrize("status", [401, 429, 500, 503])
async def test_an_http_error_is_a_status_not_an_exception(status: int) -> None:
    """A rate limit and an outage look the same from here, and neither may fail
    a build."""
    result = await _probe(_serving({"data": []}, status=status))
    assert result.status is CatalogStatus.UNREACHABLE


async def test_a_body_that_is_not_json_is_unreadable() -> None:
    transport = httpx.MockTransport(lambda _r: httpx.Response(200, text="<html>maintenance</html>"))
    result = await _probe(transport)
    assert result.status is CatalogStatus.UNREADABLE
    assert result.models == ()


async def test_a_response_shape_that_moved_is_unreadable() -> None:
    """The envelope key changing is the failure mode this whole status exists
    for: the request succeeded, so nothing else would notice."""
    result = await _probe(_serving({"models": [{"id": "openai/gpt-5.6-luna"}]}))
    assert result.status is CatalogStatus.UNREADABLE
    assert "no data list" in result.detail


async def test_rows_that_carry_no_ids_are_unreadable() -> None:
    result = await _probe(_serving({"data": [{"slug": "openai/gpt-5.6-luna"}]}))
    assert result.status is CatalogStatus.UNREADABLE


async def test_an_empty_catalogue_is_unreadable_not_an_empty_world() -> None:
    """The single most dangerous response there is. No vendor here serves an
    empty list, so parsing one means the shape moved - and reading it as truth
    would report every curated model as retired at once."""
    result = await _probe(_serving({"data": []}))
    assert result.status is CatalogStatus.UNREADABLE
    assert compare(config(), [result]) == []


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
        _serving({"data": []}),
        kind=ProviderKind.ASSEMBLYAI,
        interaction=Interaction.TRANSCRIBE,
        api_key="k",
    )
    assert result.status is CatalogStatus.NO_CATALOGUE


async def test_the_self_hosted_kind_says_whose_server_it_would_need() -> None:
    """Its endpoint is a template over a base URL only the operator knows, so
    the CI check cannot call it and says so rather than reporting nothing."""
    result = await _probe(
        _serving({"data": []}),
        kind=ProviderKind.OPENAI_COMPAT,
        interaction=Interaction.TRANSCRIBE,
    )
    assert result.status is CatalogStatus.NO_CATALOGUE
    assert "operator" in result.detail


# --------------------------------------------------------------------------
# The whole run
# --------------------------------------------------------------------------


async def test_one_url_serving_two_interactions_is_fetched_once() -> None:
    """OpenAI's /v1/models is the catalogue for transcription and for
    summarization alike. Asking twice would double the load on a vendor for an
    identical answer."""
    calls: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, json={"data": [{"id": "gpt-transcribe"}]})

    probes = await gather_probes(
        config(),
        credentials={ProviderKind.OPENAI: "k"},
        client_factory=lambda: _factory(httpx.MockTransport(handle)),
    )
    openai = [p for p in probes if p.kind is ProviderKind.OPENAI]
    assert {p.interaction for p in openai} == {Interaction.TRANSCRIBE, Interaction.SUMMARIZE}
    assert all(p.status is CatalogStatus.OK for p in openai)
    assert len([c for c in calls if "api.openai.com" in c]) == 1


async def test_a_run_where_every_vendor_is_down_reports_no_drift() -> None:
    """The requirement, end to end: an offline CI machine produces a report
    full of "not checked" and not one claim about a model."""

    def handle(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    report = await run_check(
        credentials=dict.fromkeys(ProviderKind, "k"),
        client_factory=lambda: _factory(httpx.MockTransport(handle)),
    )
    assert report.probes
    assert all(not p.usable for p in report.probes)
    assert not [f for f in report.findings if f.code.value.startswith("model.")]


async def test_the_offline_run_asks_nobody() -> None:
    report = await run_check(offline=True)
    assert report.probes == ()
    # Whatever it finds, it can only have come from the file's own dates.
    assert all(f.fact == "deprecated" for f in report.findings)
