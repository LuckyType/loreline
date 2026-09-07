"""The live path for a session whose primary connector streams.

``SessionManager`` has two live paths now (ADR 0006). The old one cuts the
microphone into ``Utterance``s and drives ``SttRouter``, one call per utterance.
This one hands the frames straight to a connector that decides its own turns,
and it exists as its own object because everything it does differently from the
router is a policy decision the router has nowhere to put:

* **where a frame goes.** :class:`StreamPath` is what the capture loop hands
  every frame to, and it is the only thing that knows whether this session is
  currently streaming or has fallen back to queueing utterances. The capture
  loop keeps its shape either way: stats, the level meter, the continuous WAV,
  the disk watch, Silero, the chunker and the utterance index all run exactly
  as before, because none of them was ever about dispatch.
* **failover without a call to retry.** ``SttRouter`` retries one bounded call
  against the fallback; a stream has no bounded call. Here a dead provider
  means opening a stream on the next one, and where the next one does not
  stream, handing the rest of the session to the utterance path (ADR 0006,
  Decision 3). Both providers dead means what it has always meant: keep
  recording, stop transcribing, tell the GM why.
* **diarization keyed off a vendor turn.** Remote diarization needs the audio a
  closed turn covers, and a turn arrives as two offsets rather than as a clip,
  so the frames are kept in a :class:`RollingPcm` window and sliced when the
  turn closes. Past the slice it is the same call the router makes, through the
  same function, deliberately: see ``loreline.stt.router.merge_diarization``.
  It runs beside the stream rather than in front of it, though, in a task of
  its own: see :meth:`StreamPath._publish`.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import TYPE_CHECKING, Protocol

from loreline.audio.rolling import RollingPcm
from loreline.logging import get_logger
from loreline.models import (
    GAP_SOURCE,
    DiarizationConfig,
    DiarizationMode,
    Glossary,
    TranscriptEvent,
)
from loreline.stt.base import STTBackend
from loreline.stt.router import merge_diarization
from loreline.stt.streaming import (
    PendingGap,
    StreamConfig,
    StreamingConnector,
    StreamOutcome,
    TranscriptStream,
    gap_event,
)

if TYPE_CHECKING:
    from loreline.audio.chunker import Utterance
    from loreline.bus import EventBus
    from loreline.diarization.base import DiarizationProvider
    from loreline.stt.router import SttRouter

log = get_logger(__name__)

# How much capture audio is kept for a turn that has not closed yet. Longer
# than any turn a vendor's endpointing produces (OpenAI's server VAD closes on
# half a second of silence), with room for a turn that ran on and on.
_TURN_AUDIO_WINDOW_S = 90.0

# How long one turn's diarization may take before its text keeps the row it
# already has. Well under the stream's own watchdog (``StreamConfig.
# watchdog_s``, 20s) and far under the diarizer's HTTP client timeout (120s,
# sized for a re-process job rather than for a live turn): a label that lands
# later than this is a label for a row the GM read minutes ago, and the turns
# behind it are still arriving.
_DIARIZE_TIMEOUT_S = 10.0

# How long the end of a session waits for the labels still in flight. Shorter
# than one call's own timeout on purpose: this runs inside the stop drain
# (``SessionManager._STOP_DRAIN_TIMEOUT_S``, 30s), and what is at stake is a
# speaker label on a row that already has its text, at the very end of an
# evening. Holding Stop open for it is the worse trade.
_DIARIZE_DRAIN_S = 5.0


class FrameSink(Protocol):
    """Where the capture loop puts what it produced.

    The seam that lets one capture loop serve both live paths. Frames are what
    a streaming connector wants and utterances are what the router wants, and
    the loop offers both without knowing which is being consumed.
    """

    def frame(self, pcm: bytes, ts: float, *, is_speech: bool) -> None:
        """One captured frame, with the local VAD's verdict on it."""
        ...

    def utterance(self, utterance: Utterance) -> None:
        """One completed utterance, for the path that transcribes those."""
        ...

    @property
    def queues_utterances(self) -> bool:
        """Whether :meth:`utterance` currently has a consumer.

        False while streaming: the vendor decides the turns, so an utterance is
        only the WAV index's business, and queueing it would either transcribe
        the session twice or fill a queue nobody drains.
        """
        ...

    def done(self) -> None:
        """No more of either is coming, however the capture ended."""
        ...


class PathEnd:
    """How :meth:`StreamPath.run` ended, and therefore what happens next.

    ``ENDED``: the microphone stopped, everything settled, the session is over.
    ``HANDOFF``: something call-shaped is left, so the rest of the session goes
    through the utterance path. Two ways to get here, and
    :attr:`StreamPath.handoff` says which: no streaming provider survived and a
    call-shaped fallback is configured, or a provider that streams turned out
    not to stream *this model* and can serve it call-shaped itself.
    ``EXHAUSTED``: nothing is left to transcribe with. Same meaning as
    ``ProvidersExhaustedError`` on the utterance path: keep recording, stop
    transcribing, say why.
    """

    ENDED = "ended"
    HANDOFF = "handoff"
    EXHAUSTED = "exhausted"


class StreamPath:
    """One session's streaming transcription, primary then fallback.

    Implements :class:`FrameSink` for the capture loop and is driven by
    :meth:`run` from the session's live task, the way ``SttRouter.run`` drives
    the other path.
    """

    def __init__(
        self,
        primary: STTBackend,
        bus: EventBus[TranscriptEvent],
        *,
        session_id: str,
        capture_rate: int,
        glossary: Glossary | None = None,
        diarization: DiarizationConfig | None = None,
        diarizer: DiarizationProvider | None = None,
        fallback: STTBackend | None = None,
        on_failover: Callable[[str], Awaitable[None]] | None = None,
        queue_utterance: Callable[[Utterance], None] | None = None,
        config: StreamConfig | None = None,
    ) -> None:
        self._primary = primary
        self._fallback = fallback
        self._bus = bus
        self._session_id = session_id
        self._capture_rate = capture_rate
        self._diarization = diarization or DiarizationConfig()
        self._diarizer = diarizer
        self._on_failover = on_failover
        self._queue_utterance = queue_utterance
        self._config = replace(config or StreamConfig(), session_id=session_id, glossary=glossary)
        self._stream: TranscriptStream | None = None
        self._stopped = False
        self._handed_off = False
        # Diarization runs beside the bus rather than in front of it, so the
        # tasks are held here: asyncio keeps only a weak reference to a task,
        # and a stopping session has to wait for the labels still in flight.
        self._labelling: set[asyncio.Task[None]] = set()
        # A span no provider transcribed, handed from one stream to the next
        # so that whichever one transcribes again is the one that ends it.
        self._gap: PendingGap | None = None
        # Where the capture is now, which is where a gap ends when nothing
        # streams again (a handoff, or nothing left to hand off to).
        self._last_frame_ts = 0.0
        # Only remote diarization needs the audio back; inline reads the words
        # the vendor already attached, and the other modes read nothing.
        self._audio = (
            RollingPcm(capture_rate, seconds=_TURN_AUDIO_WINDOW_S)
            if self._diarization.mode is DiarizationMode.REMOTE and diarizer is not None
            else None
        )
        # Set once the session hands over to the utterance path; from then on
        # this path's health is that router's, which is what the dashboard and
        # /healthz read through the manager.
        self.router: SttRouter | None = None
        self._retired: dict[str, str] = {}
        # A provider that connected and said it cannot stream this model. Not
        # retired: it transcribes perfectly well one utterance at a time, which
        # is what the session falls back to.
        self._call_shaped: STTBackend | None = None

    # -- what the capture loop sees --------------------------------------

    def frame(self, pcm: bytes, ts: float, *, is_speech: bool) -> None:
        self._last_frame_ts = ts
        if self._audio is not None:
            self._audio.append(pcm, ts)
        stream = self._stream
        if stream is not None:
            stream.feed(pcm, ts, is_speech=is_speech)

    def utterance(self, utterance: Utterance) -> None:
        if self._queue_utterance is not None:
            self._queue_utterance(utterance)

    @property
    def queues_utterances(self) -> bool:
        return self._handed_off

    def done(self) -> None:
        """Capture is over: end the stream, whatever state it was in.

        The utterance queue's own closing sentinel stays with the capture loop,
        which delivers it however it ended (see ``_capture_utterances``). This
        is the streaming half of that same guarantee: without it, a stream with
        no more frames coming would sit waiting for one for as long as the
        process lives, which is the zombie recording in a new shape.

        ``_stopped`` is set even when no stream is running, so a provider the
        loop is about to try is stopped before it opens a socket for audio that
        is never coming.
        """
        self._stopped = True
        if self._stream is not None:
            self._stream.stop()

    # -- health, read by the manager --------------------------------------

    @property
    def degraded_since(self) -> float | None:
        return self.router.degraded_since if self.router is not None else None

    @property
    def terminal_error(self) -> str | None:
        """Why this session stopped transcribing for good, or None.

        The vendor's own words where there are any, the same as the router's,
        because it reaches the GM through the same dashboard field.
        """
        if self.router is not None:
            return self.router.terminal_error
        return "; ".join(self._retired.values()) if self._exhausted() else None

    def _exhausted(self) -> bool:
        return bool(self._retired) and not self._streamable() and self._call_shaped is None

    # -- the driving task -------------------------------------------------

    async def run(self) -> str:
        """Stream this session until the input ends or nothing is left.

        Returns a :class:`PathEnd`. Never raises for a provider failure: what
        to do about a dead provider is the point of the return value.
        """
        try:
            return await self._run()
        finally:
            # Both endings, for every way out of the loop: the span nothing
            # transcribed is marked, and the labels still in flight are given
            # a bounded moment to land before the session bus closes.
            await self._close_gap()
            await self._drain_labelling()

    async def _run(self) -> str:
        for backend in self._streamable():
            if self._stopped:
                return PathEnd.ENDED
            outcome = await self._guarded_stream(backend)
            if outcome == StreamOutcome.ENDED:
                return PathEnd.ENDED
            if outcome == StreamOutcome.UNSUPPORTED:
                # Structural, not assumed: a connector may have only the
                # streaming shape, and one of those refusing a model has
                # nowhere to hand the session to, so it is simply out.
                if isinstance(backend, STTBackend):
                    self._call_shaped = self._call_shaped or backend
                    break  # a model this vendor will not stream is not a race to lose
                await self._retire(backend)
                continue
            await self._retire(backend)
        if self.handoff is not None:
            return PathEnd.HANDOFF
        log.error(
            "session.stream.exhausted",
            session_id=self._session_id,
            error=self.terminal_error,
        )
        return PathEnd.EXHAUSTED

    async def _guarded_stream(self, backend: StreamingConnector) -> str:
        """One provider's stream, with :meth:`run`'s promise made structural.

        ``TranscriptStream.run`` classifies its own failures and returns rather
        than raising, and the caller above relies on that: an exception escaping
        here would skip the failover and the handoff, leaving a session that
        keeps recording with nothing transcribing it and healthz green until
        Stop turned it into an ERROR. That is too quiet a failure to leave
        resting on every future edit staying careful, so a provider that
        breaks the contract is treated as the dead provider it is.
        """
        try:
            return await self._stream_with(backend)
        except Exception:
            log.exception(
                "stream.provider.crashed",
                provider_id=backend.config.id,
                session_id=self._session_id,
            )
            return StreamOutcome.DEAD

    async def _stream_with(self, backend: StreamingConnector) -> str:
        """Run one provider's stream to its end, feeding it from now on."""
        stream = TranscriptStream(
            backend,
            publish=self._publish,
            capture_rate=self._capture_rate,
            config=self._config,
            # A span the previous provider stopped transcribing is this one's
            # to close: its first written frame is the moment transcription
            # resumed, which is the only thing that ends a gap.
            gap=self._gap,
        )
        self._stream = stream
        if self._stopped:
            stream.stop()
        try:
            return await stream.run()
        finally:
            self._gap = stream.pending_gap
            self._stream = None

    async def _close_gap(self) -> None:
        """Mark a span that no successor ever closed, at the end of the path.

        Inside one provider's stream a gap is closed by the first frame of its
        next connection. The ones that reach here are the endings that have no
        next connection: every streaming provider retired, a handoff to the
        utterance path (which starts at the next *completed* utterance, not
        where the stream stopped), or a session that ended on a dead socket.
        """
        gap, self._gap = self._gap, None
        event = gap_event(self._session_id, gap, self._last_frame_ts)
        if event is not None:
            await self._bus.publish(event)

    def hand_off(self) -> None:
        """Send the rest of the session's utterances to the router instead.

        Called by the manager once it has a router to consume them, not by
        :meth:`run`: until something is draining that queue, filling it would
        only drop the oldest entry per utterance for the rest of the evening.
        """
        self._handed_off = True

    def _streamable(self) -> list[StreamingConnector]:
        """The providers that can stream and have not been retired, in order."""
        candidates = [self._primary, self._fallback]
        return [
            b
            for b in candidates
            if isinstance(b, StreamingConnector) and b.config.id not in self._retired
        ]

    @property
    def handoff(self) -> tuple[STTBackend, STTBackend | None] | None:
        """The primary and fallback the utterance path should take over with.

        A fallback is any enabled provider row, so a streaming primary with a
        batch fallback is a configuration a GM can already save; that is one
        way here, and the pair is then just the fallback, because the streaming
        primary is dead.

        The other way is a provider that streams as a class but not for the
        model this session picked (``StreamUnsupportedError``). Nothing is
        wrong with it, so the pair is the session the GM configured, unchanged:
        that provider one utterance at a time, with its own fallback behind it.
        Anything else would drop a working provider for a reason the GM never
        chose.
        """
        if self._call_shaped is not None:
            return self._call_shaped, self._fallback
        fallback = self._fallback
        if fallback is not None and not isinstance(fallback, StreamingConnector):
            return fallback, None
        return None

    async def _retire(self, backend: StreamingConnector) -> None:
        """Give up on one provider, and tell the GM it happened.

        Alerted here rather than at the end of :meth:`run` because a session
        that fails over silently looks identical to one that never had a
        problem, which is the complaint ``stt.degraded`` already answers on the
        utterance path.
        """
        reason = f"{backend.config.name}: the live stream failed and could not be re-established"
        self._retired[backend.config.id] = reason
        log.error(
            "stream.provider.retired",
            provider=backend.config.name,
            provider_id=backend.config.id,
            session_id=self._session_id,
        )
        await self.notify_failover(
            f"Live transcription lost its connection to {backend.config.name}. "
            "Audio keeps recording; the session can be re-transcribed later."
        )

    async def notify_failover(self, message: str) -> None:
        """Best-effort push alert, never raising into the path."""
        if self._on_failover is None:
            return
        with contextlib.suppress(Exception):
            await self._on_failover(message)

    # -- events out -------------------------------------------------------

    async def _publish(self, event: TranscriptEvent) -> None:
        """Put a turn on the session bus, and label it afterwards.

        Interims skip diarization: they are replaced within the second, and a
        speaker label on text that is about to change says nothing. Gap markers
        skip it because there is nothing there to label.

        Everything else is published twice, and the order is the whole point.
        The text goes out unlabelled the moment the turn closes, and the
        diarizer runs beside the stream in a task of its own; when it answers,
        the same turn is published again with its speakers, under the same
        ``turn_id``, which the repository upserts on and both live feeds key on,
        so the second publication replaces the first rather than following it.

        Awaiting the diarizer here instead, which is what this did, put a
        remote HTTP call on the connector's reader task: a diarizer that
        answered slowly held the reader past the stream's own watchdog and got
        a healthy socket dropped and a gap marker written, and a diarizer that
        answered with an error - any non-2xx, a timeout, a refused connection -
        raised through the reader, dropped the turn's text entirely, and had the
        send loop declare the vendor dead. The utterance path makes that trade
        because a call there is bounded by one utterance and has nothing else
        waiting; a stream has the rest of the session waiting.
        """
        if not event.is_final or event.source == GAP_SOURCE:
            await self._bus.publish(event)
            return
        if self._audio is None:
            # Inline or off: no network and no clip, so nothing to defer. The
            # speakers are already on the words the vendor sent.
            await self._bus.publish(await self._label_inline(event))
            return
        # Sliced before publishing, not inside the task: the window holds 90
        # seconds, and a turn that waits its turn behind a slow diarizer would
        # be sliced after its audio had aged out of it.
        clip, clip_start = self._audio.slice(event.start_ts, event.end_ts)
        await self._bus.publish(event)
        if not clip:
            # The turn ran longer than the window, or closed with an end before
            # its start: nothing to send, so the text keeps the unlabelled row
            # it already has.
            log.warning("stream.diarize.no_audio", session_id=self._session_id)
            return
        task = asyncio.create_task(self._label(event, clip, clip_start))
        self._labelling.add(task)
        task.add_done_callback(self._labelling.discard)

    async def _label_inline(self, event: TranscriptEvent) -> TranscriptEvent:
        """Speakers from the words the vendor already labelled, or nothing."""
        try:
            return await merge_diarization(
                event,
                b"",
                start=event.start_ts,
                sample_rate=self._capture_rate,
                config=self._diarization,
                diarizer=self._diarizer,
                session_id=self._session_id,
            )
        except Exception as exc:  # resilience: a label is never worth the text
            log.warning("stream.diarize.failed", session_id=self._session_id, error=str(exc))
            return event

    async def _label(self, event: TranscriptEvent, clip: bytes, clip_start: float) -> None:
        """Diarize one closed turn and re-publish it with its speakers.

        Bounded by :data:`_DIARIZE_TIMEOUT_S` and silent about every failure
        past a warning, because the row this would improve is already on
        screen and in the table: there is nothing here worth losing text for.

        ``clip_start`` is where the audio actually starts rather than where the
        turn does. They differ when the turn's opening aged out of the rolling
        window, and shifting the diarizer's 0-based segments by the wrong one
        of the two puts every label seconds early.
        """
        try:
            async with asyncio.timeout(_DIARIZE_TIMEOUT_S):
                labelled = await merge_diarization(
                    event,
                    clip,
                    start=clip_start,
                    sample_rate=self._capture_rate,
                    config=self._diarization,
                    diarizer=self._diarizer,
                    session_id=self._session_id,
                )
        except TimeoutError:
            log.warning(
                "stream.diarize.timeout", session_id=self._session_id, seconds=_DIARIZE_TIMEOUT_S
            )
            return
        except Exception as exc:  # resilience: any diarizer error, same answer
            log.warning("stream.diarize.failed", session_id=self._session_id, error=str(exc))
            return
        if labelled == event:
            return  # nothing to say that the row already on screen does not
        await self._bus.publish(labelled)

    async def _drain_labelling(self) -> None:
        """Give the labels still in flight a bounded moment, then drop them.

        Bounded because this runs inside the session's stop drain, and dropped
        rather than awaited because a label that lands after the session bus
        closes reaches nobody anyway; the text it would have improved is
        already stored.
        """
        tasks, self._labelling = list(self._labelling), set()
        if not tasks:
            return
        try:
            async with asyncio.timeout(_DIARIZE_DRAIN_S):
                await asyncio.gather(*tasks, return_exceptions=True)
        except TimeoutError:
            log.warning(
                "stream.diarize.drain_timeout", session_id=self._session_id, pending=len(tasks)
            )
        finally:
            for task in tasks:
                task.cancel()
