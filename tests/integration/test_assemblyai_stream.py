"""The AssemblyAI streaming shape end to end, against the mock v3 endpoint.

``test_assemblyai_frames.py`` pins the translation of recorded real messages;
this drives the whole path a live capture takes - a socket, the 50 ms audio
floor the endpoint enforces, its own endpointing deciding the turns, and
``TranscriptStream`` turning the signals back into events on the session clock.
"""

from __future__ import annotations

import asyncio
import functools
import json
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import cast
from urllib.parse import parse_qs, urlparse

import pytest
from websockets.asyncio.client import ClientConnection
from websockets.asyncio.server import ServerConnection, serve

from loreline.models import (
    GAP_SOURCE,
    Glossary,
    ProviderConfig,
    ProviderKind,
    TranscriptEvent,
)
from loreline.stt.backends.assemblyai import (
    _SESSION_END_MARGIN_S,  # pyright: ignore[reportPrivateUsage]
    AssemblyAIBackend,
)
from loreline.stt.streaming import StreamConfig, StreamUnsupportedError, TranscriptStream
from mocks.assemblyai_ws import assemblyai_handler

_LOUD = b"\x00\x20" * 320  # 20 ms of 16 kHz mono, well above the mock's threshold
_QUIET = b"\x01\x00" * 320


def _config(port: int) -> ProviderConfig:
    return ProviderConfig(
        id="aai",
        name="AssemblyAI",
        kind=ProviderKind.ASSEMBLYAI,
        base_url=f"ws://127.0.0.1:{port}",
        sample_rate=16000,
    )


async def _stream_events(
    script: list[tuple[bytes, int]],
    *,
    start: float = 100.0,
    handler: Callable[[ServerConnection], Awaitable[None]] = assemblyai_handler,
    settle: float = 0.0,
) -> list[TranscriptEvent]:
    """Run the streaming path over a script of (frame, count) against the mock.

    ``settle`` pauses between the script's blocks, which is where a reconnect
    fits: a feed that never yields long enough for one would be testing the
    queue's drop policy instead.
    """
    async with serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        backend = AssemblyAIBackend(_config(port), api_key="secret")
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
                final_wait_s=2.0,
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
        await asyncio.wait_for(task, 15)
        await backend.aclose()
    return events


# Two turns, each closed by the mock's own silence rule (600 ms), the way
# AssemblyAI's endpointing closes one on max_turn_silence.
_TWO_TURNS = [(_LOUD, 50), (_QUIET, 50), (_LOUD, 50), (_QUIET, 50)]


async def test_the_vendors_endpointing_produces_interims_then_a_final_per_turn() -> None:
    """The point of ADR 0006 for this vendor: text while somebody is speaking.

    AssemblyAI is one of the vendors whose partials really do arrive mid-turn
    rather than half a second before the final, so a turn shows several
    interims before it settles.
    """
    events = await _stream_events(_TWO_TURNS)

    interims = [e for e in events if not e.is_final]
    assert interims
    assert all(e.text for e in events)
    assert {e.turn_id for e in events} == {"aai:1:0", "aai:1:1"}
    # Timestamps land on the capture clock, not on the vendor's millisecond count.
    first = next(e for e in events if e.turn_id == "aai:1:0" and e.is_final)
    assert 100.0 <= first.start_ts < 101.5
    assert first.end_ts > first.start_ts


async def test_a_turn_is_replaced_by_its_revisions_rather_than_appended_to() -> None:
    """A partial carries the whole turn, so appending would duplicate it."""
    events = await _stream_events([(_LOUD, 50), (_QUIET, 50)])

    turn = [e for e in events if e.turn_id == "aai:1:0"]
    assert len(turn) > 1
    assert not turn[0].is_final
    # The mock speaks the same words the real service does, one per partial, so
    # a connector that appended would have said "the" twice by the second one.
    assert turn[0].text == "the"
    assert turn[1].text == "the goblin"
    assert "the the" not in turn[-1].text


async def test_the_formatted_duplicate_replaces_the_turn_it_repeats() -> None:
    """``format_turns`` re-sends an ended turn formatted, under one turn_order.

    The utterance shape has to deduplicate those, because it joins turns into
    one text. This shape does not: both are finals under one turn id, and a
    turn id is the key a row is replaced under.
    """
    events = await _stream_events([(_LOUD, 50), (_QUIET, 50)])

    finals = [e for e in events if e.is_final and e.turn_id == "aai:1:0"]
    assert len(finals) >= 2
    settled, formatted = finals[0], finals[1]
    assert settled.text == "the goblin takes"
    assert formatted.text == "The goblin takes."  # same turn, formatting applied


async def test_final_words_carry_speaker_labels_for_inline_diarization() -> None:
    events = await _stream_events(_TWO_TURNS)

    finals = [e for e in events if e.is_final and e.words]
    assert finals
    speakers = {w.speaker for e in finals for w in e.words}
    assert speakers <= {"Speaker A", "Speaker B"}
    assert None not in speakers
    # Word timings are on the capture clock, like the event's own.
    words = finals[0].words
    assert words[0].start >= 100.0
    assert words[-1].end >= words[0].start


async def test_the_end_of_session_speaker_pass_replaces_the_turns_it_revised() -> None:
    """AssemblyAI re-labels turns after Terminate; the rows follow.

    The mock revises every ended turn onto the other speaker, so a republished
    turn is distinguishable from the one it replaced by its labels alone. This
    is the only way a *published* turn's speakers can still improve, and it is
    why flush_input waits for the session's own Termination.
    """
    events = await _stream_events(_TWO_TURNS)

    turn = [e for e in events if e.turn_id == "aai:1:0" and e.is_final]
    assert len(turn) == 3  # settled, formatted, revised
    before, after = turn[1], turn[2]
    assert after.text == before.text  # a revision names words, not text
    assert {w.speaker for w in before.words} == {"Speaker A"}
    assert {w.speaker for w in after.words} == {"Speaker B"}


async def test_the_streaming_session_asks_for_partials_speakers_and_the_glossary() -> None:
    """What the query string asks for is what makes this shape work at all."""
    seen: list[str] = []

    async def handler(ws: ServerConnection) -> None:
        seen.append(getattr(ws.request, "path", ""))
        await assemblyai_handler(ws)

    async with serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        backend = AssemblyAIBackend(_config(port), api_key="secret")
        await backend.open_stream(Glossary(campaign_id="c", terms=["Drakonia", "Mistwood"]))
        await backend.close_stream()

    query = parse_qs(urlparse(seen[0]).query)
    assert query["continuous_partials"] == ["true"]
    assert query["speaker_labels"] == ["true"]
    assert query["format_turns"] == ["true"]
    assert query["encoding"] == ["pcm_s16le"]
    assert query["sample_rate"] == ["16000"]
    assert json.loads(query["keyterms_prompt"][0]) == ["Drakonia", "Mistwood"]


async def test_the_streaming_session_asks_the_vendor_for_a_language() -> None:
    """`language_codes`, a JSON array, is what tells the vendor which language
    to expect - replacing the plain `language=` this connector used to send,
    which Universal-Streaming v3 has no parameter for and silently dropped."""
    seen: list[str] = []

    async def handler(ws: ServerConnection) -> None:
        seen.append(getattr(ws.request, "path", ""))
        await assemblyai_handler(ws)

    async with serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        backend = AssemblyAIBackend(_config(port), api_key="secret")  # config.language: "de"
        await backend.open_stream(None)
        await backend.close_stream()

    query = parse_qs(urlparse(seen[0]).query)
    assert json.loads(query["language_codes"][0]) == ["de"]
    assert "language" not in query


async def test_a_model_the_endpoint_will_not_stream_is_not_a_retry() -> None:
    """v3 accepts the upgrade and refuses the query string, as a frame.

    Verified live: ``speech_model=universal-2`` answers ``{"type": "Error",
    "error_code": 3006, "error": "Invalid 'speech_model': ..."}``. Nothing
    about that changes on a second attempt, so the session runs this provider
    on the utterance path instead of spending its reconnect budget.
    """

    async def handler(ws: ServerConnection) -> None:
        await ws.send(
            json.dumps(
                {
                    "type": "Error",
                    "error_code": 3006,
                    "error": "User Input Validation Error: Invalid 'speech_model'",
                }
            )
        )
        await ws.close()

    async with serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        backend = AssemblyAIBackend(_config(port), model="universal-2", api_key="secret")
        with pytest.raises(StreamUnsupportedError, match="speech_model"):
            await backend.open_stream(None)
        await backend.aclose()


async def test_a_session_refused_for_another_reason_is_worth_retrying() -> None:
    """Not every no is final: only the input-validation one is about the model."""

    async def handler(ws: ServerConnection) -> None:
        await ws.send(json.dumps({"type": "Error", "error_code": 4001, "error": "temporary"}))
        await ws.close()

    async with serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        backend = AssemblyAIBackend(_config(port), api_key="secret")
        with pytest.raises(RuntimeError, match="temporary") as caught:
            await backend.open_stream(None)
        assert not isinstance(caught.value, StreamUnsupportedError)
        await backend.aclose()


async def test_force_endpoint_ends_a_turn_without_ending_the_session() -> None:
    """The other flush v3 defines, and the reason it is not the one used.

    ``flush_input`` sends Terminate because it runs only when the microphone
    has stopped. ForceEndpoint is what a caller would send to cut a turn while
    the session keeps going, so the mock speaks it and this pins the difference.
    """
    from websockets.asyncio.client import connect  # noqa: PLC0415 - one test's client

    async with serve(assemblyai_handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        url = f"ws://127.0.0.1:{port}/v3/ws?sample_rate=16000&speaker_labels=true&continuous_partials=true"
        async with connect(url) as ws:
            await ws.recv()  # Begin
            for _ in range(6):  # 6 x 100 ms of speech, no silence at all
                await ws.send(_LOUD * 5)
            await ws.send(json.dumps({"type": "ForceEndpoint"}))
            kinds: list[str] = []
            ended: list[dict[str, object]] = []
            while "Termination" not in kinds:
                message = json.loads(await asyncio.wait_for(ws.recv(), 5))
                kinds.append(str(message.get("type")))
                if message.get("end_of_turn"):
                    ended.append(message)
                if len(ended) == 2 and "Terminate" not in kinds:
                    kinds.append("Terminate")
                    await ws.send(json.dumps({"type": "Terminate"}))

    assert ended, "ForceEndpoint closed no turn"
    assert kinds.count("SpeechStarted") == 1  # one turn, ended without silence
    assert "SpeakerRevision" in kinds  # and Terminate still runs the speaker pass


async def test_the_utterance_shape_still_opens_a_session_per_utterance() -> None:
    """ADR 0005's contract is untouched: reprocess and the fallback need it."""
    from loreline.audio.chunker import Utterance  # noqa: PLC0415 - only this test's shape

    async with serve(assemblyai_handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        backend = AssemblyAIBackend(_config(port), api_key="secret")
        event = await backend.transcribe(
            Utterance(pcm=b"\x01\x00" * 1600, start=10.0, end=10.1), session_id="s1"
        )
        await backend.aclose()

    assert event is not None
    assert event.is_final
    assert event.turn_id is None  # nothing to replace on the utterance path
    assert event.text == "assemblyai mock 1600 samples"


async def test_a_rejected_optional_parameter_is_dropped_and_the_session_retried() -> None:
    """3006 is not one answer, and reading it as one cost the whole session.

    Verified live for the model case: ``speech_model=universal-2`` answers
    ``Invalid 'speech_model'``. A refused ``language_codes`` is the same code
    with a different name in it, and it is a nicety - dropping it and opening
    the session again keeps the transcript, where taking the provider off the
    streaming path lost it, and off the utterance path too, since both shapes
    build one query string.
    """
    paths: list[str] = []

    async def refusing_once(ws: ServerConnection) -> None:
        path = getattr(ws.request, "path", "")
        paths.append(path)
        if "language_codes" in path:
            await ws.send(
                json.dumps(
                    {
                        "type": "Error",
                        "error_code": 3006,
                        "error": "User Input Validation Error: Invalid 'language_codes'",
                    }
                )
            )
            await ws.close()
            return
        await assemblyai_handler(ws)

    async with serve(refusing_once, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        backend = AssemblyAIBackend(_config(port), api_key="secret")  # config.language: "de"
        await backend.open_stream(None)
        prepared = backend.prepare(None)  # the utterance shape stops sending it too
        await backend.aclose()

    assert len(paths) == 2
    assert "language_codes" in paths[0]
    assert "language_codes" not in paths[1]
    assert "language_codes" not in prepared
    assert "speaker_labels=true" in paths[1]  # only the one parameter was given up


async def test_a_session_shortly_before_its_cap_leaves_on_its_own() -> None:
    """v3 caps a session and a table runs for hours, so the cap is reached.

    ``Begin`` states the exact second the server will cut the session off.
    Leaving before it, and between turns, costs a reconnect and a gap marker on
    silence; being cut off costs the same reconnect in the middle of whatever
    somebody was saying. This is the same answer the Gemini connector gives to
    ``goAway``, from a different vendor's way of announcing it.
    """
    handler = functools.partial(assemblyai_handler, expires_in=_SESSION_END_MARGIN_S + 0.2)
    async with serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        backend = AssemblyAIBackend(_config(port), api_key="secret")
        await backend.open_stream(None)

        started = time.perf_counter()
        signals = [signal async for signal in backend.signals()]
        elapsed = time.perf_counter() - started
        await backend.aclose()

    # A quiet room sends no frames at all, so the deadline has to be raced
    # against the read rather than checked as frames arrive.
    assert elapsed < 3.0
    assert signals == []


async def test_the_cap_waits_for_the_open_turn_before_it_leaves() -> None:
    """Between turns, not inside one: a turn cut in half is a turn lost.

    The deadline passes while a turn is still open here, and what closes the
    connection is the turn ending rather than the clock.
    """

    class _SlowTurn:
        """One partial, then a pause past the deadline, then the turn's final."""

        def __aiter__(self) -> AsyncIterator[str]:
            return self._frames()

        async def _frames(self) -> AsyncIterator[str]:
            yield json.dumps(
                {
                    "type": "Turn",
                    "turn_order": 0,
                    "end_of_turn": False,
                    "transcript": "the goblin",
                    "words": [{"start": 0, "end": 400, "text": "the"}],
                }
            )
            await asyncio.sleep(0.3)
            yield json.dumps(
                {
                    "type": "Turn",
                    "turn_order": 0,
                    "end_of_turn": True,
                    "transcript": "the goblin takes",
                    "words": [{"start": 0, "end": 800, "text": "the"}],
                }
            )
            await asyncio.sleep(30)

    backend = AssemblyAIBackend(_config(0), api_key="secret")
    socket = cast("ClientConnection", _SlowTurn())
    backend._stream_ws = socket  # pyright: ignore[reportPrivateUsage]
    backend._leave_by = time.monotonic() + 0.1  # pyright: ignore[reportPrivateUsage]

    started = time.perf_counter()
    signals = [signal async for signal in backend.signals()]
    elapsed = time.perf_counter() - started

    assert [type(s).__name__ for s in signals] == ["TurnStarted", "TurnPartial", "TurnFinal"]
    assert 0.3 <= elapsed < 3.0  # left once the turn closed, not at the deadline


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
    """Nothing of the dead session may reach the one that replaces it.

    v3 numbers its turns per session, so the first turn after a dropped socket
    is ``turn_order`` 0 again, and the audio tail, the settled text a
    SpeakerRevision would republish and the expiry all belong to the session
    that stated them. The connection counter in the turn id is what keeps
    generation 2's first turn from replacing generation 1's.
    """
    connections = 0

    async def dropping(ws: ServerConnection) -> None:
        nonlocal connections
        connections += 1
        if connections == 1:
            await assemblyai_handler(cast("ServerConnection", _Hangup(ws, 12)))
            return
        await assemblyai_handler(ws)

    events = await _stream_events(
        [(_LOUD, 50), (_QUIET, 50), (_LOUD, 50), (_QUIET, 50)], handler=dropping, settle=0.2
    )

    assert connections == 2
    finals = [e for e in events if e.is_final and e.source == "aai"]
    assert finals
    assert any(str(e.turn_id).startswith("aai:2:0") for e in finals)
    assert [e.source for e in events if e.source == GAP_SOURCE] == [GAP_SOURCE]
