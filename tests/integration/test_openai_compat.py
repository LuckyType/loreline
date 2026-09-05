"""Integration test: OpenAI-compatible backend against the mock server."""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest
from fastapi import FastAPI

from loreline.audio.chunker import Utterance
from loreline.catalog import ClientFactory
from loreline.models import Glossary, ProviderConfig, ProviderKind, TranscriptEvent
from loreline.stt.backends.openai_compat import OpenAICompatBackend
from mocks.openai_compat import create_app


def _backend(client: httpx.AsyncClient) -> OpenAICompatBackend:
    config = ProviderConfig(
        id="speaches-1",
        name="Speaches LAN",
        kind=ProviderKind.OPENAI_COMPAT,
        base_url="http://mock/v1",
    )
    return OpenAICompatBackend(config, model="whisper-1", client=client, language="de")


async def test_transcribe_yields_final_events() -> None:
    transport = httpx.ASGITransport(app=create_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://mock/v1") as client:
        backend = _backend(client)
        utterances = [
            Utterance(pcm=b"\x01\x00" * 1600, start=0.0, end=0.1),
            Utterance(pcm=b"\x02\x00" * 3200, start=0.5, end=0.7),
        ]
        events = [await backend.transcribe(utterance, session_id="s1") for utterance in utterances]

    assert all(e is not None for e in events)
    first, second = events
    assert first is not None and second is not None
    assert first.is_final and second.is_final
    assert first.session_id == "s1"
    assert first.source == "speaches-1"
    assert "mock transcription" in first.text
    assert second.start_ts == 0.5


async def test_transcribe_passes_glossary_prompt() -> None:
    transport = httpx.ASGITransport(app=create_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://mock/v1") as client:
        backend = _backend(client)
        glossary = Glossary(campaign_id="c1", terms=["Drakonia", "Thalric"])
        utterance = Utterance(pcm=b"\x01\x00" * 1600, start=0.0, end=0.1)
        event = await backend.transcribe(utterance, session_id="s1", glossary=glossary)

    assert event is not None
    assert "prompt: Drakonia, Thalric" in event.text


# --- verbose_json: word timings and speaker labels -----------------------
#
# Reading only `text` silently discarded diarization for every model that
# produces it (e.g. OpenRouter's x-ai/grok-stt-1.0). These cover the richer
# body, and the fallback for servers that do not implement it.

_SAMPLE_RATE = 16000


def _verbose_config() -> ProviderConfig:
    return ProviderConfig(
        id="c1",
        name="Compat",
        kind=ProviderKind.OPENAI_COMPAT,
        base_url="http://stt:8000/v1",
        sample_rate=_SAMPLE_RATE,
    )


def _mock_backend(
    handler: Callable[[httpx.Request], httpx.Response],
) -> OpenAICompatBackend:
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport, base_url="http://stt:8000/v1")
    return OpenAICompatBackend(_verbose_config(), model="whisper-1", client=client)


def _single_utterance(pcm: bytes = b"\x00\x00" * 800, start: float = 10.0) -> Utterance:
    return Utterance(pcm=pcm, start=start, end=start + 0.1)


async def _transcribed(backend: OpenAICompatBackend) -> TranscriptEvent | None:
    return await backend.transcribe(_single_utterance(), session_id="s1")


_VERBOSE_BODY = {
    "text": "  hello there  ",
    "words": [
        {"word": "hello", "start": 0.0, "end": 0.4, "speaker": 0},
        {"word": "there", "start": 0.5, "end": 0.9, "speaker": 1},
    ],
}


async def test_requests_verbose_json_with_word_granularity() -> None:
    """Plain `json` returns only `text`; the words array needs both the format
    and the granularity field."""
    seen: dict[str, str] = {}

    def handle(request: httpx.Request) -> httpx.Response:
        seen["body"] = request.content.decode("utf-8", "replace")
        return httpx.Response(200, json=_VERBOSE_BODY)

    await _transcribed(_mock_backend(handle))
    assert "verbose_json" in seen["body"]
    assert "timestamp_granularities[]" in seen["body"]
    assert "word" in seen["body"]


async def test_word_timings_and_speakers_reach_the_event() -> None:
    def handle(_r: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_VERBOSE_BODY)

    event = await _transcribed(_mock_backend(handle))
    assert event is not None
    assert event.text == "hello there"  # trimmed
    assert [w.text for w in event.words] == ["hello", "there"]
    # Clip-relative timings are shifted onto the session clock.
    assert event.words[0].start == 10.0
    assert event.words[1].start == 10.5
    assert [w.speaker for w in event.words] == ["Speaker 0", "Speaker 1"]


async def test_segments_are_used_when_there_are_no_words() -> None:
    """Coarser, but it still preserves speaker changes - the part diarization
    actually needs."""

    def handle(_r: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "text": "one two",
                "segments": [
                    {"text": "one", "start": 0.0, "end": 1.0, "speaker": 2},
                    {"text": "two", "start": 1.0, "end": 2.0, "speaker": 3},
                ],
            },
        )

    event = await _transcribed(_mock_backend(handle))
    assert event is not None
    assert [w.speaker for w in event.words] == ["Speaker 2", "Speaker 3"]
    assert event.words[0].start == 10.0


async def test_a_plain_json_body_still_works() -> None:
    """Whisper-class models return no structure at all; the event must come
    through exactly as it did before any of this existed."""

    def handle(_r: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"text": "just text"})

    event = await _transcribed(_mock_backend(handle))
    assert event is not None
    assert event.text == "just text"
    assert event.words == []


async def test_an_endpoint_rejecting_verbose_json_falls_back_once() -> None:
    """A self-hosted server that does not implement verbose_json must not lose
    the utterance - and must not pay the retry on every one after."""
    formats: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        body = request.content.decode("utf-8", "replace")
        fmt = "verbose_json" if "verbose_json" in body else "json"
        formats.append(fmt)
        if fmt == "verbose_json":
            return httpx.Response(400, json={"error": {"message": "unsupported format"}})
        return httpx.Response(200, json={"text": "fallback worked"})

    backend = _mock_backend(handle)
    first = await _transcribed(backend)
    assert first is not None and first.text == "fallback worked"
    assert formats == ["verbose_json", "json"]

    # The downgrade sticks: the second utterance goes straight to json.
    second = await _transcribed(backend)
    assert second is not None and second.text == "fallback worked"
    assert formats == ["verbose_json", "json", "json"]


async def test_a_real_error_still_raises() -> None:
    """The fallback must not swallow a genuine failure (bad key, bad model)."""

    def handle(_r: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"message": "invalid api key"}})

    with pytest.raises(httpx.HTTPStatusError, match="invalid api key"):
        await _transcribed(_mock_backend(handle))


# --- a provider row that names no model ----------------------------------
#
# Leaving the field out was the old answer, on the theory that a server with
# one model loaded transcribes with it regardless. Speaches, the self-hosted
# server this repo ships a compose service for, answers such a request
# `422 {"type": "missing", "loc": ["body", "model"], "msg": "Field required"}`
# with exactly one model loaded, so the server is asked what it has instead.


def _selfhosted_config() -> ProviderConfig:
    return ProviderConfig(
        id="speaches-1",
        name="Speaches LAN",
        kind=ProviderKind.OPENAI_COMPAT,
        base_url="http://mock/v1",
    )


def _catalog_factory(app: FastAPI, opened: list[str] | None = None) -> ClientFactory:
    """A fresh client per catalogue read: the reader closes what it opens, and
    closing the connector's own client would end the run."""

    def factory() -> httpx.AsyncClient:
        if opened is not None:
            opened.append("read")
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://mock/v1")

    return factory


async def _transcribe_against(
    app: FastAPI, *, opened: list[str] | None = None, times: int = 1
) -> TranscriptEvent | None:
    """One connector with no configured model, against this server."""
    transport = httpx.ASGITransport(app=app)
    event: TranscriptEvent | None = None
    async with httpx.AsyncClient(transport=transport, base_url="http://mock/v1") as client:
        backend = OpenAICompatBackend(
            _selfhosted_config(),
            client=client,
            language="de",
            catalog_client_factory=_catalog_factory(app, opened),
        )
        for _ in range(times):
            event = await backend.transcribe(_single_utterance(start=0.0), session_id="s1")
    return event


async def test_an_unset_model_is_resolved_from_the_server() -> None:
    """The model the server reports is the model the request carries. The mock
    echoes what it was sent, and requires the field exactly as Speaches does,
    so omitting it fails here now too."""
    event = await _transcribe_against(create_app(models=["Systran/faster-whisper-small"]))
    assert event is not None
    assert "Systran/faster-whisper-small" in event.text


async def test_the_server_is_asked_once_not_per_utterance() -> None:
    """A run is hours of utterances; the answer is a fact about the run, kept
    the way FeatureConflictGuard keeps its conflict groups."""
    opened: list[str] = []
    await _transcribe_against(create_app(), opened=opened, times=3)
    assert len(opened) == 1


async def test_several_loaded_models_take_the_first_transcription_one() -> None:
    """A self-hosted server lists everything it has loaded, and Speaches serves
    its TTS voices from the same endpoint, so the narrowing the picker applies
    to that list applies here too: the connector must not run a model the GM
    would never have been offered. The voice sorts ahead of both transcription
    models here, so taking the first of the raw list would pick it. Which of
    the two remaining is genuinely ambiguous, so the first is taken and the
    whole list is logged."""
    event = await _transcribe_against(
        create_app(
            models=[
                "hexgrad/Kokoro-82M",
                "openai/whisper-large-v3",
                "mobiuslabsgmbh/faster-whisper-large-v3-turbo",
            ]
        )
    )
    assert event is not None
    assert "mobiuslabsgmbh/faster-whisper-large-v3-turbo" in event.text
    assert "Kokoro" not in event.text


async def test_a_server_that_lists_no_model_fails_with_a_clear_message() -> None:
    """The failure this replaces was a 422 about a missing body field, raised
    from inside a transcription request, which says nothing about the provider
    row that is actually missing a model."""
    with pytest.raises(ValueError, match="no model configured"):
        await _transcribe_against(create_app(models=[]))
