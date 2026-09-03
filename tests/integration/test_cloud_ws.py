"""Integration tests for Deepgram and AssemblyAI WS connectors via mocks."""

from __future__ import annotations

from collections.abc import AsyncIterator

from websockets.asyncio.server import ServerConnection, serve
from websockets.http11 import Request, Response

from loreline.audio.chunker import Utterance
from loreline.health import HealthStatus
from loreline.models import Glossary, Protocol, ProviderConfig, ProviderKind, TranscriptEvent
from loreline.stt.backends.assemblyai import AssemblyAIBackend
from loreline.stt.backends.deepgram import DeepgramBackend
from mocks.assemblyai_ws import assemblyai_handler
from mocks.deepgram_ws import deepgram_handler


async def _one_utterance() -> AsyncIterator[Utterance]:
    yield Utterance(pcm=b"\x01\x00" * 1600, start=10.0, end=10.1)


async def _two_utterances() -> AsyncIterator[Utterance]:
    yield Utterance(pcm=b"\x01\x00" * 1600, start=0.0, end=0.1)
    yield Utterance(pcm=b"\x02\x00" * 1600, start=0.5, end=0.6)


def _dg_config(port: int) -> ProviderConfig:
    return ProviderConfig(
        id="dg-1",
        name="Deepgram",
        kind=ProviderKind.DEEPGRAM,
        base_url=f"ws://127.0.0.1:{port}",
        protocol=Protocol.WS,
    )


def _aai_config(port: int) -> ProviderConfig:
    return ProviderConfig(
        id="aai-1",
        name="AssemblyAI",
        kind=ProviderKind.ASSEMBLYAI,
        base_url=f"ws://127.0.0.1:{port}",
        protocol=Protocol.WS,
    )


async def test_deepgram_streaming_with_diarization() -> None:
    async with serve(deepgram_handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        config = ProviderConfig(
            id="dg-1",
            name="Deepgram",
            kind=ProviderKind.DEEPGRAM,
            base_url=f"ws://127.0.0.1:{port}",
            protocol=Protocol.WS,
        )
        backend = DeepgramBackend(config, api_key="secret")
        glossary = Glossary(campaign_id="c1", terms=["Drakonia"])
        events: list[TranscriptEvent] = [
            e
            async for e in backend.transcribe(_one_utterance(), session_id="s1", glossary=glossary)
        ]

    assert len(events) == 1
    event = events[0]
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
            protocol=Protocol.WS,
        )
        backend = AssemblyAIBackend(config, api_key="secret")
        events: list[TranscriptEvent] = [
            e async for e in backend.transcribe(_one_utterance(), session_id="s1")
        ]

    assert len(events) == 1
    event = events[0]
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
    async def _long_utterance() -> AsyncIterator[Utterance]:
        yield Utterance(pcm=b"\x01\x00" * 80000, start=0.0, end=5.0)

    async with serve(assemblyai_handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        backend = AssemblyAIBackend(_aai_config(port), api_key="secret")
        events = [e async for e in backend.transcribe(_long_utterance(), session_id="s1")]

    assert len(events) == 1
    assert events[0].text == "assemblyai mock 80000 samples"


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
        events = [e async for e in backend.transcribe(_two_utterances(), session_id="s1")]
        await backend.aclose()

    assert len(events) == 2
    assert events[0].text == events[1].text == "deepgram mock 1600 samples"
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
        events = [e async for e in backend.transcribe(_two_utterances(), session_id="s1")]
        await backend.aclose()

    assert len(events) == 2
    assert events[0].text == events[1].text == "assemblyai mock 1600 samples"
    assert connections == 2


async def test_deepgram_health_ok() -> None:
    async with serve(deepgram_handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        report = await DeepgramBackend(_dg_config(port), api_key="secret").health()
        assert report.status is HealthStatus.HEALTHY


async def test_assemblyai_health_ok() -> None:
    async with serve(assemblyai_handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        report = await AssemblyAIBackend(_aai_config(port), api_key="secret").health()
        assert report.status is HealthStatus.HEALTHY


async def test_health_unreachable_when_nothing_listens() -> None:
    # Nothing is listening on port 1 -> connect refused. That is the base_url
    # being wrong, which is a different fix from a rejected key, so the two
    # must not collapse into the same badge.
    for report in (
        await DeepgramBackend(_dg_config(1), api_key="x").health(),
        await AssemblyAIBackend(_aai_config(1), api_key="x").health(),
    ):
        assert report.status is HealthStatus.UNREACHABLE
        assert report.detail is not None


async def test_health_reads_a_rejected_upgrade_as_an_auth_failure() -> None:
    """A bad key never opens the socket: the vendor rejects the HTTP upgrade.

    websockets surfaces that as InvalidStatus carrying the response, which is
    the only reason a streaming connector can tell a wrong key from a wrong
    host at all. Deepgram's real answer is 401 with this body.
    """

    async def reject(connection: ServerConnection, request: Request) -> Response:
        return connection.respond(
            401,
            '{"category":"UNAUTHORIZED","message":"Authentication failed.",'
            '"details":"Check that you are using the correct credentials."}',
        )

    async def handler(ws: ServerConnection) -> None:  # pragma: no cover - never reached
        await ws.wait_closed()

    async with serve(handler, "127.0.0.1", 0, process_request=reject) as server:
        port = server.sockets[0].getsockname()[1]
        report = await DeepgramBackend(_dg_config(port), api_key="bad").health()

    assert report.status is HealthStatus.UNAUTHORIZED
    # The sentence, not the raw JSON: the badge tooltip shows this verbatim.
    assert report.detail == "Authentication failed."
