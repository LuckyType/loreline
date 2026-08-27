"""Integration tests for the OpenAI Realtime backend via a mock WS server."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import pytest
from websockets.asyncio.server import ServerConnection, serve

from loreline.audio.chunker import Utterance
from loreline.audio.resample import resample_pcm16
from loreline.models import Glossary, Protocol, ProviderConfig, ProviderKind, TranscriptEvent
from loreline.stt.backends.openai_realtime import OpenAIRealtimeBackend
from mocks.openai_realtime_ws import openai_realtime_handler

_COMPLETED_TYPE = "conversation.item.input_audio_transcription.completed"


async def _one() -> AsyncIterator[Utterance]:
    yield Utterance(pcm=b"\x01\x00" * 480, start=2.0, end=2.5)


async def _utt(index: int) -> AsyncIterator[Utterance]:
    yield Utterance(pcm=b"\x01\x00" * 480, start=float(index), end=index + 0.5)


async def test_realtime_transcribe_via_mock() -> None:
    async with serve(openai_realtime_handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        config = ProviderConfig(
            id="oai-1",
            name="OpenAI",
            kind=ProviderKind.OPENAI,
            base_url=f"ws://127.0.0.1:{port}",
            protocol=Protocol.WS,
            sample_rate=24000,  # == realtime output rate -> no resample (no numpy needed)
        )
        backend = OpenAIRealtimeBackend(config, api_key="secret")
        events: list[TranscriptEvent] = [
            e async for e in backend.transcribe(_one(), session_id="s1")
        ]

    assert len(events) == 1
    assert events[0].is_final
    assert "openai realtime mock" in events[0].text
    assert events[0].start_ts == 2.0
    assert events[0].source == "oai-1"


async def test_realtime_reuses_one_connection_across_utterances() -> None:
    connections = 0

    async def counting_handler(ws: ServerConnection) -> None:
        nonlocal connections
        connections += 1
        await openai_realtime_handler(ws)

    async with serve(counting_handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        config = ProviderConfig(
            id="oai-1",
            name="OpenAI",
            kind=ProviderKind.OPENAI,
            base_url=f"ws://127.0.0.1:{port}",
            protocol=Protocol.WS,
            sample_rate=24000,
        )
        backend = OpenAIRealtimeBackend(config, api_key="secret")
        events: list[TranscriptEvent] = []
        for i in range(3):
            async for event in backend.transcribe(_utt(i), session_id="s"):
                events.append(event)
        await backend.aclose()

    assert len(events) == 3
    assert connections == 1  # one WebSocket reused for all three utterances


async def test_realtime_applies_glossary_prompt() -> None:
    captured: dict[str, str] = {}

    async def handler(ws: ServerConnection) -> None:
        async for message in ws:
            if isinstance(message, bytes):
                continue
            data = json.loads(message)
            if data.get("type") == "session.update":
                captured["raw"] = message
            elif data.get("type") == "input_audio_buffer.commit":
                await ws.send(json.dumps({"type": _COMPLETED_TYPE, "transcript": "ok"}))

    async with serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        config = ProviderConfig(
            id="oai",
            name="OpenAI",
            kind=ProviderKind.OPENAI,
            base_url=f"ws://127.0.0.1:{port}",
            protocol=Protocol.WS,
            sample_rate=24000,
        )
        backend = OpenAIRealtimeBackend(config, api_key="x")
        glossary = Glossary(campaign_id="c", terms=["Drakonia", "Mistwood"])
        events = [e async for e in backend.transcribe(_one(), session_id="s", glossary=glossary)]
        await backend.aclose()

    assert events
    raw = captured["raw"]  # the session.update sent at connection time
    assert '"prompt"' in raw
    assert "Drakonia" in raw
    assert "Mistwood" in raw


async def test_realtime_health_ok() -> None:
    async with serve(openai_realtime_handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        config = ProviderConfig(
            id="oai",
            name="OpenAI",
            kind=ProviderKind.OPENAI,
            base_url=f"ws://127.0.0.1:{port}",
            protocol=Protocol.WS,
            sample_rate=24000,
        )
        assert await OpenAIRealtimeBackend(config, api_key="secret").health() is True


async def test_realtime_health_false_when_unreachable() -> None:
    config = ProviderConfig(
        id="oai",
        name="OpenAI",
        kind=ProviderKind.OPENAI,
        base_url="ws://127.0.0.1:1",
        protocol=Protocol.WS,
        sample_rate=24000,
    )
    assert await OpenAIRealtimeBackend(config, api_key="x").health() is False


def test_resample_pcm16_upsamples_16k_to_24k() -> None:
    pytest.importorskip("numpy")
    pcm = b"\x00\x10" * 160  # 160 samples @ 16k
    out = resample_pcm16(pcm, 16000, 24000)
    assert len(out) == len(pcm) * 24000 // 16000
    assert resample_pcm16(pcm, 24000, 24000) == pcm  # identity when rates match
