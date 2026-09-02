"""Integration tests for the Gemini Live backend via a mock WS server.

The mock mirrors the documented protocol, not the real service - which is why
gemini-3.5-transcribe-live stays hidden from the pickers until a verification
run (see test_catalog.py::test_gemini_live_model_is_hidden_until_verified).
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import cast

from websockets.asyncio.server import ServerConnection, serve

from loreline.audio.chunker import Utterance
from loreline.models import Glossary, Protocol, ProviderConfig, ProviderKind, TranscriptEvent
from loreline.stt.backends.gemini_live import GeminiLiveBackend
from mocks.gemini_live_ws import gemini_live_handler


async def _one_utterance() -> AsyncIterator[Utterance]:
    yield Utterance(pcm=b"\x01\x00" * 1600, start=10.0, end=10.1)


async def _two_utterances() -> AsyncIterator[Utterance]:
    yield Utterance(pcm=b"\x01\x00" * 1600, start=0.0, end=0.1)
    yield Utterance(pcm=b"\x02\x00" * 1600, start=0.5, end=0.6)


# The registry resolves the model and hands it over; the connector keeps none
# of its own. capabilities.yaml declares Gemini's default, and this model is
# hidden there until the connector is verified against the real service, so it
# only ever arrives from a config that names it.
MODEL = "gemini-3.5-transcribe-live"


def _config(port: int, language: str = "de") -> ProviderConfig:
    return ProviderConfig(
        id="gem-live-1",
        name="Gemini Live",
        kind=ProviderKind.GEMINI,
        base_url=f"ws://127.0.0.1:{port}",
        protocol=Protocol.WS,
        language=language,
    )


async def test_gemini_live_streaming_transcribe() -> None:
    async with serve(gemini_live_handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        backend = GeminiLiveBackend(_config(port), model=MODEL, api_key="secret")
        glossary = Glossary(campaign_id="c1", terms=["Drakonia"])  # ignored, but must not break
        events: list[TranscriptEvent] = [
            e
            async for e in backend.transcribe(_one_utterance(), session_id="s1", glossary=glossary)
        ]

    assert len(events) == 1
    event = events[0]
    assert event.is_final
    assert event.source == "gem-live-1"
    # Finalized fragments concatenated (they carry their own spacing); the
    # interim frame ignored, or the text would start with "gemini l".
    assert event.text == "gemini live mock 1600 samples"
    assert event.start_ts == 10.0
    assert event.end_ts == 10.1
    # Google: "Speaker diarization is not supported in live streaming
    # sessions" - no words, no speaker, ever.
    assert event.words == []
    assert event.speaker is None


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
                await ws.send(json.dumps({"serverContent": {"turnComplete": True}}))
                return

    async with serve(recording, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        backend = GeminiLiveBackend(_config(port), model=MODEL, api_key="sekret")
        _ = [e async for e in backend.transcribe(_one_utterance(), session_id="s1")]

    assert "key=sekret" in cast("str", seen["path"])
    setup = cast("dict[str, object]", seen["setup"])
    assert setup["model"] == f"models/{MODEL}"
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
        backend = GeminiLiveBackend(_config(port), model=MODEL, api_key="secret")
        events = [e async for e in backend.transcribe(_two_utterances(), session_id="s1")]
        await backend.aclose()

    assert len(events) == 2
    assert events[0].text == events[1].text == "gemini live mock 1600 samples"
    assert connections == 2


async def test_gemini_live_health_ok() -> None:
    async with serve(gemini_live_handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        assert (
            await GeminiLiveBackend(_config(port), model=MODEL, api_key="secret").health() is True
        )


async def test_gemini_live_health_false_when_unreachable() -> None:
    # Nothing is listening on port 1 -> connect refused -> unhealthy.
    assert await GeminiLiveBackend(_config(1), model=MODEL, api_key="x").health() is False
