"""The streaming contract's own behaviour, with a fake vendor.

Everything a connector is promised it does not have to do (ADR 0006): the
capture clock, interim throttling, the reconnect budget, the liveness watchdog,
the gap marker, and the two shapes a failover can hand a session to. A fake
connector stands in for the vendor so each of those is driven directly rather
than through somebody's protocol.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from dataclasses import replace

import pytest

from loreline.audio.chunker import Utterance
from loreline.bus import EventBus
from loreline.models import (
    GAP_SOURCE,
    DiarizationConfig,
    DiarizationMode,
    Glossary,
    ProviderConfig,
    ProviderKind,
    SpeakerSegment,
    TranscriptEvent,
    Word,
)
from loreline.session import streaming as session_streaming
from loreline.session.streaming import PathEnd, StreamPath
from loreline.stt import streaming as stt_streaming
from loreline.stt.streaming import (
    StreamAlive,
    StreamConfig,
    StreamingConnector,
    StreamOutcome,
    StreamSignal,
    StreamUnsupportedError,
    TranscriptStream,
    TurnEnded,
    TurnFinal,
    TurnPartial,
    TurnSignal,
    TurnStarted,
    is_streaming,
)

_FRAME = b"\x00\x10" * 320  # 20 ms of 16 kHz mono
_END = object()


def _at(seconds: float) -> float:
    """A capture timestamp rounded past the drift of adding 0.02 to itself."""
    return round(seconds, 6)


def _provider(provider_id: str = "p1") -> ProviderConfig:
    return ProviderConfig(
        id=provider_id, name=provider_id.upper(), kind=ProviderKind.OPENAI, sample_rate=16000
    )


class FakeStreaming(StreamingConnector):
    """A vendor whose messages the test writes by hand.

    ``emit`` queues a signal for the current connection; ``end`` closes the
    socket the way a vendor hanging up does. ``open_fails`` is how a connection
    dies without ever saying anything, and ``unsupported`` is how a vendor says
    it will not stream this model at all.

    It satisfies ``STTBackend`` as well, because the connector it stands in for
    does: ``OpenAIRealtimeBackend`` has both shapes, which is what lets a
    session whose model cannot be streamed keep the provider it configured.
    ``StreamingOnly`` below is the other case.
    """

    def __init__(
        self,
        config: ProviderConfig | None = None,
        *,
        open_fails: int = 0,
        unsupported: bool = False,
    ) -> None:
        self.config = config or _provider()
        self._unsupported = unsupported
        self.opens = 0
        self.flushes = 0
        self.closes = 0
        self.sent: list[bytes] = []
        self.glossary: Glossary | None = None
        self._open_fails = open_fails
        self._inbox: asyncio.Queue[object] = asyncio.Queue()

    @property
    def stream_rate(self) -> int:
        return 16000

    async def open_stream(self, glossary: Glossary | None) -> None:
        self.opens += 1
        self.glossary = glossary
        if self._unsupported:
            msg = "this model does not support server-side turn detection"
            raise StreamUnsupportedError(msg)
        if self.opens <= self._open_fails:
            msg = "cannot connect"
            raise ConnectionError(msg)

    async def send_audio(self, pcm: bytes) -> None:
        self.sent.append(pcm)

    async def signals(self) -> AsyncIterator[StreamSignal]:
        while True:
            item = await self._inbox.get()
            if item is _END:
                return
            assert isinstance(item, (TurnStarted, TurnPartial, TurnEnded, TurnFinal, StreamAlive))
            yield item

    async def flush_input(self) -> None:
        self.flushes += 1

    async def close_stream(self) -> None:
        self.closes += 1

    async def transcribe(
        self,
        utterance: Utterance,
        *,
        session_id: str,
        glossary: Glossary | None = None,
    ) -> TranscriptEvent | None:
        _ = (utterance, session_id, glossary)
        return None

    async def aclose(self) -> None:
        return None

    def emit(self, signal: StreamSignal) -> None:
        self._inbox.put_nowait(signal)

    def end(self) -> None:
        self._inbox.put_nowait(_END)


class StreamingOnly(StreamingConnector):
    """A connector with the streaming shape and no other, unlike OpenAI's."""

    def __init__(self, provider_id: str = "p3") -> None:
        self.config = _provider(provider_id)

    @property
    def stream_rate(self) -> int:
        return 16000

    async def open_stream(self, glossary: Glossary | None) -> None:
        _ = glossary
        msg = "this model does not support server-side turn detection"
        raise StreamUnsupportedError(msg)

    async def send_audio(self, pcm: bytes) -> None:
        _ = pcm

    async def signals(self) -> AsyncIterator[TurnSignal]:
        return
        yield  # pragma: no cover - never reached; makes this an async generator

    async def flush_input(self) -> None:
        return

    async def close_stream(self) -> None:
        return


class CallShaped:
    """A backend that satisfies ``STTBackend`` and nothing else."""

    def __init__(self, provider_id: str = "p2") -> None:
        self.config = _provider(provider_id)
        self.seen: list[Utterance] = []

    async def transcribe(
        self,
        utterance: Utterance,
        *,
        session_id: str,
        glossary: Glossary | None = None,
    ) -> TranscriptEvent | None:
        _ = glossary
        self.seen.append(utterance)
        return TranscriptEvent(
            session_id=session_id,
            source=self.config.id,
            text="from the fallback",
            start_ts=utterance.start,
            end_ts=utterance.end,
            is_final=True,
        )

    async def aclose(self) -> None:
        return None


class _Collector:
    """The publish callback, keeping what a stream produced."""

    def __init__(self) -> None:
        self.events: list[TranscriptEvent] = []

    async def __call__(self, event: TranscriptEvent) -> None:
        self.events.append(event)

    def finals(self, source: str = "p1") -> list[TranscriptEvent]:
        return [e for e in self.events if e.is_final and e.source == source]

    def interims(self) -> list[TranscriptEvent]:
        return [e for e in self.events if not e.is_final]


class _Bus:
    """The session bus, as far as ``StreamPath`` uses it: one ``publish``."""

    def __init__(self) -> None:
        self.events: list[TranscriptEvent] = []

    async def publish(self, event: TranscriptEvent) -> None:
        self.events.append(event)

    def finals(self, source: str = "p1") -> list[TranscriptEvent]:
        return [e for e in self.events if e.is_final and e.source == source]

    def gaps(self) -> list[TranscriptEvent]:
        return [e for e in self.events if e.source == GAP_SOURCE]


class FakeDiarizer:
    """The remote diarizer, as far as the streaming path uses it.

    ``fails`` stands in for every way a real one refuses - any non-2xx, a
    timeout, a refused connection - since ``RemoteDiarizer.diarize`` raises for
    all of them. ``gate`` holds an answer back, which is how a test tells
    "published before the diarizer answered" from "published after".
    """

    def __init__(
        self,
        segments: list[SpeakerSegment] | None = None,
        *,
        fails: bool = False,
        gate: asyncio.Event | None = None,
    ) -> None:
        self._segments = segments or []
        self._fails = fails
        self._gate = gate
        self.calls = 0
        self.clips: list[bytes] = []

    async def diarize(
        self,
        wav: bytes,
        *,
        sample_rate: int = 16000,
        min_speakers: int | None = None,
        max_speakers: int | None = None,
        session_id: str | None = None,
    ) -> list[SpeakerSegment]:
        _ = (sample_rate, min_speakers, max_speakers, session_id)
        self.calls += 1
        self.clips.append(wav)
        if self._gate is not None:
            await self._gate.wait()
        if self._fails:
            msg = "the diarization service answered 503"
            raise RuntimeError(msg)
        return list(self._segments)

    async def aclose(self) -> None:
        return None


async def _settle(times: int = 12) -> None:
    """Let the stream's tasks run without pinning the test to wall time."""
    for _ in range(times):
        await asyncio.sleep(0)


async def _until(condition: Callable[[], bool], *, limit: float = 2.0) -> None:
    """Wait for something the stream's own tasks bring about.

    Polled rather than signalled because what is being waited for is a
    reconnect deep inside the stream, which has no event to offer and should
    not grow one for a test's sake.
    """
    async with asyncio.timeout(limit):
        while not condition():  # noqa: ASYNC110 - see the docstring
            await asyncio.sleep(0.001)


def _stream(connector: FakeStreaming, sink: _Collector, **overrides: object) -> TranscriptStream:
    """A stream with the two waits shortened, since no test wants to sit them out."""
    base = StreamConfig(session_id="s1", final_wait_s=0.05, reconnect_backoff_s=0.0)
    return TranscriptStream(
        connector, publish=sink, capture_rate=16000, config=replace(base, **overrides)
    )


async def _feed(stream: TranscriptStream, count: int, start: float, *, speech: bool = True) -> None:
    ts = start
    for _ in range(count):
        stream.feed(_FRAME, ts, is_speech=speech)
        ts += 0.02
        await _settle(3)


def test_a_streaming_connector_is_recognised_by_shape() -> None:
    assert is_streaming(FakeStreaming())
    assert not is_streaming(CallShaped())  # pyright: ignore[reportArgumentType]


async def test_vendor_offsets_land_on_the_capture_clock() -> None:
    """t0 is the first frame written, so an offset is t0 + offset and nothing else."""
    fake = FakeStreaming()
    sink = _Collector()
    stream = _stream(fake, sink)
    task = asyncio.create_task(stream.run())
    await _feed(stream, 5, start=100.0)

    fake.emit(TurnEnded(at=1.5, ref="a"))  # arrives before the turn it names
    fake.emit(TurnFinal(text="hello there", ref="a", at=0.5))
    await _settle()
    stream.stop()
    assert await asyncio.wait_for(task, 2) == StreamOutcome.ENDED

    (event,) = sink.finals()
    assert event.start_ts == 100.5
    assert event.end_ts == 101.5
    assert event.source == "p1"
    assert event.turn_id == "p1:1:a"


async def test_a_signal_with_no_offset_falls_back_to_the_last_frame() -> None:
    fake = FakeStreaming()
    sink = _Collector()
    stream = _stream(fake, sink)
    task = asyncio.create_task(stream.run())
    await _feed(stream, 4, start=50.0)

    fake.emit(TurnFinal(text="no timing at all", ref="x"))
    await _settle()
    stream.stop()
    await asyncio.wait_for(task, 2)

    (event,) = sink.finals()
    assert round(event.start_ts, 6) == _at(50.06)  # the last of the four frames written


async def test_interims_are_throttled_and_the_final_replaces_them() -> None:
    fake = FakeStreaming()
    sink = _Collector()
    stream = _stream(fake, sink, interim_interval_s=60.0)
    task = asyncio.create_task(stream.run())
    await _feed(stream, 2, start=10.0)

    fake.emit(TurnStarted(at=0.0, ref="t"))
    for word in ("one", " two", " three"):
        fake.emit(TurnPartial(text=word, ref="t", append=True))
    await _settle()
    fake.emit(TurnFinal(text="one two three", ref="t"))
    await _settle()
    stream.stop()
    await asyncio.wait_for(task, 2)

    assert [e.text for e in sink.interims()] == ["one"]  # the rest fell inside the window
    assert [e.text for e in sink.finals()] == ["one two three"]
    # One turn, one key: the final replaces the interim rather than following it.
    assert {e.turn_id for e in sink.events} == {"p1:1:t"}


async def test_every_partial_publishes_when_throttling_is_off() -> None:
    fake = FakeStreaming()
    sink = _Collector()
    stream = _stream(fake, sink, interim_interval_s=0.0)
    task = asyncio.create_task(stream.run())
    await _feed(stream, 2, start=10.0)

    fake.emit(TurnPartial(text="a", ref="t", append=True))
    fake.emit(TurnPartial(text="b", ref="t", append=True))
    await _settle()
    stream.stop()
    await asyncio.wait_for(task, 2)

    assert [e.text for e in sink.interims()] == ["a", "ab"]


async def test_a_partial_that_replaces_does_not_accumulate() -> None:
    """``append=False`` is the vendor resending the whole interim every time."""
    fake = FakeStreaming()
    sink = _Collector()
    stream = _stream(fake, sink, interim_interval_s=0.0)
    task = asyncio.create_task(stream.run())
    await _feed(stream, 2, start=10.0)

    fake.emit(TurnPartial(text="the go", ref="t"))
    fake.emit(TurnPartial(text="the goblin", ref="t"))
    await _settle()
    stream.stop()
    await asyncio.wait_for(task, 2)

    assert [e.text for e in sink.interims()] == ["the go", "the goblin"]


async def test_a_turn_still_open_when_the_mic_stops_is_settled_not_left_interim() -> None:
    fake = FakeStreaming()
    sink = _Collector()
    stream = _stream(fake, sink, interim_interval_s=0.0)
    task = asyncio.create_task(stream.run())
    await _feed(stream, 2, start=10.0)

    fake.emit(TurnPartial(text="half a sen", ref="t", append=True))
    await _settle()
    stream.stop()
    assert await asyncio.wait_for(task, 2) == StreamOutcome.ENDED

    assert fake.flushes == 1  # the vendor was asked to flush before we gave up
    last = sink.events[-1]
    assert last.is_final
    assert last.text == "half a sen"
    assert last.turn_id == sink.events[0].turn_id  # replaces its own interim


async def test_an_unnamed_signal_belongs_to_the_one_open_turn() -> None:
    """A vendor with no turn ids has one turn open at a time, which is the fallback."""
    fake = FakeStreaming()
    sink = _Collector()
    stream = _stream(fake, sink, interim_interval_s=0.0)
    task = asyncio.create_task(stream.run())
    await _feed(stream, 2, start=10.0)

    fake.emit(TurnStarted(at=0.0))
    fake.emit(TurnPartial(text="unnamed", append=True))
    fake.emit(TurnFinal(text="unnamed turn"))
    await _settle()
    fake.emit(TurnFinal(text="a second one"))
    await _settle()
    stream.stop()
    await asyncio.wait_for(task, 2)

    finals = sink.finals()
    assert [e.text for e in finals] == ["unnamed turn", "a second one"]
    assert finals[0].turn_id != finals[1].turn_id  # two turns, not one revised


async def test_a_dropped_socket_reconnects_and_marks_the_gap() -> None:
    fake = FakeStreaming()
    sink = _Collector()
    stream = _stream(fake, sink)
    task = asyncio.create_task(stream.run())
    await _feed(stream, 3, start=200.0)  # 200.00, 200.02, 200.04

    fake.end()  # the vendor hangs up
    stream.feed(_FRAME, 200.06, is_speech=True)  # the frame that finds it dead
    await _until(lambda: fake.opens == 2)
    await _feed(stream, 3, start=205.0)
    fake.emit(TurnFinal(text="after the break", ref="b"))
    await _settle()
    stream.stop()
    assert await asyncio.wait_for(task, 2) == StreamOutcome.ENDED

    assert fake.closes == 2  # both connections were closed exactly once
    (gap,) = [e for e in sink.events if e.source == GAP_SOURCE]
    assert round(gap.start_ts, 6) == _at(200.04)  # the last frame written before the drop
    assert round(gap.end_ts, 6) == _at(205.0)  # the first frame written after it
    assert gap.is_final
    assert "not transcribed" in gap.text
    # A turn after the reconnect cannot collide with one from before it: OpenAI
    # numbers its items per session, so the handles repeat and the generation
    # is what keeps them apart.
    assert [e.turn_id for e in sink.finals()] == ["p1:2:b"]


async def test_a_provider_that_never_connects_is_declared_dead() -> None:
    fake = FakeStreaming(open_fails=99)
    sink = _Collector()
    stream = _stream(fake, sink, max_reconnects=2)

    assert await asyncio.wait_for(stream.run(), 2) == StreamOutcome.DEAD
    assert fake.opens == 3  # the first try plus the budget


async def test_the_watchdog_fires_once_a_turn_has_ended_with_no_answer() -> None:
    fake = FakeStreaming()
    sink = _Collector()
    stream = _stream(fake, sink, watchdog_s=0.01, quiet_grace_s=0.02, max_reconnects=0)
    task = asyncio.create_task(stream.run())

    ts = 300.0
    for i in range(40):
        # Speech, and then the pause that ends it: only past a turn boundary
        # does "the vendor said nothing" mean the connection is gone.
        stream.feed(_FRAME, ts, is_speech=i < 20)
        ts += 0.02
        await asyncio.sleep(0.003)
    assert await asyncio.wait_for(task, 2) == StreamOutcome.DEAD
    assert fake.opens == 1  # no budget to reconnect with


async def test_the_watchdog_does_not_interrupt_a_long_turn() -> None:
    """Measured against OpenAI: a transcription session says nothing at all for
    the whole length of a turn, then everything at once when it closes. A
    watchdog that counted only voiced audio killed a twelve second turn
    mid-sentence and lost the nineteen seconds that followed to a gap."""
    fake = FakeStreaming()
    sink = _Collector()
    stream = _stream(fake, sink, watchdog_s=0.01, quiet_grace_s=5.0)
    task = asyncio.create_task(stream.run())

    ts = 300.0
    for _ in range(40):
        stream.feed(_FRAME, ts, is_speech=True)  # nobody has paused yet
        ts += 0.02
        await asyncio.sleep(0.003)
    stream.stop()
    assert await asyncio.wait_for(task, 2) == StreamOutcome.ENDED
    assert fake.opens == 1


async def test_the_watchdog_leaves_a_silent_room_alone() -> None:
    """A server-VAD session says nothing at all while nobody speaks."""
    fake = FakeStreaming()
    sink = _Collector()
    stream = _stream(fake, sink, watchdog_s=0.01)
    task = asyncio.create_task(stream.run())

    ts = 400.0
    for _ in range(40):
        stream.feed(_FRAME, ts, is_speech=False)
        ts += 0.02
        await asyncio.sleep(0.002)
    stream.stop()
    assert await asyncio.wait_for(task, 2) == StreamOutcome.ENDED
    assert fake.opens == 1  # never reconnected


async def test_an_open_turn_is_settled_when_its_connection_dies() -> None:
    fake = FakeStreaming()
    sink = _Collector()
    stream = _stream(fake, sink, interim_interval_s=0.0)
    task = asyncio.create_task(stream.run())
    await _feed(stream, 2, start=500.0)

    fake.emit(TurnPartial(text="mid sentence", ref="t", append=True))
    await _settle()
    fake.end()
    stream.feed(_FRAME, 500.06, is_speech=True)
    await _until(lambda: fake.opens == 2)
    stream.stop()
    await asyncio.wait_for(task, 2)

    settled = sink.finals()
    assert [e.text for e in settled] == ["mid sentence"]
    assert settled[0].turn_id == "p1:1:t"  # replaces the interim it had published


async def test_the_glossary_is_re_applied_on_every_connection() -> None:
    fake = FakeStreaming()
    sink = _Collector()
    glossary = Glossary(campaign_id="c", terms=["Drakonia"])
    stream = TranscriptStream(
        fake,
        publish=sink,
        capture_rate=16000,
        config=StreamConfig(
            session_id="s1", glossary=glossary, reconnect_backoff_s=0.0, final_wait_s=0.05
        ),
    )
    task = asyncio.create_task(stream.run())
    await _feed(stream, 2, start=1.0)
    fake.end()
    stream.feed(_FRAME, 1.04, is_speech=True)
    await _until(lambda: fake.opens == 2)
    stream.stop()
    await asyncio.wait_for(task, 2)

    assert fake.glossary is glossary


# --- the session path around it -------------------------------------------


def _path(
    primary: object,
    *,
    bus: _Bus | None = None,
    config: StreamConfig | None = None,
    **kwargs: object,
) -> StreamPath:
    return StreamPath(
        primary,  # pyright: ignore[reportArgumentType]
        bus or EventBus(),  # pyright: ignore[reportArgumentType]
        session_id="s1",
        capture_rate=16000,
        config=config or StreamConfig(reconnect_backoff_s=0.0, max_reconnects=0),
        **kwargs,  # pyright: ignore[reportArgumentType]
    )


def _diarizing_path(primary: object, diarizer: FakeDiarizer, bus: _Bus) -> StreamPath:
    """A path in remote diarization mode, which is the only mode that ships audio."""
    return _path(
        primary,
        bus=bus,
        diarization=DiarizationConfig(mode=DiarizationMode.REMOTE, endpoint="http://d"),
        diarizer=diarizer,
    )


async def _feed_path(path: StreamPath, count: int, start: float) -> None:
    """Frames into the path the way the capture loop delivers them."""
    ts = start
    for _ in range(count):
        path.frame(_FRAME, ts, is_speech=True)
        ts += 0.02
        await _settle(3)


async def test_a_dead_stream_fails_over_to_a_streaming_fallback() -> None:
    primary = FakeStreaming(_provider("p1"), open_fails=99)
    fallback = FakeStreaming(_provider("p2"))
    path = _path(primary, fallback=fallback)
    task = asyncio.create_task(path.run())
    await _until(lambda: fallback.opens == 1)

    path.frame(_FRAME, 1.0, is_speech=True)
    await _settle()
    path.done()
    assert await asyncio.wait_for(task, 2) == PathEnd.ENDED
    assert fallback.sent  # the fallback's own stream carried the rest


async def test_a_dead_stream_hands_a_call_shaped_fallback_the_session() -> None:
    primary = FakeStreaming(_provider("p1"), open_fails=99)
    fallback = CallShaped()
    path = _path(primary, fallback=fallback)

    assert await asyncio.wait_for(path.run(), 2) == PathEnd.HANDOFF
    assert path.handoff == (fallback, None)  # the dead primary is not behind it
    assert not path.queues_utterances  # nothing drains that queue until told to
    path.hand_off()
    assert path.queues_utterances


async def test_a_model_the_vendor_will_not_stream_keeps_the_session_it_configured() -> None:
    """OpenAI's two realtime-routed models refuse server VAD outright.

    The provider is not broken and its fallback is not needed: the session runs
    exactly as it did before streaming existed, one utterance at a time,
    through the very connector that just said no.
    """
    primary = FakeStreaming(_provider("p1"), unsupported=True)
    fallback = CallShaped()
    path = _path(primary, fallback=fallback)

    assert await asyncio.wait_for(path.run(), 2) == PathEnd.HANDOFF
    assert path.handoff == (primary, fallback)
    assert path.terminal_error is None  # nothing failed, so nothing to report
    assert primary.opens == 1  # a refusal is not retried


async def test_a_streaming_only_connector_that_refuses_has_nowhere_to_hand_off() -> None:
    """No second shape to fall back to, so the provider is simply out."""
    path = _path(StreamingOnly())

    assert await asyncio.wait_for(path.run(), 2) == PathEnd.EXHAUSTED
    assert path.terminal_error is not None


async def test_no_provider_left_is_the_same_exhaustion_as_the_utterance_path() -> None:
    primary = FakeStreaming(_provider("p1"), open_fails=99)
    path = _path(primary)

    assert await asyncio.wait_for(path.run(), 2) == PathEnd.EXHAUSTED
    assert path.terminal_error is not None
    assert "P1" in path.terminal_error


async def test_utterances_only_reach_the_queue_after_a_handoff() -> None:
    queued: list[Utterance] = []
    primary = FakeStreaming(_provider("p1"), open_fails=99)
    path = _path(primary, fallback=CallShaped(), queue_utterance=queued.append)

    assert await asyncio.wait_for(path.run(), 2) == PathEnd.HANDOFF
    path.hand_off()
    path.utterance(Utterance(pcm=b"", start=1.0, end=2.0))
    assert len(queued) == 1


# --- the turn key, and the turns it must keep straight ---------------------


async def test_a_final_that_moves_a_turn_start_does_not_move_its_key() -> None:
    """A vendor may revise where a turn began; the row it is written to may not.

    x.ai states a turn's span only when it closes it, and Deepgram's own docs
    warn that endpointing refines a start. Deriving the key from the emitted
    start a second time writes the final as a new row beside the interims it
    was supposed to replace, which the end of the session then deletes.
    """
    fake = FakeStreaming()
    sink = _Collector()
    stream = _stream(fake, sink, interim_interval_s=0.0)
    task = asyncio.create_task(stream.run())
    await _feed(stream, 3, start=10.0)

    fake.emit(TurnStarted(at=0.0))  # no ref: the key can only come from a time
    fake.emit(TurnPartial(text="the goblin", append=True))
    await _settle()
    fake.emit(TurnFinal(text="the goblin snarls", at=0.04))  # a revised start
    await _settle()
    stream.stop()
    await asyncio.wait_for(task, 2)

    (interim,) = sink.interims()
    (final,) = sink.finals()
    assert final.start_ts == _at(10.04)  # the revision moved the timestamps
    assert final.turn_id == interim.turn_id  # ...and nothing else
    assert final.turn_id == "p1:1:t10000"  # the start it was opened with


async def test_a_partial_naming_a_settled_turn_is_dropped() -> None:
    """A late partial must not reopen a turn that already has its final.

    Reopening writes an interim under the settled turn's key, which the upsert
    puts over the final; ``delete_interims`` then removes it at the end of the
    session, and the turn's text is gone from a transcript that had it.
    """
    fake = FakeStreaming()
    sink = _Collector()
    stream = _stream(fake, sink, interim_interval_s=0.0)
    task = asyncio.create_task(stream.run())
    await _feed(stream, 2, start=20.0)

    fake.emit(TurnPartial(text="who goes", ref="t", append=True))
    fake.emit(TurnFinal(text="who goes there", ref="t"))
    await _settle()
    fake.emit(TurnPartial(text="who goes there ha", ref="t", append=False))
    await _settle()
    stream.stop()
    await asyncio.wait_for(task, 2)

    assert [e.text for e in sink.finals()] == ["who goes there"]
    assert [e.text for e in sink.interims()] == ["who goes"]  # the late one, never
    assert sink.events[-1].is_final  # the turn's last word on itself is settled


async def test_an_empty_final_settles_the_interim_it_would_have_stranded() -> None:
    """A vendor coming back empty for a turn it was sending interims for.

    Dropping the final settles nothing: the interim row stays dimmed until the
    end of the session deletes it, taking the only text there ever was. An
    empty final is a routine frame, not a statement that nothing was said.
    """
    fake = FakeStreaming()
    sink = _Collector()
    stream = _stream(fake, sink, interim_interval_s=0.0)
    task = asyncio.create_task(stream.run())
    await _feed(stream, 2, start=30.0)

    fake.emit(TurnPartial(text="a fine morning", ref="t", append=True))
    await _settle()
    fake.emit(TurnFinal(text="", ref="t"))
    await _settle()
    stream.stop()
    await asyncio.wait_for(task, 2)

    (interim,) = sink.interims()
    (final,) = sink.finals()
    assert final.text == "a fine morning"
    assert final.turn_id == interim.turn_id  # it replaces the row it stranded


async def test_a_turn_that_published_nothing_and_ends_empty_emits_nothing() -> None:
    """A vendor that heard silence is still not a segment."""
    fake = FakeStreaming()
    sink = _Collector()
    stream = _stream(fake, sink, interim_interval_s=0.0)
    task = asyncio.create_task(stream.run())
    await _feed(stream, 2, start=40.0)

    fake.emit(TurnStarted(at=0.0, ref="t"))
    fake.emit(TurnFinal(text="", ref="t"))
    await _settle()
    stream.stop()
    await asyncio.wait_for(task, 2)

    assert sink.events == []


async def test_open_turns_are_bounded_within_one_connection() -> None:
    """A vendor that opens turns and never ends them cannot grow the books.

    The oldest is settled rather than dropped, for the same reason a dying
    connection settles what it had open: a forgotten turn leaves a dimmed row
    that nothing will ever replace.
    """
    fake = FakeStreaming()
    sink = _Collector()
    stream = _stream(fake, sink, interim_interval_s=0.0)
    task = asyncio.create_task(stream.run())
    await _feed(stream, 2, start=50.0)

    for i in range(33):  # one past _MAX_OPEN_TURNS
        fake.emit(TurnPartial(text=f"turn {i}", ref=f"t{i}", append=True))
    await _until(lambda: len(sink.finals()) == 1)

    assert [e.text for e in sink.finals()] == ["turn 0"]
    stream.stop()
    await asyncio.wait_for(task, 2)


async def test_stated_ends_are_bounded_within_one_connection() -> None:
    """An end for a turn that never arrives is remembered, but not forever."""
    fake = FakeStreaming()
    sink = _Collector()
    stream = _stream(fake, sink)
    task = asyncio.create_task(stream.run())
    await _feed(stream, 3, start=60.0)  # the last frame written is 60.04

    fake.emit(TurnEnded(at=0.5, ref="orphan"))
    for i in range(64):  # _MAX_PENDING_ENDS more, which pushes the orphan out
        fake.emit(TurnEnded(at=1.0, ref=f"e{i}"))
    await _settle(200)
    fake.emit(TurnFinal(text="late", ref="orphan"))
    await _until(lambda: len(sink.finals()) == 1)
    stream.stop()
    await asyncio.wait_for(task, 2)

    # Forgotten with the oldest entries, so the final falls back to the last
    # frame written rather than to the 60.5 the vendor once stated.
    assert round(sink.finals()[0].end_ts, 6) == _at(60.04)


# --- liveness --------------------------------------------------------------


async def test_a_vendor_message_that_carries_no_turn_still_proves_it_is_alive() -> None:
    """Four connectors promise this: a consumed ack keeps the socket alive.

    Deepgram's ``Metadata``, AssemblyAI's ``Begin`` and ``Termination``, x.ai's
    ``transcript.created``. Consuming one silently tells the watchdog nothing,
    and a slow but live connection is then killed as soon as the local VAD goes
    quiet - which is the same setup as
    ``test_the_watchdog_fires_once_a_turn_has_ended_with_no_answer``, where it
    should be.
    """
    fake = FakeStreaming()
    sink = _Collector()
    stream = _stream(fake, sink, watchdog_s=0.01, quiet_grace_s=0.02, max_reconnects=0)
    task = asyncio.create_task(stream.run())

    ts = 300.0
    for i in range(40):
        fake.emit(StreamAlive())  # the vendor is thinking, and says so
        stream.feed(_FRAME, ts, is_speech=i < 20)
        ts += 0.02
        await asyncio.sleep(0.003)
    stream.stop()
    assert await asyncio.wait_for(task, 2) == StreamOutcome.ENDED
    assert fake.opens == 1  # never declared dead, never reconnected
    assert sink.events == []  # and it is not a segment either


# --- endings that must not escape or hang ----------------------------------


async def test_a_publish_that_raises_while_settling_does_not_escape_run() -> None:
    """``run`` promises a return value for every provider failure.

    An exception out of the teardown escapes it, and the session manager then
    skips its failover and handoff handling entirely: the recording carries on
    with nothing transcribing it, healthz stays green, and Stop turns the
    session into an ERROR hours later.
    """

    class Breaking(_Collector):
        async def __call__(self, event: TranscriptEvent) -> None:
            if event.is_final:
                msg = "a subscriber with a bug"
                raise RuntimeError(msg)
            await super().__call__(event)

    fake = FakeStreaming()
    sink = Breaking()
    stream = _stream(fake, sink, interim_interval_s=0.0)
    task = asyncio.create_task(stream.run())
    await _feed(stream, 2, start=70.0)

    fake.emit(TurnPartial(text="mid sentence", ref="t", append=True))
    await _settle()
    stream.stop()
    assert await asyncio.wait_for(task, 2) == StreamOutcome.ENDED


async def test_a_connection_that_will_not_close_does_not_hold_the_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Teardown after a cancelled drain is bounded, and bounded per step.

    ``_finish`` cancels the live task when its 30s drain runs out, and awaiting
    the cancelled reader consumes that cancellation, so nothing else stops a
    vendor call here from taking as long as its own client allows.
    """

    class Hanging(FakeStreaming):
        async def close_stream(self) -> None:
            self.closes += 1
            await asyncio.Event().wait()  # a socket that never closes

    monkeypatch.setattr(stt_streaming, "_TEARDOWN_TIMEOUT_S", 0.05)
    fake = Hanging()
    sink = _Collector()
    stream = _stream(fake, sink, interim_interval_s=0.0)
    task = asyncio.create_task(stream.run())
    await _feed(stream, 2, start=80.0)

    fake.emit(TurnPartial(text="half a sen", ref="t", append=True))
    await _settle()
    stream.stop()
    assert await asyncio.wait_for(task, 2) == StreamOutcome.ENDED
    # The hung close cost its own deadline and nothing else: the open turn was
    # still settled behind it.
    assert [e.text for e in sink.finals()] == ["half a sen"]


async def test_a_stop_while_a_provider_is_failing_to_connect_is_not_exhaustion() -> None:
    """Stop during a reconnect is a normal shutdown, not a dead provider.

    Checking ``_stopped`` only after a connection served spends the whole
    reconnect budget and its backoff on audio that is not coming, and ends a
    single-provider session at ``EXHAUSTED`` - which fires the ERROR-level
    "Transcription stopped" alert on the way out of an evening that went fine.
    """
    primary = FakeStreaming(_provider("p1"), open_fails=99)
    path = _path(primary, config=StreamConfig(reconnect_backoff_s=0.0, max_reconnects=99))
    task = asyncio.create_task(path.run())
    await _until(lambda: primary.opens >= 2)

    path.done()
    assert await asyncio.wait_for(task, 2) == PathEnd.ENDED
    assert path.terminal_error is None


async def test_a_stream_that_breaks_its_contract_is_treated_as_a_dead_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The path's own promise not to raise, made structural rather than assumed."""

    async def boom(self: TranscriptStream) -> str:
        _ = self
        msg = "a bug below the contract"
        raise RuntimeError(msg)

    monkeypatch.setattr(TranscriptStream, "run", boom)
    path = _path(FakeStreaming(_provider("p1")), fallback=CallShaped())

    assert await asyncio.wait_for(path.run(), 2) == PathEnd.HANDOFF


# --- gap markers across a whole path ---------------------------------------


async def test_the_gap_a_dead_provider_left_is_closed_by_its_successor() -> None:
    """A gap belongs to the span, not to the stream object that opened it.

    Held per ``TranscriptStream``, and one is built per provider, the span
    between a dead primary and its successor's first frame was marked by
    nobody: the very failover the marker exists for.
    """
    primary = FakeStreaming(_provider("p1"))
    fallback = FakeStreaming(_provider("p2"))
    bus = _Bus()
    path = _path(primary, bus=bus, fallback=fallback)
    task = asyncio.create_task(path.run())
    await _until(lambda: primary.opens == 1)
    await _feed_path(path, 3, start=200.0)  # 200.00, 200.02, 200.04

    primary.end()  # the vendor hangs up, with no budget to reconnect
    path.frame(_FRAME, 200.06, is_speech=True)  # the frame that finds it dead
    await _until(lambda: fallback.opens == 1)
    await _feed_path(path, 2, start=205.0)
    path.done()
    assert await asyncio.wait_for(task, 2) == PathEnd.ENDED

    (gap,) = bus.gaps()
    assert round(gap.start_ts, 6) == _at(200.04)  # the last frame P1 transcribed
    assert round(gap.end_ts, 6) == _at(205.0)  # the first frame P2 did
    assert "P1" in gap.text  # the provider that dropped, not the one that took over


async def test_the_last_provider_giving_up_marks_the_span_it_left() -> None:
    """``_publish_gap(self._last_ts)`` on exhaustion was a no-op by construction.

    The pending start had just been set to that same ``_last_ts``, so the
    marker spanned nothing. The capture is the only thing still running by
    then, so it is where the span ends.
    """
    primary = FakeStreaming(_provider("p1"))
    bus = _Bus()
    path = _path(primary, bus=bus)
    task = asyncio.create_task(path.run())
    await _until(lambda: primary.opens == 1)
    await _feed_path(path, 3, start=300.0)

    primary.end()
    path.frame(_FRAME, 300.06, is_speech=True)  # finds it dead
    path.frame(_FRAME, 305.0, is_speech=True)  # and the recording goes on
    assert await asyncio.wait_for(task, 2) == PathEnd.EXHAUSTED

    (gap,) = bus.gaps()
    assert round(gap.start_ts, 6) == _at(300.04)
    assert round(gap.end_ts, 6) == _at(305.0)  # where the capture had got to


async def test_a_handoff_marks_the_span_the_utterance_path_will_not_cover() -> None:
    """The utterance path resumes at the next completed utterance, not here."""
    primary = FakeStreaming(_provider("p1"))
    bus = _Bus()
    path = _path(primary, bus=bus, fallback=CallShaped())
    task = asyncio.create_task(path.run())
    await _until(lambda: primary.opens == 1)
    await _feed_path(path, 3, start=400.0)

    primary.end()
    path.frame(_FRAME, 400.06, is_speech=True)
    path.frame(_FRAME, 402.0, is_speech=True)
    assert await asyncio.wait_for(task, 2) == PathEnd.HANDOFF

    (gap,) = bus.gaps()
    assert round(gap.start_ts, 6) == _at(400.04)
    assert round(gap.end_ts, 6) == _at(402.0)


# --- diarization beside the stream rather than in front of it --------------


async def test_a_diarizer_that_fails_costs_the_label_and_not_the_text() -> None:
    """Awaited before the bus, a refusing diarizer took the turn down with it.

    ``RemoteDiarizer.diarize`` raises for any non-2xx, a timeout and a refused
    connection alike. Raised through the connector's reader that killed the
    reader task, and the send loop then declared a healthy socket dead: a lost
    turn, a gap marker blaming the wrong vendor, and a reconnect.
    """
    fake = FakeStreaming()
    bus = _Bus()
    diarizer = FakeDiarizer(fails=True)
    path = _diarizing_path(fake, diarizer, bus)
    task = asyncio.create_task(path.run())
    await _until(lambda: fake.opens == 1)
    await _feed_path(path, 5, start=100.0)

    fake.emit(TurnFinal(text="the goblin speaks", ref="a", at=0.0, to=0.06))
    await _until(lambda: diarizer.calls == 1)
    fake.emit(TurnFinal(text="and the party answers", ref="b", at=0.06, to=0.08))
    await _until(lambda: len(bus.finals()) == 2)
    path.done()
    assert await asyncio.wait_for(task, 2) == PathEnd.ENDED

    assert [e.text for e in bus.finals()] == ["the goblin speaks", "and the party answers"]
    assert [e.speaker for e in bus.finals()] == [None, None]
    assert bus.gaps() == []  # nothing was lost, so nothing is marked lost
    assert fake.opens == 1  # and the socket beside it was never touched


async def test_a_turn_is_published_at_once_and_relabelled_when_the_diarizer_answers() -> None:
    """Two publications of one turn, under one key, so the second replaces the first.

    The first is the text, the moment the vendor settled it. The second is the
    same turn with its speakers, whenever the diarizer gets there. Meanwhile
    the reader keeps reading, which is the half that a diarizer awaited inline
    used to cost: a slow call held the reader past the stream's own watchdog
    and got a healthy connection dropped.
    """
    gate = asyncio.Event()
    diarizer = FakeDiarizer([SpeakerSegment(start=0.0, end=0.2, speaker="Speaker 1")], gate=gate)
    fake = FakeStreaming()
    bus = _Bus()
    path = _diarizing_path(fake, diarizer, bus)
    task = asyncio.create_task(path.run())
    await _until(lambda: fake.opens == 1)
    await _feed_path(path, 5, start=100.0)

    fake.emit(TurnFinal(text="who goes there", ref="a", at=0.0, to=0.04))
    await _until(lambda: diarizer.calls == 1)
    assert [e.text for e in bus.finals()] == ["who goes there"]  # before any answer
    assert bus.finals()[0].speaker is None

    fake.emit(TurnFinal(text="a friend", ref="b", at=0.04, to=0.08))
    await _until(lambda: len(bus.finals()) == 2)  # the reader never stalled on it
    gate.set()
    await _until(lambda: len(bus.finals()) == 4)
    path.done()
    assert await asyncio.wait_for(task, 2) == PathEnd.ENDED

    labelled = bus.finals()[2:]
    assert {e.turn_id for e in labelled} == {"p1:1:a", "p1:1:b"}  # the same rows
    assert [e.speaker for e in labelled] == ["Speaker 1", "Speaker 1"]


async def test_a_diarizer_that_never_answers_does_not_hold_the_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stop waits a bounded moment for the labels in flight, then leaves.

    The bound has to fit inside ``SessionManager._STOP_DRAIN_TIMEOUT_S``, which
    cancels the whole live path when it runs out.
    """
    monkeypatch.setattr(session_streaming, "_DIARIZE_DRAIN_S", 0.05)
    gate = asyncio.Event()  # never set
    diarizer = FakeDiarizer([SpeakerSegment(start=0.0, end=0.2, speaker="X")], gate=gate)
    fake = FakeStreaming()
    bus = _Bus()
    path = _diarizing_path(fake, diarizer, bus)
    task = asyncio.create_task(path.run())
    await _until(lambda: fake.opens == 1)
    await _feed_path(path, 5, start=100.0)

    fake.emit(TurnFinal(text="the goblin speaks", ref="a", at=0.0, to=0.06))
    await _until(lambda: diarizer.calls == 1)
    path.done()
    assert await asyncio.wait_for(task, 2) == PathEnd.ENDED

    assert [e.text for e in bus.finals()] == ["the goblin speaks"]  # published, unlabelled
    assert bus.finals()[0].speaker is None


async def test_labels_are_shifted_by_the_audio_that_was_left_of_a_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A turn whose opening aged out of the window ships a clip that starts later.

    The diarizer's segments come back 0-based against the clip it was sent, so
    shifting them by where the turn began puts every label early by exactly the
    part of the turn that was lost.
    """
    monkeypatch.setattr(session_streaming, "_TURN_AUDIO_WINDOW_S", 0.06)  # three frames
    diarizer = FakeDiarizer([SpeakerSegment(start=0.03, end=0.06, speaker="Speaker 1")])
    fake = FakeStreaming()
    bus = _Bus()
    path = _diarizing_path(fake, diarizer, bus)
    task = asyncio.create_task(path.run())
    await _until(lambda: fake.opens == 1)
    await _feed_path(path, 5, start=100.0)  # only 100.04 onwards is still held

    fake.emit(
        TurnFinal(
            text="a long sentence",
            ref="a",
            at=0.0,
            to=0.1,
            words=(Word(text="sentence", start=0.07, end=0.08),),
        )
    )
    await _until(lambda: len(bus.finals()) == 2)
    path.done()
    assert await asyncio.wait_for(task, 2) == PathEnd.ENDED

    # The segment sits at 100.07-100.10 (the clip starts at 100.04), which is
    # where the word is. Shifted by the turn's own start it would sit at
    # 100.03-100.06, and the word would come back with no speaker at all.
    assert bus.finals()[1].words[0].speaker == "Speaker 1"


async def test_a_socket_that_dies_before_a_single_frame_marks_no_gap() -> None:
    """A connection that wrote nothing has no instant to start a marker at.

    ``_last_ts`` is still zero on a stream that never wrote, and a marker
    running from zero to a capture-clock timestamp claims the whole uptime of
    the machine was lost.
    """
    fake = FakeStreaming()
    sink = _Collector()
    stream = _stream(fake, sink, max_reconnects=1)
    task = asyncio.create_task(stream.run())
    await _until(lambda: fake.opens == 1)

    fake.end()  # the socket is gone before any audio went out
    stream.feed(_FRAME, 500.0, is_speech=True)  # the frame that finds it dead
    await _until(lambda: fake.opens == 2)
    await _feed(stream, 2, start=505.0)
    stream.stop()
    assert await asyncio.wait_for(task, 2) == StreamOutcome.ENDED

    assert [e for e in sink.events if e.source == GAP_SOURCE] == []
