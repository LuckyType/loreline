"""Integration tests for both xAI connector shapes via ``mocks/xai_ws.py``.

The utterance shape's one behaviour worth a whole file: xAI's
``transcript.done`` repeats everything its ``transcript.partial`` events already
said, so a connector that accumulated both would emit every word twice. The mock
sends both, with the final text exactly equal to the partials joined, which
makes the doubling visible rather than plausible.

The streaming shape's is the other half of the same fact. Every event carries
the transcript so far rather than what is new, so a turn is *replaced* on its
way to the screen and never appended to, and a locked chunk in the middle of a
sentence must not be mistaken for the end of one. Both are driven here through
``TranscriptStream``, the same object a live capture drives.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from itertools import pairwise
from typing import cast

from websockets.asyncio.server import ServerConnection, serve

from loreline.audio.chunker import Utterance
from loreline.models import (
    GAP_SOURCE,
    Glossary,
    ProviderConfig,
    ProviderKind,
    TranscriptEvent,
)
from loreline.stt.backends._ws import as_dict, get_str
from loreline.stt.backends.xai import XaiBackend
from loreline.stt.base import transcribe_capabilities
from loreline.stt.streaming import StreamConfig, TranscriptStream
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


# --- the streaming shape --------------------------------------------------

_LOUD = b"\x00\x20" * 320  # 20 ms of 16 kHz mono, well above the mock's threshold
_QUIET = b"\x01\x00" * 320


async def _stream_events(
    script: list[tuple[bytes, int]],
    *,
    start: float = 100.0,
    handler: Callable[[ServerConnection], Awaitable[None]] = xai_handler,
    settle: float = 0.0,
) -> list[TranscriptEvent]:
    """Run the streaming path over a script of (frame, count) against the mock.

    ``settle`` pauses between the script's blocks, which is where a reconnect
    fits: a feed that never yields long enough for one would be testing the
    queue's drop policy instead.
    """
    async with serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        backend = _backend(port)
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


async def test_streaming_publishes_interims_then_a_final_per_vendor_turn() -> None:
    """One socket, two turns, and text before either of them closed.

    This is what ADR 0006 is about for this vendor: the turns come from xAI's
    own `endpointing` window rather than from 800 ms of local trailing silence,
    and `interim_results` puts words on screen while the sentence is still
    being said.
    """
    events = await _stream_events([(_LOUD, 50), (_QUIET, 30), (_LOUD, 50), (_QUIET, 30)])

    finals = [e for e in events if e.is_final]
    assert len(finals) == 2
    assert [e for e in events if not e.is_final]  # and interims before each one
    assert all(e.text for e in events)

    first, second = finals
    # Timestamps land on the capture clock, not on the vendor's stream offsets.
    assert first.turn_id == "xai-1:1:t100000"
    assert second.turn_id == "xai-1:1:t101600"
    assert first.start_ts == 100.0
    assert 101.5 < second.start_ts < 101.7
    assert first.end_ts > first.start_ts
    # Inline diarization comes off the same words[] the batch transport sends.
    assert {w.speaker for w in first.words} == {"Speaker 0", "Speaker 1"}
    # The settled words are the vendor's refined ones, which sit a little later
    # than where the turn was opened: a turn's start does not move once the row
    # exists, so the words may end up inside it rather than on it. See the
    # mock's ``_SETTLE_SHIFT_MS`` and the connector's ``_partial``.
    assert first.words[0].start >= first.start_ts


async def test_streaming_replaces_a_turn_rather_than_appending_to_it() -> None:
    """xAI resends the whole text every time, so nothing may be concatenated."""
    events = await _stream_events([(_LOUD, 50), (_QUIET, 30)])

    turn = [e for e in events if e.turn_id == "xai-1:1:t100000"]
    assert len(turn) > 1  # several interims and then the final
    assert [e.is_final for e in turn] == [False] * (len(turn) - 1) + [True]
    assert all(later.text.startswith(earlier.text) for earlier, later in pairwise(turn))


async def test_a_locked_chunk_mid_turn_does_not_split_the_turn() -> None:
    """Four seconds of speech crosses the mock's ~3 s chunk-final threshold.

    `is_final=true` with `speech_final=false` means "this much text is locked",
    not "the speaker stopped", so it must grow the open turn instead of closing
    it: one row for the sentence, not one row per three seconds of it.
    """
    events = await _stream_events([(_LOUD, 200), (_QUIET, 30)])

    assert len([e for e in events if e.is_final]) == 1
    assert len({e.turn_id for e in events}) == 1


async def test_finalize_settles_a_turn_the_microphone_stopped_in_the_middle_of() -> None:
    """No trailing silence, so only the flush can close this turn."""
    events = await _stream_events([(_LOUD, 50)])

    finals = [e for e in events if e.is_final]
    assert len(finals) == 1
    assert finals[0].text
    assert finals[0].start_ts == 100.0


async def test_the_streaming_query_string_asks_for_interims_and_endpointing() -> None:
    """Two parameters are the whole difference between the shapes on the wire."""
    paths: list[str] = []

    async def capturing(ws: ServerConnection) -> None:
        request = ws.request
        assert request is not None
        paths.append(request.path)
        await xai_handler(ws)

    async with serve(capturing, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        backend = _backend(port)
        await backend.open_stream(Glossary(campaign_id="c1", terms=["Drakonia"]))
        await backend.close_stream()

    query = paths[0]
    assert "interim_results=true" in query
    assert "endpointing=400" in query
    # ...and everything the utterance shape sends is still there.
    assert "encoding=pcm" in query
    assert "sample_rate=16000" in query
    assert "diarize=true" in query
    assert "keyterm=Drakonia" in query
    assert "model=" not in query
    # The rate the resampler targets and the rate the socket is told are one
    # number: a mismatch is audio played back at the wrong speed, not an error.
    assert f"sample_rate={backend.stream_rate}" in query


async def test_finalize_flushes_and_audio_done_ends_the_socket() -> None:
    """`finalize` keeps the connection; `audio.done` is what gives it up."""
    control: list[str] = []
    ended = asyncio.Event()

    async def inspecting(ws: ServerConnection) -> None:
        await ws.send(json.dumps({"type": "transcript.created"}))
        async for message in ws:
            if isinstance(message, bytes):
                continue
            control.append(get_str(as_dict(message), "type"))
            if control[-1] == "audio.done":
                ended.set()

    async with serve(inspecting, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        backend = _backend(port)
        await backend.open_stream(None)
        await backend.send_audio(_LOUD)  # raw binary, never counted as control
        await backend.flush_input()
        await backend.close_stream()
        await asyncio.wait_for(ended.wait(), 5)

    assert control == ["finalize", "audio.done"]


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


async def test_a_reconnect_starts_a_connection_whose_books_are_empty() -> None:
    """Nothing of the dead connection may reach the one that replaces it.

    This connector reads every event against what it has already published on
    *this* connection - the settled text it strips off the front, and the
    offset before which a word belongs to a turn that is over. Both are stated
    from the first byte of each connection, so carrying either across a
    reconnect would strip the new connection's first turn against the old
    connection's last.
    """
    connections = 0

    async def dropping(ws: ServerConnection) -> None:
        nonlocal connections
        connections += 1
        if connections == 1:
            await xai_handler(cast("ServerConnection", _Hangup(ws, 40)))
            return
        await xai_handler(ws)

    events = await _stream_events(
        [(_LOUD, 60), (_QUIET, 30), (_LOUD, 60), (_QUIET, 30)], handler=dropping, settle=0.2
    )

    assert connections == 2
    finals = [e for e in events if e.is_final and e.source == "xai-1"]
    assert finals
    # Generation 2's first turn opens at its own connection's zero, so its
    # handle is a small offset behind a connection counter that moved.
    assert any(str(e.turn_id).startswith("xai-1:2:") for e in finals)
    second = next(e for e in finals if str(e.turn_id).startswith("xai-1:2:"))
    assert second.text  # nothing was stripped against the dead connection's text
    assert second.words
    assert [e.source for e in events if e.source == GAP_SOURCE] == [GAP_SOURCE]
