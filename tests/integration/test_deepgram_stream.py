"""The Deepgram streaming shape, end to end against the mock service.

The unit tests beside this one pin the translation of recorded vendor frames;
this drives the whole path a live capture takes: frames in at the capture rate,
the mock's own endpointing deciding the turns, and ``TranscriptEvent``s out
through :class:`TranscriptStream`. What it is really checking is the two things
Deepgram's protocol makes easy to get wrong: that one turn made of several
``is_final`` segments arrives as one event, and that every revision of it
carries the same key so the final replaces the interims instead of piling up
behind them.
"""

from __future__ import annotations

import asyncio
from http import HTTPStatus
from itertools import pairwise
from urllib.parse import parse_qs, urlparse

import pytest
from websockets.asyncio.server import Server, ServerConnection, serve
from websockets.http11 import Request, Response

from loreline.models import Glossary, ProviderConfig, ProviderKind, TranscriptEvent
from loreline.stt.backends.deepgram import DeepgramBackend
from loreline.stt.streaming import StreamConfig, StreamUnsupportedError, TranscriptStream
from mocks.deepgram_ws import deepgram_handler

_LOUD = b"\x00\x20" * 320  # 20 ms of 16 kHz mono, well above the mock's threshold
_QUIET = b"\x01\x00" * 320
_CAPTURE_RATE = 16000


def _config(port: int) -> ProviderConfig:
    return ProviderConfig(
        id="dg",
        name="Deepgram",
        kind=ProviderKind.DEEPGRAM,
        base_url=f"ws://127.0.0.1:{port}",
        sample_rate=_CAPTURE_RATE,
    )


def _port(server: Server) -> int:
    return int(server.sockets[0].getsockname()[1])


async def _drive(
    backend: DeepgramBackend,
    script: list[tuple[bytes, int]],
    *,
    start: float = 100.0,
) -> list[TranscriptEvent]:
    """Feed a script of (frame, count) through the streaming path."""
    events: list[TranscriptEvent] = []

    async def publish(event: TranscriptEvent) -> None:
        events.append(event)

    stream = TranscriptStream(
        backend,
        publish=publish,
        capture_rate=_CAPTURE_RATE,
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


async def _stream_events(script: list[tuple[bytes, int]]) -> list[TranscriptEvent]:
    async with serve(deepgram_handler, "127.0.0.1", 0) as server:
        backend = DeepgramBackend(_config(_port(server)), model="nova-3", api_key="secret")
        return await _drive(backend, script)


async def test_a_turn_of_several_final_segments_arrives_as_one_event() -> None:
    """Deepgram splits one turn into several ``is_final`` frames; we do not.

    Two turns of loud frames, separated by enough silence for the mock's
    endpointing to fire, and each long enough that the mock finalizes a segment
    mid-turn. A connector that published a segment would report four events.
    """
    events = await _stream_events([(_LOUD, 120), (_QUIET, 40), (_LOUD, 120), (_QUIET, 40)])

    finals = [e for e in events if e.is_final]
    interims = [e for e in events if not e.is_final]
    assert len(finals) == 2
    assert interims  # and text arrived while each turn was still open
    # Each final holds every word of its turn, not just the last segment's.
    assert all(len(e.text.split()) > 3 for e in finals)
    assert finals[0].start_ts < finals[1].start_ts
    assert all(e.end_ts > e.start_ts for e in finals)


async def test_every_revision_of_a_turn_carries_one_key() -> None:
    """The final replaces its interims rather than appending a second row."""
    events = await _stream_events([(_LOUD, 120), (_QUIET, 40)])

    keys = {e.turn_id for e in events}
    assert len(keys) == 1
    turn = [e for e in events if e.turn_id == next(iter(keys))]
    assert len(turn) > 1
    assert [e.is_final for e in turn] == [False] * (len(turn) - 1) + [True]
    # Interims restate the segment in full, so the turn's text only ever grows.
    assert all(later.text.startswith(earlier.text) for earlier, later in pairwise(turn))


async def test_a_turn_start_predates_its_first_word() -> None:
    """A turn starts where its first segment does, not where its text arrived.

    The timeline dots, the click-to-jump and the karaoke highlight all key off
    ``start_ts``, so a turn that claims to start where its first interim was
    published points a GM at the wrong second of the recording.
    """
    events = await _stream_events([(_QUIET, 20), (_LOUD, 120), (_QUIET, 40)])

    final = next(e for e in events if e.is_final)
    assert final.words
    assert final.start_ts <= final.words[0].start
    # ... and on the capture clock, not on Deepgram's count from the first byte.
    assert 100.0 <= final.start_ts < 101.0


async def test_words_carry_a_speaker_that_holds_for_a_whole_turn() -> None:
    """Inline diarization reads ``event.words``; this is what it gets."""
    events = await _stream_events([(_LOUD, 120), (_QUIET, 40), (_LOUD, 120), (_QUIET, 40)])

    finals = [e for e in events if e.is_final]
    assert all(len({w.speaker for w in e.words}) == 1 for e in finals)
    assert {w.speaker for e in finals for w in e.words} == {"Speaker 0", "Speaker 1"}


async def test_the_stream_asks_for_endpointing_interims_and_the_glossary() -> None:
    """The query string is the whole session configuration for this vendor."""
    seen: list[str] = []

    async def handler(websocket: ServerConnection) -> None:
        request = websocket.request
        seen.append(request.path if request is not None else "")
        await deepgram_handler(websocket)

    async with serve(handler, "127.0.0.1", 0) as server:
        backend = DeepgramBackend(_config(_port(server)), model="nova-3", api_key="secret")
        await backend.open_stream(Glossary(campaign_id="c", terms=["Drakonia"]))
        await backend.close_stream()

    params = parse_qs(urlparse(seen[0]).query)
    assert params["interim_results"] == ["true"]  # without it there are no partials
    assert params["endpointing"] == ["500"]
    assert "vad_events" not in params  # SpeechStarted lags a turn; see the connector
    assert params["utterance_end_ms"] == ["1000"]
    assert params["diarize"] == ["true"]
    assert params["model"] == ["nova-3"]
    assert params["keyterm"] == ["Drakonia"]  # nova-3's biasing field, not `keywords`
    assert params["sample_rate"] == ["16000"]


async def test_the_last_turn_is_flushed_when_the_microphone_stops() -> None:
    """A turn with no trailing silence still settles, through ``Finalize``.

    Deepgram's endpointing closes every turn but the one that was open when
    capture ended: nothing follows it to be silent. ``Finalize`` is the flush,
    and its ``from_finalize`` answer is what closes the turn here.
    """
    events = await _stream_events([(_LOUD, 120)])

    finals = [e for e in events if e.is_final]
    assert len(finals) == 1
    assert finals[0].text
    assert finals[0].words  # settled through the vendor, so the words survived


async def test_a_refused_request_is_not_retried() -> None:
    """A 400 is Deepgram saying no to the query string, and it will keep saying it.

    Live case: the Flux models speak a different protocol on ``/v2/listen`` and
    are not in this endpoint's model enum at all. Spending the stream's
    reconnect budget on that answer only delays handing the session back to the
    shape that does work.
    """

    def reject(connection: ServerConnection, request: Request) -> Response:
        _ = (connection, request)
        return connection.respond(HTTPStatus.BAD_REQUEST, '{"err_msg":"model not found"}')

    async with serve(deepgram_handler, "127.0.0.1", 0, process_request=reject) as server:
        backend = DeepgramBackend(_config(_port(server)), model="flux-general-en", api_key="x")
        with pytest.raises(StreamUnsupportedError, match="model not found"):
            await backend.open_stream(None)
