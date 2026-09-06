"""Integration tests for the OpenAI Realtime backend via a mock WS server."""

from __future__ import annotations

import asyncio
import json
from itertools import pairwise

import pytest
from websockets.asyncio.server import ServerConnection, serve

from loreline.audio.chunker import Utterance
from loreline.audio.resample import resample_pcm16
from loreline.models import Glossary, ProviderConfig, ProviderKind, TranscriptEvent
from loreline.stt.backends.openai_realtime import OpenAIRealtimeBackend
from loreline.stt.streaming import StreamConfig, TranscriptStream
from mocks.openai_realtime_ws import openai_realtime_handler

_COMPLETED_TYPE = "conversation.item.input_audio_transcription.completed"


def _one() -> Utterance:
    return Utterance(pcm=b"\x01\x00" * 480, start=2.0, end=2.5)


def _utt(index: int) -> Utterance:
    return Utterance(pcm=b"\x01\x00" * 480, start=float(index), end=index + 0.5)


async def test_realtime_transcribe_via_mock() -> None:
    async with serve(openai_realtime_handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        config = ProviderConfig(
            id="oai-1",
            name="OpenAI",
            kind=ProviderKind.OPENAI,
            base_url=f"ws://127.0.0.1:{port}",
            sample_rate=24000,  # == realtime output rate -> no resample (no numpy needed)
        )
        backend = OpenAIRealtimeBackend(config, api_key="secret")
        event = await backend.transcribe(_one(), session_id="s1")

    assert event is not None
    assert event.is_final
    assert "openai realtime mock" in event.text
    assert event.start_ts == 2.0
    assert event.source == "oai-1"


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
            sample_rate=24000,
        )
        backend = OpenAIRealtimeBackend(config, api_key="secret")
        events = [await backend.transcribe(_utt(i), session_id="s") for i in range(3)]
        await backend.aclose()

    assert all(e is not None for e in events)
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
            sample_rate=24000,
        )
        backend = OpenAIRealtimeBackend(config, api_key="x")
        glossary = Glossary(campaign_id="c", terms=["Drakonia", "Mistwood"])
        event = await backend.transcribe(_one(), session_id="s", glossary=glossary)
        await backend.aclose()

    assert event is not None
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
            sample_rate=24000,
        )
        backend = OpenAIRealtimeBackend(config, api_key="x")
        glossary = Glossary(campaign_id="c", terms=terms)
        event = await backend.transcribe(_one(), session_id="s", glossary=glossary)
        await backend.aclose()

    assert event is not None
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
            sample_rate=24000,
        )
        backend = OpenAIRealtimeBackend(config, api_key="x")
        glossary = Glossary(campaign_id="c", terms=["Drakonia"])
        events = [
            await backend.transcribe(_utt(i), session_id="s", glossary=glossary) for i in range(2)
        ]
        await backend.aclose()

    assert all(e is not None for e in events)  # no utterance swallowed by the rejection
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


# --- the streaming shape --------------------------------------------------

_LOUD = b"\x00\x20" * 320  # 20 ms of 16 kHz mono, well above the mock's threshold
_QUIET = b"\x01\x00" * 320


async def _stream_events(
    script: list[tuple[bytes, int]], *, start: float = 100.0
) -> list[TranscriptEvent]:
    """Run the streaming path over a script of (frame, count) against the mock."""
    async with serve(openai_realtime_handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        config = ProviderConfig(
            id="oai",
            name="OpenAI",
            kind=ProviderKind.OPENAI,
            base_url=f"ws://127.0.0.1:{port}",
            sample_rate=16000,  # capture rate: exercises the 16k -> 24k resampler
        )
        backend = OpenAIRealtimeBackend(config, api_key="secret")
        events: list[TranscriptEvent] = []

        async def publish(event: TranscriptEvent) -> None:
            events.append(event)

        stream = TranscriptStream(
            backend,
            publish=publish,
            capture_rate=16000,
            config=StreamConfig(session_id="s1", interim_interval_s=0.0, final_wait_s=1.0),
        )
        task = asyncio.create_task(stream.run())
        ts = start
        for frame, count in script:
            for _ in range(count):
                stream.feed(frame, ts, is_speech=frame is _LOUD)
                ts += 0.02
                await asyncio.sleep(0)
        await asyncio.sleep(0.2)
        stream.stop()
        await asyncio.wait_for(task, 10)
        await backend.aclose()
    return events


async def test_streaming_publishes_interims_then_a_final_per_server_vad_turn() -> None:
    """Server VAD decides the turns; deltas arrive as interims before the final.

    This is the whole point of ADR 0006: text while someone is still speaking,
    from the vendor's own endpointing rather than 800 ms after they stop.
    """
    pytest.importorskip("soxr")
    events = await _stream_events([(_LOUD, 50), (_QUIET, 50), (_LOUD, 50), (_QUIET, 40)])

    interims = [e for e in events if not e.is_final]
    finals = [e for e in events if e.is_final]
    assert len(finals) == 2  # two turns, cut by the mock's own silence rule
    assert interims  # and text before each one closed
    assert all(e.text for e in events)

    first, second = finals
    assert first.turn_id == "oai:1:item_001"
    assert second.turn_id == "oai:1:item_002"
    # Timestamps land on the capture clock, not on the vendor's millisecond count.
    assert 100.0 <= first.start_ts < 101.0
    assert 102.0 <= second.start_ts < 103.0
    assert first.end_ts > first.start_ts


async def test_streaming_replaces_a_turn_rather_than_appending_to_it() -> None:
    """Every revision of one turn carries one key, and the text only grows."""
    pytest.importorskip("soxr")
    events = await _stream_events([(_LOUD, 50), (_QUIET, 50)])

    turn = [e for e in events if e.turn_id == "oai:1:item_001"]
    assert len(turn) > 1  # several interims and then the final
    assert [e.is_final for e in turn] == [False] * (len(turn) - 1) + [True]
    assert all(later.text.startswith(earlier.text) for earlier, later in pairwise(turn))


async def test_streaming_sends_server_vad_and_the_glossary_prompt() -> None:
    """The session config is what makes the vendor, not a local VAD, cut turns."""
    updates: list[str] = []

    async def handler(ws: ServerConnection) -> None:
        async for message in ws:
            if isinstance(message, bytes):
                continue
            if json.loads(message).get("type") == "session.update":
                updates.append(message)
                await ws.send(json.dumps({"type": "session.updated"}))

    async with serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        config = ProviderConfig(
            id="oai",
            name="OpenAI",
            kind=ProviderKind.OPENAI,
            base_url=f"ws://127.0.0.1:{port}",
            sample_rate=24000,
        )
        backend = OpenAIRealtimeBackend(config, api_key="x")
        await backend.open_stream(Glossary(campaign_id="c", terms=["Drakonia"]))
        await backend.close_stream()

    sent = json.loads(updates[0])["session"]["audio"]["input"]
    assert sent["turn_detection"]["type"] == "server_vad"
    assert sent["transcription"]["prompt"] == "Drakonia"


async def test_the_utterance_shape_still_runs_with_turn_detection_off() -> None:
    """ADR 0005's contract is untouched: the fallback path and reprocess need it."""
    async with serve(openai_realtime_handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        config = ProviderConfig(
            id="oai",
            name="OpenAI",
            kind=ProviderKind.OPENAI,
            base_url=f"ws://127.0.0.1:{port}",
            sample_rate=24000,
        )
        backend = OpenAIRealtimeBackend(config, api_key="secret")
        event = await backend.transcribe(_one(), session_id="s1")
        await backend.aclose()

    assert event is not None
    assert event.is_final
    assert event.turn_id is None  # nothing to replace on the utterance path
