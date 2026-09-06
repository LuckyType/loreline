"""Integration tests for the xAI streaming connector via ``mocks/xai_ws.py``.

The one behaviour worth a whole file: xAI's ``transcript.done`` repeats
everything its ``transcript.partial`` events already said, so a connector that
accumulated both would emit every word twice. The mock sends both, with the
final text exactly equal to the partials joined, which makes the doubling
visible rather than plausible.
"""

from __future__ import annotations

import json

from websockets.asyncio.server import ServerConnection, serve

from loreline.audio.chunker import Utterance
from loreline.models import Glossary, ProviderConfig, ProviderKind
from loreline.stt.backends._ws import as_dict
from loreline.stt.backends.xai import XaiBackend
from loreline.stt.base import transcribe_capabilities
from mocks.xai_ws import xai_handler

MODEL = "grok-stt-1.0"


def _config(port: int) -> ProviderConfig:
    return ProviderConfig(
        id="xai-1",
        name="xAI",
        kind=ProviderKind.XAI,
        base_url=f"ws://127.0.0.1:{port}",
        language="de",
    )


def _backend(port: int) -> XaiBackend:
    return XaiBackend(
        _config(port),
        model=MODEL,
        caps=transcribe_capabilities(ProviderKind.XAI, MODEL),
        api_key="secret",
    )


def _one_utterance(start: float = 10.0) -> Utterance:
    return Utterance(pcm=b"\x01\x00" * 1600, start=start, end=start + 0.1)


async def test_streaming_with_diarization() -> None:
    async with serve(xai_handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        backend = _backend(port)
        glossary = Glossary(campaign_id="c1", terms=["Drakonia"])
        event = await backend.transcribe(_one_utterance(), session_id="s1", glossary=glossary)

    assert event is not None
    assert event.is_final
    assert event.source == "xai-1"
    # transcript.done is the whole utterance, said once: the two partials that
    # preceded it carried the same words and must not have been added on top.
    assert event.text == "xai mock 1600 samples"
    assert event.start_ts == 10.0
    # Inline diarization: distinct speakers, and word timings on the session
    # clock rather than on the utterance's own.
    assert {w.speaker for w in event.words} == {"Speaker 0", "Speaker 1"}
    assert len(event.words) == 4
    assert event.words[0].start >= 10.0
    assert event.speaker == "Speaker 0"


async def test_the_query_string_carries_the_whole_configuration() -> None:
    """This endpoint takes no setup frame: the audio format, the language, the
    diarization request and the glossary are all query parameters, and there is
    no `model` among them because the API has none."""
    paths: list[str] = []

    async def capturing(ws: ServerConnection) -> None:
        request = ws.request
        assert request is not None
        paths.append(request.path)
        await xai_handler(ws)

    async with serve(capturing, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        backend = _backend(port)
        glossary = Glossary(campaign_id="c1", terms=["Drakonia", "Thalric"])
        await backend.transcribe(_one_utterance(), session_id="s1", glossary=glossary)

    query = paths[0]
    assert "encoding=pcm" in query
    assert "sample_rate=16000" in query
    assert "channels=1" in query
    assert "language=de" in query
    assert "diarize=true" in query
    # Repeated, not comma-joined.
    assert query.count("keyterm=") == 2
    assert "keyterm=Drakonia" in query
    assert "keyterm=Thalric" in query
    assert "model=" not in query
    # Interim results are left off: one final event per utterance is the
    # contract, so half-formed text is not worth the frames.
    assert "interim_results" not in query


async def test_one_connection_per_utterance() -> None:
    """audio.done is the flush signal and the service hangs up after answering
    it, so a shared socket cannot be reused - and reusing one would leak a late
    frame into the next utterance's window."""
    connections = 0

    async def counting(ws: ServerConnection) -> None:
        nonlocal connections
        connections += 1
        await xai_handler(ws)

    async with serve(counting, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        backend = _backend(port)
        events = [
            await backend.transcribe(_one_utterance(start), session_id="s1") for start in (0.0, 0.5)
        ]

    assert connections == 2
    assert all(e is not None for e in events)


async def test_the_audio_goes_out_as_raw_binary_frames() -> None:
    """No base64, no JSON envelope: the socket takes the PCM as it is, and the
    only text frame the client sends is the control message."""
    binary: list[int] = []
    control: list[dict[str, object]] = []

    async def inspecting(ws: ServerConnection) -> None:
        await ws.send(json.dumps({"type": "transcript.created"}))
        async for message in ws:
            if isinstance(message, bytes):
                binary.append(len(message))
                continue
            control.append(as_dict(message))
            await ws.send(json.dumps({"type": "transcript.done", "text": "ok", "words": []}))
            return

    async with serve(inspecting, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        backend = _backend(port)
        event = await backend.transcribe(_one_utterance(), session_id="s1")

    assert binary == [3200]
    assert control == [{"type": "audio.done"}]
    assert event is not None
    assert event.text == "ok"


async def test_locked_partials_are_the_fallback_for_a_silent_final_event() -> None:
    """Belt and braces for the one reading of the docs this connector rests on.

    If transcript.done ever arrives empty, the utterance is still recovered
    from the partials already on the socket rather than dropped.
    """

    frames: list[dict[str, object]] = [
        {
            "type": "transcript.partial",
            "text": "erste",
            "words": [{"text": "erste", "start": 0.0, "end": 0.2, "speaker": 0}],
            "is_final": True,
        },
        {
            "type": "transcript.partial",
            "text": "zweite",
            "words": [{"text": "zweite", "start": 0.2, "end": 0.4, "speaker": 1}],
            "is_final": True,
        },
        {"type": "transcript.done", "text": "", "words": [], "duration": 0.4},
    ]

    async def only_partials(ws: ServerConnection) -> None:
        await ws.send(json.dumps({"type": "transcript.created"}))
        async for message in ws:
            if isinstance(message, bytes):
                continue
            for frame in frames:
                await ws.send(json.dumps(frame))
            return

    async with serve(only_partials, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        backend = _backend(port)
        event = await backend.transcribe(_one_utterance(), session_id="s1")

    assert event is not None
    assert event.text == "erste zweite"
    assert {w.speaker for w in event.words} == {"Speaker 0", "Speaker 1"}


async def test_an_interim_partial_is_ignored() -> None:
    """Interim results are never requested, but a service that sent one anyway
    must not have it stitched into a final transcript."""

    frames: list[dict[str, object]] = [
        {"type": "transcript.partial", "text": "halb", "words": [], "is_final": False},
        {"type": "transcript.done", "text": "", "words": [], "duration": 0.1},
    ]

    async def chatty(ws: ServerConnection) -> None:
        await ws.send(json.dumps({"type": "transcript.created"}))
        async for message in ws:
            if isinstance(message, bytes):
                continue
            for frame in frames:
                await ws.send(json.dumps(frame))
            return

    async with serve(chatty, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        backend = _backend(port)
        event = await backend.transcribe(_one_utterance(), session_id="s1")

    # Nothing final ever arrived, so there is no event for this utterance.
    assert event is None


async def test_an_error_frame_costs_the_utterance_and_not_the_session() -> None:
    """A rejected utterance is a None the router already handles; raising would
    fail the whole capture over one bad request."""

    async def refusing(ws: ServerConnection) -> None:
        await ws.send(json.dumps({"type": "transcript.created"}))
        async for message in ws:
            if isinstance(message, bytes):
                continue
            await ws.send(json.dumps({"type": "error", "message": "keyterm too long"}))
            return

    async with serve(refusing, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        backend = _backend(port)
        event = await backend.transcribe(_one_utterance(), session_id="s1")

    assert event is None
