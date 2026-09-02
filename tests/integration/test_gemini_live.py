"""Integration tests for the Gemini Live backend via a mock WS server.

The mock replays frames recorded from the real service (see
mocks/gemini_live_ws.py), including the parts the docs get wrong: turns end
with generationComplete, and the server never closes the session.
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from typing import cast

from websockets.asyncio.server import ServerConnection, serve

from loreline.audio.chunker import Utterance
from loreline.models import Glossary, Protocol, ProviderConfig, ProviderKind, TranscriptEvent
from loreline.stt.backends.gemini_live import (
    _RECV_TIMEOUT_S,  # pyright: ignore[reportPrivateUsage]
    GeminiLiveBackend,
)
from mocks.gemini_live_ws import gemini_live_handler

# 8000 samples is 0.5 s at 16 kHz, which the connector sends as five paced
# 100 ms chunks: enough send-side read windows for the mock's mid-stream turn
# to land in one of them, as it does in a real session.
_SAMPLES = 8000


async def _one_utterance() -> AsyncIterator[Utterance]:
    yield Utterance(pcm=b"\x01\x00" * _SAMPLES, start=10.0, end=10.5)


async def _two_utterances() -> AsyncIterator[Utterance]:
    yield Utterance(pcm=b"\x01\x00" * _SAMPLES, start=0.0, end=0.5)
    yield Utterance(pcm=b"\x02\x00" * _SAMPLES, start=0.5, end=1.0)


def _config(port: int, language: str = "de") -> ProviderConfig:
    return ProviderConfig(
        id="gem-live-1",
        name="Gemini Live",
        kind=ProviderKind.GEMINI,
        base_url=f"ws://127.0.0.1:{port}",
        protocol=Protocol.WS,
        model="gemini-3.5-transcribe-live",
        language=language,
    )


async def test_gemini_live_streaming_transcribe() -> None:
    async with serve(gemini_live_handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        backend = GeminiLiveBackend(_config(port), api_key="secret")
        glossary = Glossary(campaign_id="c1", terms=["Drakonia"])  # ignored, but must not break
        events: list[TranscriptEvent] = [
            e
            async for e in backend.transcribe(_one_utterance(), session_id="s1", glossary=glossary)
        ]

    assert len(events) == 1
    event = events[0]
    assert event.is_final
    assert event.source == "gem-live-1"
    # One final per turn, joined with a space: the finals carry no spacing of
    # their own, so concatenating them ran the turns together. Both turns are
    # present, which is the mid-stream turn being read during the paced send
    # rather than ending the session. Interims contribute nothing while their
    # turn finalizes, or the text would repeat "gemini".
    assert event.text == f"gemini live mock {_SAMPLES} samples"
    assert event.start_ts == 10.0
    assert event.end_ts == 10.5
    # Google: "Speaker diarization is not supported in live streaming
    # sessions" - no words, no speaker, ever.
    assert event.words == []
    assert event.speaker is None


async def test_session_ends_on_the_turn_end_not_on_the_timeout() -> None:
    """The defect this pins cost 10 s per utterance.

    The loop used to wait for turnComplete, which this service never sends,
    so every session ran until _RECV_TIMEOUT_S expired. The mock leaves the
    socket open after its last frame, exactly as the real service does, so
    only reading the turn end can get us out of here quickly.
    """
    async with serve(gemini_live_handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        backend = GeminiLiveBackend(_config(port), api_key="secret")
        started = time.perf_counter()
        events = [e async for e in backend.transcribe(_one_utterance(), session_id="s1")]
        elapsed = time.perf_counter() - started

    assert len(events) == 1
    # 0.5 s of paced audio plus the flush. The bound is deliberately loose:
    # anything near _RECV_TIMEOUT_S means the turn end was missed again.
    assert elapsed < _RECV_TIMEOUT_S / 2


async def test_gemini_live_setup_and_key_on_the_wire() -> None:
    """The parts of the wire format a mock CAN pin down: key as a URL query
    parameter, model under setup with the models/ prefix, TEXT modality, the
    configured language, and the PCM mime type with the configured rate."""
    seen: dict[str, object] = {}

    async def recording(ws: ServerConnection) -> None:
        if ws.request is not None:
            seen["path"] = ws.request.path
        async for message in ws:
            data = cast("dict[str, object]", json.loads(message))
            if "setup" in data:
                seen["setup"] = data["setup"]
                await ws.send(json.dumps({"setupComplete": {}}))
                continue
            realtime = cast("dict[str, object]", data.get("realtimeInput", {}))
            audio = realtime.get("audio")
            if isinstance(audio, dict):
                seen.setdefault("mime", cast("dict[str, object]", audio).get("mimeType"))
            if realtime.get("audioStreamEnd"):
                await ws.send(json.dumps({"serverContent": {"generationComplete": True}}))
                return

    async with serve(recording, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        backend = GeminiLiveBackend(_config(port), api_key="sekret")
        _ = [e async for e in backend.transcribe(_one_utterance(), session_id="s1")]

    assert "key=sekret" in cast("str", seen["path"])
    setup = cast("dict[str, object]", seen["setup"])
    assert setup["model"] == "models/gemini-3.5-transcribe-live"
    assert setup["generationConfig"] == {"responseModalities": ["TEXT"]}
    assert setup["inputAudioTranscription"] == {"languageCodes": ["de"]}
    assert seen["mime"] == "audio/pcm;rate=16000"


async def test_gemini_live_one_session_per_utterance() -> None:
    # Deliberate, matching Deepgram and AssemblyAI: audioStreamEnd is the only
    # documented "no more audio" signal, so every utterance gets its own
    # session rather than leaking late frames into the next one's reads.
    connections = 0

    async def counting(ws: ServerConnection) -> None:
        nonlocal connections
        connections += 1
        await gemini_live_handler(ws)

    async with serve(counting, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        backend = GeminiLiveBackend(_config(port), api_key="secret")
        events = [e async for e in backend.transcribe(_two_utterances(), session_id="s1")]
        await backend.aclose()

    assert len(events) == 2
    assert events[0].text == events[1].text == f"gemini live mock {_SAMPLES} samples"
    assert connections == 2


async def test_gemini_live_health_ok() -> None:
    async with serve(gemini_live_handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        assert await GeminiLiveBackend(_config(port), api_key="secret").health() is True


async def test_gemini_live_health_false_when_unreachable() -> None:
    # Nothing is listening on port 1 -> connect refused -> unhealthy.
    assert await GeminiLiveBackend(_config(1), api_key="x").health() is False
