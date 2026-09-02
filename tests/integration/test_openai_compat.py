"""Integration test: OpenAI-compatible backend against the mock server."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable

import httpx
import pytest

from loreline.audio.chunker import Utterance
from loreline.models import Glossary, Protocol, ProviderConfig, ProviderKind, TranscriptEvent
from loreline.stt.backends.openai_compat import OpenAICompatBackend
from mocks.openai_compat import create_app


def _backend(client: httpx.AsyncClient) -> OpenAICompatBackend:
    config = ProviderConfig(
        id="speaches-1",
        name="Speaches LAN",
        kind=ProviderKind.OPENAI_COMPAT,
        base_url="http://mock/v1",
        protocol=Protocol.HTTP_BATCH,
    )
    return OpenAICompatBackend(config, model="whisper-1", client=client, language="de")


async def _utterances(items: list[Utterance]) -> AsyncIterator[Utterance]:
    for item in items:
        yield item


async def test_transcribe_yields_final_events() -> None:
    transport = httpx.ASGITransport(app=create_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://mock/v1") as client:
        backend = _backend(client)
        utterances = [
            Utterance(pcm=b"\x01\x00" * 1600, start=0.0, end=0.1),
            Utterance(pcm=b"\x02\x00" * 3200, start=0.5, end=0.7),
        ]
        events: list[TranscriptEvent] = [
            event async for event in backend.transcribe(_utterances(utterances), session_id="s1")
        ]

    assert len(events) == 2
    assert all(e.is_final for e in events)
    assert events[0].session_id == "s1"
    assert events[0].source == "speaches-1"
    assert "mock transcription" in events[0].text
    assert events[1].start_ts == 0.5


async def test_transcribe_passes_glossary_prompt() -> None:
    transport = httpx.ASGITransport(app=create_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://mock/v1") as client:
        backend = _backend(client)
        glossary = Glossary(campaign_id="c1", terms=["Drakonia", "Thalric"])
        utterances = [Utterance(pcm=b"\x01\x00" * 1600, start=0.0, end=0.1)]
        events = [
            e
            async for e in backend.transcribe(
                _utterances(utterances), session_id="s1", glossary=glossary
            )
        ]

    assert "prompt: Drakonia, Thalric" in events[0].text


async def test_health_ok() -> None:
    transport = httpx.ASGITransport(app=create_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://mock/v1") as client:
        backend = _backend(client)
        assert await backend.health() is True


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
        protocol=Protocol.HTTP_BATCH,
        base_url="http://stt:8000/v1",
        sample_rate=_SAMPLE_RATE,
    )


def _mock_backend(
    handler: Callable[[httpx.Request], httpx.Response],
) -> OpenAICompatBackend:
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport, base_url="http://stt:8000/v1")
    return OpenAICompatBackend(_verbose_config(), client=client)


async def _single_utterance(
    pcm: bytes = b"\x00\x00" * 800, start: float = 10.0
) -> AsyncIterator[Utterance]:
    yield Utterance(pcm=pcm, start=start, end=start + 0.1)


async def _collect_events(backend: OpenAICompatBackend) -> list[TranscriptEvent]:
    return [e async for e in backend.transcribe(_single_utterance(), session_id="s1")]


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

    await _collect_events(_mock_backend(handle))
    assert "verbose_json" in seen["body"]
    assert "timestamp_granularities[]" in seen["body"]
    assert "word" in seen["body"]


async def test_word_timings_and_speakers_reach_the_event() -> None:
    def handle(_r: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_VERBOSE_BODY)

    events = await _collect_events(_mock_backend(handle))
    assert len(events) == 1
    event = events[0]
    assert event.text == "hello there"  # trimmed
    assert [w.text for w in event.words] == ["hello", "there"]
    # Clip-relative timings are shifted onto the session clock.
    assert event.words[0].start == 10.0
    assert event.words[1].start == 10.5
    assert [w.speaker for w in event.words] == ["Speaker 0", "Speaker 1"]
    # The event's own speaker is its first labelled word, as elsewhere.
    assert event.speaker == "Speaker 0"


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

    events = await _collect_events(_mock_backend(handle))
    assert [w.speaker for w in events[0].words] == ["Speaker 2", "Speaker 3"]
    assert events[0].words[0].start == 10.0


async def test_a_plain_json_body_still_works() -> None:
    """Whisper-class models return no structure at all; the event must come
    through exactly as it did before any of this existed."""

    def handle(_r: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"text": "just text"})

    events = await _collect_events(_mock_backend(handle))
    assert events[0].text == "just text"
    assert events[0].words == []
    assert events[0].speaker is None


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
    first = await _collect_events(backend)
    assert first[0].text == "fallback worked"
    assert formats == ["verbose_json", "json"]

    # The downgrade sticks: the second utterance goes straight to json.
    second = [e async for e in backend.transcribe(_single_utterance(), session_id="s1")]
    assert second[0].text == "fallback worked"
    assert formats == ["verbose_json", "json", "json"]


async def test_a_real_error_still_raises() -> None:
    """The fallback must not swallow a genuine failure (bad key, bad model)."""

    def handle(_r: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"message": "invalid api key"}})

    with pytest.raises(httpx.HTTPStatusError, match="invalid api key"):
        await _collect_events(_mock_backend(handle))
