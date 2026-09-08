"""Integration tests for the Gemini Live backend via a mock WS server.

The mock replays frames recorded from the real service (see
mocks/gemini_live_ws.py), including the parts the docs get wrong: turns end
with generationComplete, and the server never closes the session.

Both connector shapes are covered: one session per utterance (ADR 0005) up to
the divider, then one session per capture with the service deciding the turns
(ADR 0006), which is what a live capture takes.
"""

from __future__ import annotations

import asyncio
import functools
import json
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from itertools import pairwise
from typing import cast

import pytest
from websockets.asyncio.client import ClientConnection
from websockets.asyncio.server import ServerConnection, serve

from loreline.audio.chunker import Utterance
from loreline.capability_config import TranscribeCapabilities
from loreline.models import GAP_SOURCE, Glossary, ProviderConfig, ProviderKind, TranscriptEvent
from loreline.stt.backends.gemini_live import (
    _RECV_TIMEOUT_S,  # pyright: ignore[reportPrivateUsage]
    GeminiLiveBackend,
)
from loreline.stt.base import transcribe_capabilities
from loreline.stt.streaming import StreamConfig, StreamUnsupportedError, TranscriptStream
from mocks.gemini_live_ws import gemini_live_handler

# 8000 samples is 0.5 s at 16 kHz, which the connector sends as five paced
# 100 ms chunks: enough send-side read windows for the mock's mid-stream turn
# to land in one of them, as it does in a real session.
_SAMPLES = 8000


def _one_utterance() -> Utterance:
    return Utterance(pcm=b"\x01\x00" * _SAMPLES, start=10.0, end=10.5)


def _two_utterances() -> list[Utterance]:
    return [
        Utterance(pcm=b"\x01\x00" * _SAMPLES, start=0.0, end=0.5),
        Utterance(pcm=b"\x02\x00" * _SAMPLES, start=0.5, end=1.0),
    ]


# The registry resolves the model and hands it over; the connector keeps none
# of its own. capabilities.yaml declares Gemini's default, and this model is
# hidden there until the connector is verified against the real service, so it
# only ever arrives from a config that names it.
MODEL = "gemini-3.5-transcribe-live"


def _caps() -> TranscribeCapabilities | None:
    """What the registry resolves for this model and hands to the connector.

    Read from capabilities.yaml exactly as ``create_backend`` reads it, so the
    glossary ceiling these tests exercise is the file's, not a number restated
    here.
    """
    return transcribe_capabilities(ProviderKind.GEMINI, MODEL)


def _config(port: int, language: str = "de") -> ProviderConfig:
    return ProviderConfig(
        id="gem-live-1",
        name="Gemini Live",
        kind=ProviderKind.GEMINI,
        base_url=f"ws://127.0.0.1:{port}",
        language=language,
    )


async def test_gemini_live_utterance_transcribe() -> None:
    async with serve(gemini_live_handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        backend = GeminiLiveBackend(_config(port), model=MODEL, caps=_caps(), api_key="secret")
        glossary = Glossary(campaign_id="c1", terms=["Drakonia"])
        event = await backend.transcribe(_one_utterance(), session_id="s1", glossary=glossary)

    assert event is not None
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
        event = await backend.transcribe(_one_utterance(), session_id="s1")
        elapsed = time.perf_counter() - started

    assert event is not None
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
        backend = GeminiLiveBackend(_config(port), model=MODEL, caps=_caps(), api_key="sekret")
        _ = await backend.transcribe(_one_utterance(), session_id="s1")

    assert "key=sekret" in cast("str", seen["path"])
    setup = cast("dict[str, object]", seen["setup"])
    assert setup["model"] == f"models/{MODEL}"
    assert setup["generationConfig"] == {"responseModalities": ["TEXT"]}
    assert setup["inputAudioTranscription"] == {"languageCodes": ["de"]}
    assert seen["mime"] == "audio/pcm;rate=16000"


# ---------------------------------------------------------------------------
# Custom vocabulary. This connector used to log "glossary_unsupported" and send
# nothing, on the reading that the Live API documents no such parameter. That
# was wrong, and the correction was measured against the real service rather
# than re-read out of the docs: setup.inputAudioTranscription.customVocabulary
# is accepted, the service rejects unknown sibling fields outright ('Unknown
# name "zzzNotAField" ... Cannot find field.') so the ack means something, and
# the transcript changes ("Cape Morgion" -> "Cape Morgiou", "the old Foxy
# docks" -> "the old Phocee docks"). See capabilities.yaml for the full record.
# ---------------------------------------------------------------------------


async def _setup_frame(
    port: int, glossary: Glossary | None, setups: list[dict[str, object]]
) -> dict[str, object]:
    backend = GeminiLiveBackend(_config(port), model=MODEL, caps=_caps(), api_key="secret")
    _ = await backend.transcribe(_one_utterance(), session_id="s1", glossary=glossary)
    return cast("dict[str, object]", setups[0]["inputAudioTranscription"])


async def test_glossary_rides_in_the_setup_as_custom_vocabulary() -> None:
    setups: list[dict[str, object]] = []
    handler = functools.partial(gemini_live_handler, setups=setups)
    async with serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        glossary = Glossary(campaign_id="c1", terms=["Drakonia", "Thalric"])
        transcription = await _setup_frame(port, glossary, setups)

    # lowerCamelCase, like every other field this connector sends. The service
    # accepts custom_vocabulary too, but proto-JSON emits the camel spelling.
    assert transcription["customVocabulary"] == ["Drakonia", "Thalric"]
    assert transcription["languageCodes"] == ["de"]


async def test_no_glossary_sends_no_custom_vocabulary_at_all() -> None:
    """An empty list is not the same message as an absent field, and a session
    without a glossary must send exactly what it sent before this existed."""
    setups: list[dict[str, object]] = []
    handler = functools.partial(gemini_live_handler, setups=setups)
    async with serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        transcription = await _setup_frame(port, None, setups)

    assert "customVocabulary" not in transcription


async def test_glossary_is_capped_at_the_ceiling_the_service_enforces() -> None:
    """Over 1000 entries the service closes the socket with 1007
    "custom_vocabulary cannot contain more than 1000 entries", which costs the
    whole utterance rather than just the surplus terms. Measured: 1000 acks,
    1001 closes. capabilities.yaml carries the number."""
    setups: list[dict[str, object]] = []
    handler = functools.partial(gemini_live_handler, setups=setups)
    async with serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        glossary = Glossary(campaign_id="c1", terms=[f"term{i}" for i in range(1200)])
        transcription = await _setup_frame(port, glossary, setups)

    terms = cast("list[str]", transcription["customVocabulary"])
    assert len(terms) == 1000
    # Glossary order is priority order, so the head survives.
    assert terms[0] == "term0"


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
        backend = GeminiLiveBackend(_config(port), model=MODEL, caps=_caps(), api_key="secret")
        events = [
            await backend.transcribe(utterance, session_id="s1") for utterance in _two_utterances()
        ]
        await backend.aclose()

    assert [e.text for e in events if e] == [f"gemini live mock {_SAMPLES} samples"] * 2
    assert connections == 2


# ---------------------------------------------------------------------------
# The streaming shape (ADR 0006): one session for a whole capture, turns cut by
# the service's own activity detection, and a connection that ends on schedule
# because this vendor caps a session in minutes.
# ---------------------------------------------------------------------------

_LOUD = b"\x00\x20" * 320  # 20 ms at 16 kHz, well above the mock's threshold
_QUIET = b"\x01\x00" * 320


async def _stream_events(
    handler: Callable[[ServerConnection], Awaitable[None]],
    script: list[tuple[bytes, int]],
    *,
    start: float = 100.0,
    settle: float = 0.3,
) -> list[TranscriptEvent]:
    """Run the streaming path over a script of (frame, count) against a mock.

    A pause between blocks, not just between frames: a reconnect has to happen
    somewhere, and a feed that never yields long enough for one would test the
    queue's drop policy instead of the reconnect.
    """
    async with serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        backend = GeminiLiveBackend(_config(port), model=MODEL, caps=_caps(), api_key="secret")
        events: list[TranscriptEvent] = []

        async def publish(event: TranscriptEvent) -> None:
            events.append(event)

        stream = TranscriptStream(
            backend,
            publish=publish,
            capture_rate=16000,  # == the Live API's only input rate: no resampler
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
            await asyncio.sleep(settle)
        stream.stop()
        await asyncio.wait_for(task, 10)
        await backend.aclose()
    return events


async def test_streaming_publishes_interims_then_a_final_per_service_turn() -> None:
    """The point of ADR 0006 for this vendor: text while somebody is still
    speaking, from Google's endpointing rather than 800 ms after they stop."""
    events = await _stream_events(gemini_live_handler, [(_LOUD, 50), (_QUIET, 50)] * 2)

    finals = [e for e in events if e.is_final]
    interims = [e for e in events if not e.is_final]
    assert len(finals) == 2  # two turns, cut by the mock's own silence rule
    assert interims  # and text before each one closed
    assert all(e.text for e in events)

    first, second = finals
    assert first.turn_id != second.turn_id
    # The vendor names no turns, so the connector numbers them itself, behind
    # the provider id and this connection's number. Its own numbering rather
    # than the turn's start, because this service can settle one turn twice
    # and only a ref makes the second one replace the first.
    assert (first.turn_id, second.turn_id) == ("gem-live-1:1:1", "gem-live-1:1:2")
    # Timestamps land on the capture clock, from the last frame written when
    # each signal arrived: this service states no offsets at all.
    assert 100.0 <= first.start_ts < second.start_ts
    assert first.end_ts >= first.start_ts
    # Google: "Speaker diarization is not supported in live streaming
    # sessions", and no frame carries word timings either.
    assert all(e.words == [] for e in events)


async def test_streaming_replaces_a_turn_rather_than_appending_to_it() -> None:
    """Interims are cumulative here, so every revision of one turn carries one
    key and the text only grows. Appending them would repeat every word."""
    events = await _stream_events(gemini_live_handler, [(_LOUD, 50), (_QUIET, 50)])

    turn = [e for e in events if e.turn_id == events[0].turn_id]
    assert len(turn) > 1  # several interims and then the final
    assert [e.is_final for e in turn] == [False] * (len(turn) - 1) + [True]
    assert all(later.text.startswith(earlier.text) for earlier, later in pairwise(turn))


async def test_streaming_settles_the_open_turn_when_the_microphone_stops() -> None:
    """The one turn nothing else closes: flush_input says audioStreamEnd, and
    the service finalizes without waiting out its own silence threshold."""
    events = await _stream_events(gemini_live_handler, [(_LOUD, 50)])

    finals = [e for e in events if e.is_final]
    assert [e.text for e in finals] == ["the goblin takes the chest"]


async def test_streaming_setup_asks_for_the_services_own_endpointing() -> None:
    """The session config is what makes Google, not a local VAD, cut turns."""
    setups: list[dict[str, object]] = []
    handler = functools.partial(gemini_live_handler, setups=setups)
    async with serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        backend = GeminiLiveBackend(_config(port), model=MODEL, caps=_caps(), api_key="secret")
        await backend.open_stream(Glossary(campaign_id="c1", terms=["Drakonia"]))
        await backend.close_stream()

    setup = setups[0]
    realtime = cast("dict[str, object]", setup["realtimeInputConfig"])
    detection = cast("dict[str, object]", realtime["automaticActivityDetection"])
    assert detection["disabled"] is False
    # Below Google's recommended 500-800 ms, and measured: see _AUTOMATIC_VAD.
    assert detection["silenceDurationMs"] == 300
    assert detection["startOfSpeechSensitivity"] == "START_SENSITIVITY_HIGH"
    # The session carries the glossary, so a reconnect re-applies it for free.
    transcription = cast("dict[str, object]", setup["inputAudioTranscription"])
    assert transcription["customVocabulary"] == ["Drakonia"]
    # Asked for on the first connection too: a handle only exists once the
    # server has offered one, and it only offers when asked.
    assert setup["sessionResumption"] == {}


async def test_a_model_that_refuses_activity_detection_cannot_stream() -> None:
    """This service says no by closing with 1007 and naming the field path.

    A refusal is final, so it is StreamUnsupportedError rather than one more
    failed attempt: the session then runs this same connector one utterance at
    a time, which is what it did before streaming existed.
    """

    async def refusing(ws: ServerConnection) -> None:
        async for _ in ws:
            await ws.close(
                code=1007,
                reason=(
                    "Invalid JSON payload received. Unknown name "
                    "\"automatic_activity_detection\" at 'setup.realtime_input_config'"
                ),
            )
            return

    async with serve(refusing, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        backend = GeminiLiveBackend(_config(port), model=MODEL, caps=_caps(), api_key="secret")
        with pytest.raises(StreamUnsupportedError):
            await backend.open_stream(None)
        await backend.aclose()


async def test_go_away_ends_the_connection_and_the_next_one_starts_clean() -> None:
    """A session cap of minutes against an evening at a table.

    goAway is ordinary operation for this vendor, so it has to cost the open
    turn and a gap marker rather than the session. What the second connection
    must not inherit is any of the first one's bookkeeping: its turn numbering
    starts at one again, behind a connection counter that does not, so the
    evening's fifth turn follows the fourth instead of replacing the first.

    No resumption handle here, because the real service has never sent one:
    the default mock answers as it does, and the handle path has its own test
    below.
    """
    connections = 0
    handles: list[str | None] = []

    async def counting(ws: ServerConnection) -> None:
        nonlocal connections
        connections += 1
        # Only the first session goes away, so what the test counts is one
        # reconnect rather than however many fit in the run.
        await gemini_live_handler(
            ws, handles=handles, go_away_after_turns=1 if connections == 1 else None
        )

    events = await _stream_events(counting, [(_LOUD, 50), (_QUIET, 50)] * 2, settle=0.5)

    assert connections == 2
    # Both sessions presented nothing, since neither was ever offered a handle.
    assert handles == [None, None]
    finals = [e for e in events if e.is_final and e.source == "gem-live-1"]
    assert finals
    # Generation 2 numbers its turns from scratch, and the connection counter
    # in front of them is what keeps that from replacing generation 1's rows.
    assert {e.turn_id for e in finals} <= {f"gem-live-1:{gen}:1" for gen in (1, 2)} | {
        f"gem-live-1:{gen}:2" for gen in (1, 2)
    }
    assert any(str(e.turn_id).startswith("gem-live-1:2:1") for e in finals)
    # What the reconnect swallowed is a span aligned to nothing, so it is a row
    # in the transcript rather than a line in the log (ADR 0006, Decision 3).
    assert [e.source for e in events if e.source == GAP_SOURCE] == [GAP_SOURCE]


async def test_a_resumption_handle_is_presented_to_the_connection_after_it() -> None:
    """The path that would work the day Google starts issuing handles.

    It never has, in any verification run, which is why the mock only offers
    one when asked. The code stays because it costs one field in a message
    that is sent anyway; this is what says the code is still wired up.
    """
    connections = 0
    handles: list[str | None] = []

    async def counting(ws: ServerConnection) -> None:
        nonlocal connections
        connections += 1
        await gemini_live_handler(
            ws,
            handles=handles,
            offer_handles=True,
            go_away_after_turns=1 if connections == 1 else None,
        )

    _ = await _stream_events(counting, [(_LOUD, 50), (_QUIET, 50)] * 2, settle=0.5)

    assert connections == 2
    # None on the first session (there was nothing to resume), then the handle
    # the first session was given.
    assert handles == [None, "h+1"]


async def test_a_quiet_room_still_leaves_before_the_deadline_passes() -> None:
    """The deadline is raced against the read rather than checked per frame.

    A table that has gone quiet with a turn still open sends nothing at all,
    and a goAway acted on only when the next frame arrives is a goAway the
    server beats: it hangs up at its own moment, which is mid-turn. Two
    frames here and then a socket that says nothing ever again.
    """

    class _GoesQuiet:
        """One interim, a goAway inside that turn, then silence for as long as anyone waits."""

        def __aiter__(self) -> AsyncIterator[str]:
            return self._frames()

        async def _frames(self) -> AsyncIterator[str]:
            # In that order: a goAway with no turn open is left on at once,
            # which is already a turn boundary. The case that needs a deadline
            # is a turn the room then stops feeding.
            yield json.dumps({"serverContent": {"interimInputTranscription": {"text": "hallo"}}})
            yield json.dumps({"goAway": {"timeLeft": "5.2s"}})  # 0.2 s past the margin
            await asyncio.sleep(30)

    backend = GeminiLiveBackend(_config(0), model=MODEL, api_key="secret")
    quiet = cast("ClientConnection", _GoesQuiet())
    backend._stream_ws = quiet  # pyright: ignore[reportPrivateUsage]

    started = time.perf_counter()
    signals = [signal async for signal in backend.signals()]
    elapsed = time.perf_counter() - started

    assert elapsed < 3.0  # the deadline, not the next frame that never came
    assert [type(s).__name__ for s in signals] == ["TurnPartial", "StreamAlive"]


async def test_the_utterance_shape_is_untouched_by_the_streaming_one() -> None:
    """ADR 0005's contract still serves reprocess and the call-shaped fallback,
    and its events carry no turn id because nothing replaces them."""
    async with serve(gemini_live_handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        backend = GeminiLiveBackend(_config(port), model=MODEL, caps=_caps(), api_key="secret")
        event = await backend.transcribe(_one_utterance(), session_id="s1")
        await backend.aclose()

    assert event is not None
    assert event.is_final
    assert event.turn_id is None


async def test_go_away_mid_turn_waits_for_the_turn_to_close() -> None:
    """50 seconds of notice is many turns' worth, so none of them is spent.

    Leaving on the spot would settle the open turn from its newest interim and
    put the audio still in flight into the gap. Waiting for the service to close
    the turn it was already closing costs nothing and keeps both.
    """

    async def going_away(ws: ServerConnection) -> None:
        # Zero: the goAway lands on the first audio frame, one interim into a
        # turn that has six more words to go.
        await gemini_live_handler(ws, go_away_after_turns=0)

    events = await _stream_events(going_away, [(_LOUD, 100), (_QUIET, 50)], settle=0.5)

    finals = [e for e in events if e.is_final and e.source == "gem-live-1"]
    assert [e.text for e in finals] == ["the goblin takes the chest and runs"]
    gaps = [e for e in events if e.source == GAP_SOURCE]
    assert len(gaps) == 1
    # The span the reconnect swallowed is the reconnect, not the two seconds of
    # speech that were in flight when the notice arrived.
    assert gaps[0].end_ts - gaps[0].start_ts < 0.5
