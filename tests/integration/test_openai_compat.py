"""Integration test: OpenAI-compatible backend against the mock server."""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx

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
        model="whisper-1",
    )
    return OpenAICompatBackend(config, client=client, language="de")


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
