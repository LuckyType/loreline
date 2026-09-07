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
"""

from __future__ import annotations

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
from loreline.stt.streaming import StreamConfig, StreamingConnector, StreamOutcome, TranscriptStream

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
        for backend in self._streamable():
            if self._stopped:
                return PathEnd.ENDED
            outcome = await self._stream_with(backend)
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

    async def _stream_with(self, backend: StreamingConnector) -> str:
        """Run one provider's stream to its end, feeding it from now on."""
        stream = TranscriptStream(
            backend,
            publish=self._publish,
            capture_rate=self._capture_rate,
            config=self._config,
        )
        self._stream = stream
        if self._stopped:
            stream.stop()
        try:
            return await stream.run()
        finally:
            self._stream = None

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
        """Diarize a settled turn and put it on the session bus.

        Interims skip diarization: they are replaced within the second, and a
        speaker label on text that is about to change says nothing. Gap markers
        skip it because there is nothing there to label.

        The diarizer is awaited here rather than in a task of its own, so the
        connector's reader stalls for the length of the call. That is the same
        trade the utterance path makes, and it keeps the events on the bus in
        the order the vendor closed them.
        """
        if not event.is_final or event.source == GAP_SOURCE:
            await self._bus.publish(event)
            return
        await self._bus.publish(await self._diarize(event))

    async def _diarize(self, event: TranscriptEvent) -> TranscriptEvent:
        pcm = b""
        if self._audio is not None:
            pcm = self._audio.slice(event.start_ts, event.end_ts)
            if not pcm:
                # The turn ran longer than the window, or closed with an end
                # before its start: nothing to send, so the text ships
                # unlabelled rather than not at all.
                log.warning("stream.diarize.no_audio", session_id=self._session_id)
                return event
        return await merge_diarization(
            event,
            pcm,
            start=event.start_ts,
            sample_rate=self._capture_rate,
            config=self._diarization,
            diarizer=self._diarizer,
            session_id=self._session_id,
        )
