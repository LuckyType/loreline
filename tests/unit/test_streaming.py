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

from loreline.audio.chunker import Utterance
from loreline.bus import EventBus
from loreline.models import GAP_SOURCE, Glossary, ProviderConfig, ProviderKind, TranscriptEvent
from loreline.session.streaming import PathEnd, StreamPath
from loreline.stt.streaming import (
    StreamConfig,
    StreamingConnector,
    StreamOutcome,
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
    dies without ever saying anything.
    """

    def __init__(self, config: ProviderConfig | None = None, *, open_fails: int = 0) -> None:
        self.config = config or _provider()
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
        if self.opens <= self._open_fails:
            msg = "cannot connect"
            raise ConnectionError(msg)

    async def send_audio(self, pcm: bytes) -> None:
        self.sent.append(pcm)

    async def signals(self) -> AsyncIterator[TurnSignal]:
        while True:
            item = await self._inbox.get()
            if item is _END:
                return
            assert isinstance(item, (TurnStarted, TurnPartial, TurnEnded, TurnFinal))
            yield item

    async def flush_input(self) -> None:
        self.flushes += 1

    async def close_stream(self) -> None:
        self.closes += 1

    def emit(self, signal: TurnSignal) -> None:
        self._inbox.put_nowait(signal)

    def end(self) -> None:
        self._inbox.put_nowait(_END)


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


async def test_the_watchdog_fires_on_voiced_audio_with_no_answer() -> None:
    fake = FakeStreaming()
    sink = _Collector()
    stream = _stream(fake, sink, watchdog_s=0.01, max_reconnects=0)
    task = asyncio.create_task(stream.run())

    ts = 300.0
    for _ in range(40):
        stream.feed(_FRAME, ts, is_speech=True)
        ts += 0.02
        await asyncio.sleep(0.002)
    assert await asyncio.wait_for(task, 2) == StreamOutcome.DEAD
    assert fake.opens == 1  # no budget to reconnect with


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


def _path(primary: object, **kwargs: object) -> StreamPath:
    return StreamPath(
        primary,  # pyright: ignore[reportArgumentType]
        EventBus(),
        session_id="s1",
        capture_rate=16000,
        config=StreamConfig(reconnect_backoff_s=0.0, max_reconnects=0),
        **kwargs,  # pyright: ignore[reportArgumentType]
    )


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
    assert path.call_shaped_fallback is fallback
    assert not path.queues_utterances  # nothing drains that queue until told to
    path.hand_off()
    assert path.queues_utterances


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
