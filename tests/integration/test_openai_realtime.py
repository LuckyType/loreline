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
                await ws.send(json.dumps({"type": "session.updated"}))
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


async def test_realtime_prompt_capped_to_openai_limit() -> None:
    """A glossary past OpenAI's 1024-char prompt limit is truncated at a term
    boundary instead of being sent oversized - an oversized prompt is rejected
    server-side together with the whole session config."""
    captured: dict[str, str] = {}

    async def handler(ws: ServerConnection) -> None:
        async for message in ws:
            if isinstance(message, bytes):
                continue
            data = json.loads(message)
            if data.get("type") == "session.update":
                captured["raw"] = message
                await ws.send(json.dumps({"type": "session.updated"}))
            elif data.get("type") == "input_audio_buffer.commit":
                await ws.send(json.dumps({"type": _COMPLETED_TYPE, "transcript": "ok"}))

    terms = [f"Term{i:04d}xxxxxxxxxxxx" for i in range(100)]  # ~1800 chars joined
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
        glossary = Glossary(campaign_id="c", terms=terms)
        events = [e async for e in backend.transcribe(_one(), session_id="s", glossary=glossary)]
        await backend.aclose()

    assert events
    prompt = json.loads(captured["raw"])["session"]["audio"]["input"]["transcription"]["prompt"]
    assert len(prompt) <= 1024
    assert prompt.startswith(terms[0])
    assert not prompt.endswith(",")  # whole terms only, no mid-term cut
    assert prompt.split(", ")[-1] in terms


async def test_realtime_prompt_rejection_downgrades_to_promptless() -> None:
    """A model that refuses the prompt param voids the whole session.update, so
    the backend must retry once without the prompt - keeping language/format -
    and still transcribe. The rejection is remembered across reconnects."""
    updates: list[str] = []

    async def handler(ws: ServerConnection) -> None:
        async for message in ws:
            if isinstance(message, bytes):
                continue
            data = json.loads(message)
            if data.get("type") == "session.update":
                updates.append(message)
                if '"prompt"' in message:
                    await ws.send(
                        json.dumps(
                            {
                                "type": "error",
                                "error": {
                                    "type": "invalid_request_error",
                                    "code": "invalid_value",
                                    "param": "session.audio.input.transcription.prompt",
                                    "message": "The 'prompt' parameter is not supported.",
                                },
                            }
                        )
                    )
                else:
                    await ws.send(json.dumps({"type": "session.updated"}))
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
        glossary = Glossary(campaign_id="c", terms=["Drakonia"])
        events: list[TranscriptEvent] = []
        for i in range(2):
            async for event in backend.transcribe(_utt(i), session_id="s", glossary=glossary):
                events.append(event)
        await backend.aclose()

    assert len(events) == 2  # no utterance was swallowed by the rejection
    assert len(updates) == 2  # with-prompt attempt, then the promptless retry
    assert '"prompt"' in updates[0]
    assert '"prompt"' not in updates[1]
    assert '"language"' in updates[1]  # rest of the session config kept


def test_resample_pcm16_upsamples_16k_to_24k() -> None:
    pytest.importorskip("numpy")
    pcm = b"\x00\x10" * 160  # 160 samples @ 16k
    out = resample_pcm16(pcm, 16000, 24000)
    assert len(out) == len(pcm) * 24000 // 16000
    assert resample_pcm16(pcm, 24000, 24000) == pcm  # identity when rates match
