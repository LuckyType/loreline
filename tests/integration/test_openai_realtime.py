"""Integration tests for the OpenAI Realtime backend via a mock WS server."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from itertools import pairwise
from typing import cast

import pytest
from websockets.asyncio.server import ServerConnection, serve

from loreline.audio.chunker import Utterance
from loreline.audio.resample import resample_pcm16
from loreline.models import (
    GAP_SOURCE,
    Glossary,
    ProviderConfig,
    ProviderKind,
    TranscriptEvent,
)
from loreline.stt.backends.openai_realtime import OpenAIRealtimeBackend
from loreline.stt.streaming import StreamConfig, StreamUnsupportedError, TranscriptStream
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
    script: list[tuple[bytes, int]],
    *,
    start: float = 100.0,
    handler: Callable[[ServerConnection], Awaitable[None]] = openai_realtime_handler,
    settle: float = 0.0,
) -> list[TranscriptEvent]:
    """Run the streaming path over a script of (frame, count) against the mock.

    ``settle`` pauses between the script's blocks, which is where a reconnect
    fits: a feed that never yields long enough for one would be testing the
    queue's drop policy instead.
    """
    async with serve(handler, "127.0.0.1", 0) as server:
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
            config=StreamConfig(
                session_id="s1",
                interim_interval_s=0.0,
                final_wait_s=1.0,
                reconnect_backoff_s=0.0,
            ),
        )
        task = asyncio.create_task(stream.run())
        ts = start
        for frame, count in script:
            for _ in range(count):
                stream.feed(frame, ts, is_speech=frame is _LOUD)
                ts += 0.02
                await asyncio.sleep(0)
            if settle:
                await asyncio.sleep(settle)
        await asyncio.sleep(0.2)
        stream.stop()
        await asyncio.wait_for(task, 10)
        await backend.aclose()
    return events


async def test_streaming_publishes_interims_then_a_final_per_server_vad_turn() -> None:
    """Server VAD decides the turns, and the deltas arrive as the turn closes.

    Not while somebody is still speaking, which is what this test used to
    claim and what the mock used to fake by inventing a delta per 200 ms of
    speech. The ADR 0006 verification run against the real endpoint measured a
    median time to first interim of 7.6 s against a median turn length of
    7.6 s: this vendor says nothing at all until it closes a turn, and then
    sends every delta of it at once. So the interims are real and they are
    worth publishing - a growing row settles into a final under one key - but
    they are not the mid-turn text the other vendors give.
    """
    pytest.importorskip("soxr")
    events = await _stream_events([(_LOUD, 50), (_QUIET, 50), (_LOUD, 50), (_QUIET, 40)])

    interims = [e for e in events if not e.is_final]
    finals = [e for e in events if e.is_final]
    assert len(finals) == 2  # two turns, cut by the mock's own silence rule
    assert interims  # deltas, published as a growing row before the final lands
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


async def test_a_model_that_refuses_server_vad_cannot_be_streamed() -> None:
    """The path the whole OpenAI migration rests on, and it had no test.

    Both models capabilities.yaml routes to this connector answer a server-VAD
    ``session.update`` with "Turn detection is not supported for this
    transcription model", naming the parameter. Nothing about that changes on
    a second attempt, so it is StreamUnsupportedError rather than a failed
    connection, and the session runs this same connector one utterance at a
    time - which is what it did before streaming existed.
    """
    async with serve(openai_realtime_handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        config = ProviderConfig(
            id="oai",
            name="OpenAI",
            kind=ProviderKind.OPENAI,
            base_url=f"ws://127.0.0.1:{port}",
            sample_rate=24000,
        )
        backend = OpenAIRealtimeBackend(config, model="gpt-live-transcribe", api_key="x")
        with pytest.raises(StreamUnsupportedError, match="turn detection"):
            await backend.open_stream(None)
        await backend.aclose()


async def test_the_same_model_still_transcribes_one_utterance_at_a_time() -> None:
    """...which is the point of the refusal being a refusal and not a failure.

    Turn detection is off on the utterance shape, so the model that will not
    stream still answers here, and that is the whole fallback.
    """
    async with serve(openai_realtime_handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        config = ProviderConfig(
            id="oai",
            name="OpenAI",
            kind=ProviderKind.OPENAI,
            base_url=f"ws://127.0.0.1:{port}",
            sample_rate=24000,
        )
        backend = OpenAIRealtimeBackend(config, model="gpt-live-transcribe", api_key="x")
        event = await backend.transcribe(_one(), session_id="s1")
        await backend.aclose()

    assert event is not None
    assert event.text


async def test_a_session_configuration_error_is_a_failed_attempt() -> None:
    """A rejected session.update that is not about turn detection still raises.

    Treating it as configured left a socket with no server VAD on it: audio
    goes out, nothing comes back, and the liveness watchdog is what finally
    notices, twenty seconds and one gap marker later.
    """

    async def refusing(ws: ServerConnection) -> None:
        async for message in ws:
            if isinstance(message, bytes):
                continue
            if json.loads(message).get("type") == "session.update":
                await ws.send(
                    json.dumps(
                        {
                            "type": "error",
                            "error": {
                                "type": "invalid_request_error",
                                "param": "session.audio.input.format.rate",
                                "message": "Unsupported input rate.",
                            },
                        }
                    )
                )

    async with serve(refusing, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        config = ProviderConfig(
            id="oai",
            name="OpenAI",
            kind=ProviderKind.OPENAI,
            base_url=f"ws://127.0.0.1:{port}",
            sample_rate=24000,
        )
        backend = OpenAIRealtimeBackend(config, api_key="x")
        with pytest.raises(RuntimeError) as caught:
            await backend.open_stream(None)
        assert not isinstance(caught.value, StreamUnsupportedError)
        await backend.aclose()


async def test_an_item_that_failed_to_transcribe_does_not_end_the_session() -> None:
    """``transcription.failed`` is about one item, not about the session.

    Audio too short, or a content filter. Ending the whole live session on one
    threw away every turn after it and cost a gap marker for a two-word
    backchannel; settling the item is the answer, and the stream publishes it
    as whatever the turn had already said.
    """
    signals: list[str] = []

    class _Frames:
        def __aiter__(self) -> AsyncIterator[str]:
            return self._replay()

        async def _replay(self) -> AsyncIterator[str]:
            yield json.dumps({"type": "input_audio_buffer.speech_started", "item_id": "a"})
            yield json.dumps(
                {
                    "type": "conversation.item.input_audio_transcription.failed",
                    "item_id": "a",
                    "error": {"message": "audio is too short"},
                }
            )
            yield json.dumps({"type": "input_audio_buffer.speech_started", "item_id": "b"})
            yield json.dumps({"type": _COMPLETED_TYPE, "item_id": "b", "transcript": "still here"})

    config = ProviderConfig(id="oai", name="OpenAI", kind=ProviderKind.OPENAI)
    backend = OpenAIRealtimeBackend(config, api_key="x")
    frames = _Frames()
    backend._stream_ws = frames  # pyright: ignore[reportPrivateUsage, reportAttributeAccessIssue]
    async for signal in backend.signals():
        signals.append(type(signal).__name__)

    assert signals == ["TurnStarted", "TurnFinal", "TurnStarted", "TurnFinal"]


class _Hangup:
    """A server socket that stops feeding its handler after ``limit`` messages.

    The handler then returns, ``serve`` closes the connection, and the client
    sees a socket that died mid-session - which is the only way to drive a
    reconnect against a mock that would otherwise serve forever.
    """

    def __init__(self, ws: ServerConnection, limit: int) -> None:
        self._ws = ws
        self._limit = limit

    def __getattr__(self, name: str) -> object:
        return getattr(self._ws, name)

    def __aiter__(self) -> AsyncIterator[str | bytes]:
        return self._limited()

    async def _limited(self) -> AsyncIterator[str | bytes]:
        seen = 0
        async for message in self._ws:
            yield message
            seen += 1
            if seen >= self._limit:
                return


async def test_a_reconnect_starts_a_session_whose_books_are_empty() -> None:
    """Nothing of the dead connection may reach the one that replaces it.

    OpenAI numbers its items per session, so the first turn after a dropped
    socket is ``item_001`` again. The connection counter in the turn id is
    what keeps that from replacing the first turn of the evening rather than
    following it, and this is the test that says the counter moved at all.
    """
    pytest.importorskip("soxr")
    connections = 0

    async def dropping(ws: ServerConnection) -> None:
        nonlocal connections
        connections += 1
        if connections == 1:
            await openai_realtime_handler(cast("ServerConnection", _Hangup(ws, 40)))
            return
        await openai_realtime_handler(ws)

    events = await _stream_events(
        [(_LOUD, 50), (_QUIET, 50), (_LOUD, 50), (_QUIET, 50)], handler=dropping, settle=0.2
    )

    assert connections == 2
    finals = [e for e in events if e.is_final and e.source == "oai"]
    assert finals
    assert any(str(e.turn_id).startswith("oai:2:item_001") for e in finals)
    # The gap the reconnect left is a row in the transcript, not a log line.
    assert [e.source for e in events if e.source == GAP_SOURCE] == [GAP_SOURCE]
