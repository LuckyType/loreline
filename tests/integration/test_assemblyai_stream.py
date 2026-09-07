"""The AssemblyAI streaming shape end to end, against the mock v3 endpoint.

``test_assemblyai_frames.py`` pins the translation of recorded real messages;
this drives the whole path a live capture takes - a socket, the 50 ms audio
floor the endpoint enforces, its own endpointing deciding the turns, and
``TranscriptStream`` turning the signals back into events on the session clock.
"""

from __future__ import annotations

import asyncio
import json
from urllib.parse import parse_qs, urlparse

import pytest
from websockets.asyncio.server import ServerConnection, serve

from loreline.models import Glossary, ProviderConfig, ProviderKind, TranscriptEvent
from loreline.stt.backends.assemblyai import AssemblyAIBackend
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
    script: list[tuple[bytes, int]], *, start: float = 100.0
) -> list[TranscriptEvent]:
    """Run the streaming path over a script of (frame, count) against the mock."""
    async with serve(assemblyai_handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        backend = AssemblyAIBackend(_config(port), api_key="secret")
        events: list[TranscriptEvent] = []

        async def publish(event: TranscriptEvent) -> None:
            events.append(event)

        stream = TranscriptStream(
            backend,
            publish=publish,
            capture_rate=16000,
            config=StreamConfig(session_id="s1", interim_interval_s=0.0, final_wait_s=2.0),
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
