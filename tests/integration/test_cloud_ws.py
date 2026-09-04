"""Integration tests for Deepgram and AssemblyAI WS connectors via mocks."""

from __future__ import annotations

from websockets.asyncio.server import ServerConnection, serve

from loreline.audio.chunker import Utterance
from loreline.models import Glossary, ProviderConfig, ProviderKind
from loreline.stt.backends.assemblyai import AssemblyAIBackend
from loreline.stt.backends.deepgram import DeepgramBackend
from mocks.assemblyai_ws import assemblyai_handler
from mocks.deepgram_ws import deepgram_handler


def _one_utterance() -> Utterance:
    return Utterance(pcm=b"\x01\x00" * 1600, start=10.0, end=10.1)


def _two_utterances() -> list[Utterance]:
    return [
        Utterance(pcm=b"\x01\x00" * 1600, start=0.0, end=0.1),
        Utterance(pcm=b"\x02\x00" * 1600, start=0.5, end=0.6),
    ]


def _dg_config(port: int) -> ProviderConfig:
    return ProviderConfig(
        id="dg-1",
        name="Deepgram",
        kind=ProviderKind.DEEPGRAM,
        base_url=f"ws://127.0.0.1:{port}",
    )


def _aai_config(port: int) -> ProviderConfig:
    return ProviderConfig(
        id="aai-1",
        name="AssemblyAI",
        kind=ProviderKind.ASSEMBLYAI,
        base_url=f"ws://127.0.0.1:{port}",
    )


async def test_deepgram_streaming_with_diarization() -> None:
    async with serve(deepgram_handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        config = ProviderConfig(
            id="dg-1",
            name="Deepgram",
            kind=ProviderKind.DEEPGRAM,
            base_url=f"ws://127.0.0.1:{port}",
        )
        backend = DeepgramBackend(config, api_key="secret")
        glossary = Glossary(campaign_id="c1", terms=["Drakonia"])
        event = await backend.transcribe(_one_utterance(), session_id="s1", glossary=glossary)

    assert event is not None
    assert event.is_final
    assert event.source == "dg-1"
    # all finals of the flush accumulated (empty lead-in dropped, both content
    # splits kept), not just the first frame.
    assert event.text == "deepgram mock 1600 samples"
    assert event.start_ts == 10.0
    # inline diarization: distinct speakers, offset applied to word timings.
    assert {w.speaker for w in event.words} == {"Speaker 0", "Speaker 1"}
    assert event.words[0].start >= 10.0


async def test_assemblyai_streaming_with_diarization() -> None:
    async with serve(assemblyai_handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        config = ProviderConfig(
            id="aai-1",
            name="AssemblyAI",
            kind=ProviderKind.ASSEMBLYAI,
            base_url=f"ws://127.0.0.1:{port}",
        )
        backend = AssemblyAIBackend(config, api_key="secret")
        event = await backend.transcribe(_one_utterance(), session_id="s1")

    assert event is not None
    assert event.is_final
    # both turns of the flush accumulated (not just the first), and turn 0's
    # formatted duplicate deduplicated by turn_order (not counted twice).
    assert event.text == "assemblyai mock 1600 samples"
    assert {w.speaker for w in event.words} == {"Speaker A", "Speaker B"}
    # ms -> s conversion plus utterance offset.
    assert event.words[0].start >= 10.0


async def test_assemblyai_rechunks_long_utterances() -> None:
    # The v3 endpoint rejects any single audio message outside 50-1000 ms
    # (the mock enforces it with close code 3007, like the real service), so a
    # 5 s utterance only survives if the backend re-chunks it before sending.
    long_utterance = Utterance(pcm=b"\x01\x00" * 80000, start=0.0, end=5.0)

    async with serve(assemblyai_handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        backend = AssemblyAIBackend(_aai_config(port), api_key="secret")
        event = await backend.transcribe(long_utterance, session_id="s1")

    assert event is not None
    assert event.text == "assemblyai mock 80000 samples"


async def test_deepgram_one_connection_per_utterance() -> None:
    # Deliberate: CloseStream -> Metadata is Deepgram's only unconditional
    # flush signal, so every utterance gets its own connection - reusing one
    # stream leaks late frames into the next utterance's reads.
    connections = 0

    async def counting(ws: ServerConnection) -> None:
        nonlocal connections
        connections += 1
        await deepgram_handler(ws)

    async with serve(counting, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        backend = DeepgramBackend(_dg_config(port), api_key="secret")
        events = [
            await backend.transcribe(utterance, session_id="s1") for utterance in _two_utterances()
        ]
        await backend.aclose()

    assert [e.text for e in events if e] == ["deepgram mock 1600 samples"] * 2
    assert connections == 2


async def test_assemblyai_one_session_per_utterance() -> None:
    # Deliberate: Terminate -> Termination is the v3 protocol's only
    # end-of-flush signal, so every utterance gets its own session.
    connections = 0

    async def counting(ws: ServerConnection) -> None:
        nonlocal connections
        connections += 1
        await assemblyai_handler(ws)

    async with serve(counting, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        backend = AssemblyAIBackend(_aai_config(port), api_key="secret")
        events = [
            await backend.transcribe(utterance, session_id="s1") for utterance in _two_utterances()
        ]
        await backend.aclose()

    assert [e.text for e in events if e] == ["assemblyai mock 1600 samples"] * 2
    assert connections == 2
