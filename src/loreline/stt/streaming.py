"""The second connector shape: a session's audio in, the vendor's turns out.

``stt/base.py`` is the shape for audio somebody else has already cut up: one
``Utterance`` in, one final ``TranscriptEvent`` out. This module is the shape
for audio nobody has cut up. A streaming connector is handed raw PCM frames for
the life of a session, decides its own turns by its vendor's protocol, and
yields ``TranscriptEvent``s as those turns take shape: interims while a turn is
open (``is_final=False``) and one final per closed turn. See
``docs/adr/0006-streaming-realtime-transcription.md``.

Two things live here, and the split between them is the point:

``StreamingConnector`` is the vendor half, and it is meant to be *only* protocol
translation. A connector opens a socket, writes PCM to it, and turns each
message the vendor sends into one of four turn signals (:class:`TurnStarted`,
:class:`TurnPartial`, :class:`TurnEnded`, :class:`TurnFinal`) or into
:class:`StreamAlive`, which says only that the socket is still there. It keeps
no timing state, no turn bookkeeping and no reconnect logic, because the half
below already has all three, and five connectors reimplementing them is five
chances to get the clock wrong.

``TranscriptStream`` is the session half, one per provider per session, and it
owns everything that is the same whichever vendor answered:

* **the clock.** A vendor counts in milliseconds since the first byte of the
  current connection; the app counts in ``time.monotonic()`` values from the
  capture device. ``t0`` is the capture timestamp of the first frame written to
  the current connection, so a vendor offset becomes ``t0 + offset`` and the
  timeline dots, the click-to-jump and the karaoke highlight keep working
  (they all key off ``TranscriptEvent.start_ts``). A signal that carries no
  offset falls back to the capture timestamp of the last frame written, which
  is what a vendor reporting no timing at all leaves to work with.
* **interim throttling.** A vendor emits a delta per word; the browser needs a
  few updates a second, and every interim published is a row written and a
  frame pushed to every socket.
* **the resampler.** ``Pcm16Stream`` (soxr, stateful) where the vendor's rate
  differs from the capture rate, one per connection, because a resampler's
  filter state belongs to the stream it was filtering.
* **liveness and reconnect.** A watchdog that only counts *voiced* audio (see
  :meth:`TranscriptStream.feed`), a bounded number of reconnects against the
  same provider, and a gap marker for the span a dead connection swallowed.

The two are separate objects rather than one base class with hooks because a
connector outlives no connection and a stream outlives several: reconnecting is
the stream throwing its connector's socket away and asking for another one.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, replace

from loreline.audio.resample import Pcm16Stream
from loreline.logging import get_logger
from loreline.models import GAP_SOURCE, Glossary, ProviderConfig, TranscriptEvent, Word

log = get_logger(__name__)

__all__ = [
    "PendingGap",
    "StreamAlive",
    "StreamConfig",
    "StreamOutcome",
    "StreamSignal",
    "StreamingConnector",
    "TranscriptStream",
    "TurnEnded",
    "TurnFinal",
    "TurnPartial",
    "TurnSignal",
    "TurnStarted",
    "gap_event",
    "is_streaming",
]


class StreamUnsupportedError(RuntimeError):
    """This connector cannot stream *this model*, and it never will.

    Not a connection failure, so nothing about it is worth retrying: the stream
    stops at once rather than spending its reconnect budget on an answer that
    cannot change. The caller's move is to run the same provider through the
    utterance path, which is what it did before streaming existed and which
    still works.

    Real and not hypothetical: OpenAI's ``gpt-live-transcribe`` and
    ``gpt-realtime-whisper``, the two models capabilities.yaml routes to the
    realtime connector, both answer a server-VAD ``session.update`` with
    "Turn detection is not supported for this transcription model", while
    ``gpt-4o-transcribe`` and ``whisper-1``, which it routes to batch, accept
    it. Whether a model can be streamed is a fact about the model, and until
    the yaml carries it the vendor is the one who says so, on connect.
    """


# --------------------------------------------------------------------------
# The signals a connector translates its vendor's messages into.
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TurnStarted:
    """The vendor decided a turn began.

    ``at`` is seconds into the audio written to *this connection*, as the
    vendor counts it (OpenAI's ``audio_start_ms / 1000``). None where the
    vendor names no offset, which the stream reads as "now".

    ``ref`` is the vendor's own handle for the turn, forwarded verbatim so
    later signals can be matched to it (OpenAI's ``item_id``, AssemblyAI's
    ``turn_order``). Leave it empty for a vendor that names none: the stream
    then treats every signal as belonging to the one open turn, which is what a
    protocol without ids means.
    """

    at: float | None = None
    ref: str = ""


@dataclass(frozen=True, slots=True)
class TurnPartial:
    """More text for an open turn, published as an interim.

    ``append`` says how to read ``text``: True for a vendor that sends
    increments (OpenAI's transcription deltas, one per word or so), False for a
    vendor that resends the whole interim every time (Deepgram's non-final
    ``Results``). Getting it wrong is visible as either duplicated or truncated
    interim text, so it is stated per signal rather than assumed per connector.

    A partial whose ``ref`` names no open turn opens one, starting at the last
    frame written. That is the fallback for a vendor that announces no turn
    start of its own.
    """

    text: str
    ref: str = ""
    append: bool = False


@dataclass(frozen=True, slots=True)
class TurnEnded:
    """The vendor closed a turn's *audio*; its text may still be coming.

    Separate from :class:`TurnFinal` because several vendors say "the speaker
    stopped" and "here is what they said" in two messages, and only the first
    carries the offset the event's ``end_ts`` needs (OpenAI's ``speech_stopped``
    has ``audio_end_ms``, its ``completed`` has no timing at all). Sending both
    is a connector forwarding what it was told; the stream remembers the offset
    until the text arrives.
    """

    at: float | None = None
    ref: str = ""


@dataclass(frozen=True, slots=True)
class TurnFinal:
    """A turn's settled text: the stream publishes it and closes the turn.

    ``words`` is empty for a vendor that returns none, which leaves the event
    exactly as ``Transcription`` does on the utterance path. Word timings, when
    a vendor sends them, are offsets into this connection's audio like ``at``
    and ``to``, not capture-clock values: the stream shifts them.

    ``at`` and ``to`` override whatever :class:`TurnStarted` and
    :class:`TurnEnded` established, for a vendor that only states a turn's span
    once it closes it.
    """

    text: str
    ref: str = ""
    words: tuple[Word, ...] = ()
    at: float | None = None
    to: float | None = None


@dataclass(frozen=True, slots=True)
class StreamAlive:
    """This connection is alive and had nothing to say about a turn.

    Yield this for any vendor message that proves the socket is alive but
    carries no turn: Deepgram's ``Metadata``, AssemblyAI's ``Begin``,
    ``SpeechStarted`` and ``Termination``, x.ai's ``transcript.created``. The
    stream refreshes its liveness watchdog on it and does nothing else.

    It exists because the watchdog counts messages *the stream* was told about,
    not messages the socket carried. A connector that consumes an ack silently
    leaves the stream unable to tell a slow connection from a dead one, and
    kills a live session ``watchdog_s`` after the local VAD goes quiet.
    """


# What a signal about a turn is, and what a connector may yield. The two are
# separate because :class:`StreamAlive` is deliberately not a turn signal: a
# connector that never has anything to say beyond turns keeps returning
# ``AsyncIterator[TurnSignal]`` and stays correct, while the contract below
# accepts either.
TurnSignal = TurnStarted | TurnPartial | TurnEnded | TurnFinal
StreamSignal = TurnSignal | StreamAlive


# --------------------------------------------------------------------------
# The vendor half.
# --------------------------------------------------------------------------


class StreamingConnector(ABC):
    """One vendor's streaming protocol, and nothing else.

    A connector may implement this *and* ``Connector`` from ``stt/base.py``:
    the utterance shape still serves re-processing (which replays a stored file
    and asks for ``prefer_batch``) and the call-shaped fallback path, while
    this shape serves a live capture. ``OpenAIRealtimeBackend`` does exactly
    that.

    The hooks are called by :class:`TranscriptStream` in this order, and only
    from its own tasks, so an implementation needs no locking of its own:

    1. :meth:`open_stream` once per connection,
    2. :meth:`send_audio` per frame, with :meth:`signals` iterating alongside,
    3. :meth:`flush_input` once, when the microphone stops,
    4. :meth:`close_stream` once per connection, always, failures included.

    What an implementation must *not* do: reconnect, retry, resample, throttle,
    hold audio back, or time anything against the capture clock. The stream
    above it does all of that, and a connector that also does it fights it.
    """

    config: ProviderConfig

    @property
    @abstractmethod
    def stream_rate(self) -> int:
        """The sample rate this vendor wants, in Hz.

        The stream resamples the capture feed to it (statefully, with soxr)
        when it differs from the capture rate, so a connector always receives
        PCM at exactly this rate and never resamples anything itself.
        """

    @abstractmethod
    async def open_stream(self, glossary: Glossary | None) -> None:
        """Connect and configure a session, ready for audio.

        Called again after every disconnect, so anything cached from a previous
        connection must be dropped or deliberately kept (a "this model rejects
        the prompt parameter" flag is worth keeping; a socket is not). Raising
        counts as one failed attempt against the stream's reconnect budget.

        Raise :class:`StreamUnsupportedError` instead where the vendor says it
        will not stream *this model*, which is an answer no retry can change:
        the stream stops at once and the session runs this same provider on the
        utterance path. Check for it here rather than declaring it, because the
        vendor is where the answer lives; OpenAI's is a rejected
        ``turn_detection`` in its reply to ``session.update``.
        """

    @abstractmethod
    async def send_audio(self, pcm: bytes) -> None:
        """Write one block of mono s16le PCM at :attr:`stream_rate`.

        Block sizes are not fixed: a stateful resampler holds a block's tail
        back until the next one arrives, so some calls carry a little more than
        a frame and some a little less. Never buffer here to even them out;
        that only adds latency to the thing the whole feature is about.
        """

    @abstractmethod
    def signals(self) -> AsyncIterator[StreamSignal]:
        """Translate the vendor's messages into signals, until the socket ends.

        One message in, zero or more signals out, with no state kept between
        them beyond what the protocol itself forces. Returning (the socket
        closed) and raising both mean "this connection is over"; the stream
        decides whether that is a reconnect, a failover or the end of it.

        Anything the vendor sends that is not a turn signal (acks, session
        confirmations, keepalives) should still be consumed here *and yielded
        as* :class:`StreamAlive`. The liveness watchdog counts messages it was
        told about, and a connection that only acks is a connection that is
        still alive - but consuming an ack silently says nothing, and the
        watchdog then kills the socket once the local VAD goes quiet.
        """

    @abstractmethod
    async def flush_input(self) -> None:
        """Tell the vendor no more audio is coming for the open turn.

        Called once, when the microphone stops, before the stream waits a
        bounded time for the last finals. For OpenAI this is a final
        ``input_audio_buffer.commit``; for a vendor whose only flush signal
        also ends the session, send it here, since the session is ending
        anyway. A vendor that needs no flush leaves this empty.
        """

    @abstractmethod
    async def close_stream(self) -> None:
        """Drop the connection. Called exactly once per connection, always."""


def is_streaming(backend: object) -> bool:
    """Whether this backend can be driven by :class:`TranscriptStream`.

    The one question the session manager asks to choose between the two live
    paths. A connector's shape is a fact about its class, not a field in
    capabilities.yaml (ADR 0006, Decision 2), so this is an isinstance check
    and deliberately not a lookup.
    """
    return isinstance(backend, StreamingConnector)


# --------------------------------------------------------------------------
# The session half.
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StreamConfig:
    """What a live stream is allowed to do before it gives up.

    The defaults are chosen against the shape of a table session, not against a
    benchmark: people are silent for minutes at a time, so nothing here may
    read silence as failure, and a reconnect costs a visible gap marker, so
    nothing here may reconnect over a hiccup.
    """

    session_id: str = ""
    glossary: Glossary | None = None
    # Frames held for a connection that is not draining. 250 * 20 ms = 5s: deep
    # enough that a normal scheduling hiccup never reaches it, shallow enough
    # that filling it means the socket is gone rather than busy.
    queue_frames: int = 250
    # Longest one write may take before the socket counts as dead. Same order
    # as the queue depth above, for the same reason.
    send_timeout_s: float = 5.0
    # Voiced audio written with nothing at all coming back for this long is a
    # dead connection (ADR 0006, Decision 4). Counted from voiced frames only:
    # with server VAD a silent room produces no vendor messages by design, so a
    # plain "nothing received" timer would fire on every coffee break.
    watchdog_s: float = 20.0
    # ...and not while somebody is still talking. Measured against OpenAI, a
    # transcription session says nothing at all for the whole length of a turn
    # and then everything at once when it closes, so a watchdog that only
    # counted voiced audio killed a twelve second turn mid-sentence and cost
    # nineteen seconds of speech to the gap that followed. Waiting for the
    # local VAD to go quiet first is what makes "nothing came back" mean
    # something: every vendor emits at a turn boundary, so silence past one is
    # a dead connection and silence inside a turn is just a long sentence.
    #
    # Invariant: this must be longer than the vendor's own endpointing silence.
    # The vendor closes a turn, and says so, after N seconds of quiet; the
    # watchdog starts counting a pause as suspicious after this long. With this
    # the shorter of the two, an ordinary pause beginning more than watchdog_s
    # after the vendor's last message would be read as a dead socket and would
    # kill a live turn. All five vendors sit comfortably under 2s today: OpenAI
    # server VAD 500ms, AssemblyAI its default, Deepgram endpointing 500ms,
    # x.ai 400ms, Gemini 300ms. A vendor configured slower than this needs this
    # raised with it.
    quiet_grace_s: float = 2.0
    # Consecutive failed attempts against the same provider before the caller
    # is told to fail over. Each one costs a gap marker, so it is small.
    max_reconnects: int = 3
    reconnect_backoff_s: float = 1.0
    # A connection that stayed up this long earns a fresh reconnect budget: a
    # socket that served an hour and then dropped is a network blip, while one
    # that dies every few seconds is a provider to leave. Without this, an
    # evening's normal reconnects would spend the budget and fail over for no
    # reason.
    healthy_after_s: float = 30.0
    # Frames kept from before a connection was ready, about a second at the
    # 20 ms frames capture produces. Opening a socket takes a moment and the
    # microphone does not wait, so dropping everything would clip the first
    # words of every session; keeping everything would replay an outage's whole
    # backlog into a fresh stream, where the vendor times it as if it had just
    # been spoken (and, for at least one vendor, desynchronizes its turn
    # machinery outright). The tail is the part that is still nearly true.
    resume_keep_frames: int = 50
    # At most one interim per turn per this long. The eye wants a few updates a
    # second; every one of them is a row written and a frame to every socket.
    interim_interval_s: float = 0.3
    # How long the last finals get after the microphone stops. Whatever is
    # still open when it expires is published as a final anyway, so this bounds
    # how long Stop can take rather than deciding whether text survives.
    final_wait_s: float = 5.0


class StreamOutcome:
    """Why :meth:`TranscriptStream.run` returned.

    ``ENDED``: the microphone stopped and the stream closed cleanly.
    ``DEAD``: the provider is out of reconnects and the caller should move to
    the next one (ADR 0006, Decision 3).
    ``UNSUPPORTED``: this provider works, but not for this model over this
    shape, so the caller should run it through the utterance path instead. See
    :class:`StreamUnsupportedError`.
    """

    ENDED = "ended"
    DEAD = "dead"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True, slots=True)
class PendingGap:
    """A span a dead connection swallowed, waiting for something to end it.

    Held rather than published because a gap is only a gap once something
    *else* transcribes again: the marker runs from where audio stopped being
    transcribed to where it started again, and the second half is not known
    until the next connection writes its first frame, or until the whole path
    gives up (see :meth:`TranscriptStream.pending_gap`).

    ``provider`` is whoever lost the audio, kept because the successor that
    ends the span is usually somebody else, and blaming the vendor that is
    working for the outage of the one that is not helps nobody read a log.
    """

    start: float
    provider: str


def gap_event(session_id: str, gap: PendingGap | None, until: float) -> TranscriptEvent | None:
    """The marker for a span nothing transcribed, or None if there is no span.

    A streaming loss is aligned to nothing: no utterance names it, so no
    re-process recovers exactly it, which is why it is a row in the transcript
    rather than a line in the log (ADR 0006, Decision 3). Built here rather
    than at either call site because both halves need it: the stream closes a
    gap with the first frame of its next connection, and the path above it
    closes the ones no next connection ever comes for.
    """
    if gap is None or until <= gap.start:
        return None
    return TranscriptEvent(
        session_id=session_id,
        source=GAP_SOURCE,
        text=(
            f"{until - gap.start:.0f}s of audio was not transcribed: "
            f"the connection to {gap.provider} dropped."
        ),
        start_ts=gap.start,
        end_ts=until,
        is_final=True,
    )


@dataclass(frozen=True, slots=True)
class _Frame:
    """One captured frame on its way to the vendor."""

    pcm: bytes
    ts: float
    is_speech: bool


_STOP = object()  # queued by stop() to end the send loop after the last frame

# Longest one connection's teardown step may take. Closing a socket and
# publishing a handful of settled turns is instant when anything works at all,
# so this is only ever reached by something that has stopped answering, and it
# is short because a stop is already waiting on it (see
# ``SessionManager._STOP_DRAIN_TIMEOUT_S``).
_TEARDOWN_TIMEOUT_S = 5.0


class _ConnectionLostError(RuntimeError):
    """Internal: this connection is over, for a reason worth one log line."""


@dataclass(slots=True)
class _OpenTurn:
    """A turn the vendor opened and has not settled, on the capture clock.

    ``handle`` is fixed for the turn's life and ``start`` is not: a vendor may
    revise where a turn began as its endpointing refines (x.ai does; Deepgram's
    own docs warn it may), and the key every revision of the turn is written
    under must not move with it. See :func:`_handle`.
    """

    ref: str
    handle: str
    start: float
    end: float | None = None
    text: str = ""
    published: bool = False
    next_interim: float = 0.0  # monotonic; 0 -> the first partial publishes


def _handle(ref: str, start: float) -> str:
    """The vendor's handle for a turn, or the millisecond the turn began.

    Millisecond-resolution start on the capture clock for a vendor that names
    no turn, which is unique within a connection because two turns cannot begin
    in the same millisecond. Derived when the turn opens and never again, so a
    final that states a different start moves the event's timestamps and not
    its key - deriving it a second time from a revised start would write the
    final as a new row beside the interims it was meant to replace.
    """
    return ref or f"t{round(start * 1000)}"


# Per connection, and only reached by a vendor that is misbehaving: a turn the
# stream is never told the end of, an end for a turn that never arrives, a
# stale handle repeated forever. Each cap is far above any real session's
# working set, and is here so that a connection that runs all evening cannot
# grow one of these dicts without bound (only reconnecting clears them).
_MAX_OPEN_TURNS = 32
_MAX_PENDING_ENDS = 64
_MAX_CLOSED_REFS = 128


class TranscriptStream:
    """One provider's live transcription stream for one session.

    Fed frames through :meth:`feed` (never blocks, called from the capture
    loop), driven by :meth:`run` (one task, owns the connection), ended by
    :meth:`stop`. Events reach the caller through ``publish``, interims and
    finals alike; what to do with them is the caller's, which is where
    diarization and the session bus live.
    """

    def __init__(
        self,
        connector: StreamingConnector,
        *,
        publish: Callable[[TranscriptEvent], Awaitable[None]],
        capture_rate: int,
        config: StreamConfig,
        gap: PendingGap | None = None,
    ) -> None:
        self._connector = connector
        self._publish = publish
        self._capture_rate = capture_rate
        self._config = config
        self._queue: asyncio.Queue[_Frame | object] = asyncio.Queue(maxsize=config.queue_frames)
        self._stopped = False
        # Set by feed() when the queue overflowed: the connection is not
        # draining, and audio has been dropped, so the byte count the vendor's
        # offsets are measured against no longer matches the capture clock.
        # Reconnecting is what restores that mapping (a fresh t0), which is why
        # an overflow is a death here rather than the logged drop it is on the
        # utterance path.
        self._stalled = False
        # Connection-scoped state; _reset_connection() is the list of it.
        self._t0: float | None = None
        self._last_ts = 0.0
        self._last_rx = 0.0
        self._opened_at = 0.0
        self._voiced_at: float | None = None
        self._turns: dict[str, _OpenTurn] = {}
        self._order: list[str] = []  # open turns, oldest first: "the open turn"
        self._pending_end: dict[str, float] = {}  # ref -> capture-clock end
        # Turns this connection has settled, oldest first, used as a bounded
        # ordered set. A vendor whose late partial names one of these would
        # otherwise reopen a settled turn under its own key, replacing the
        # final with an interim that the end of the session then deletes.
        self._closed: dict[str, None] = {}
        self._resampler: Pcm16Stream | None = None
        self._settled = asyncio.Event()
        self._settled.set()
        self._generation = 0  # connections opened so far; part of every turn id
        # The span a dead connection swallowed, published as a gap marker once
        # the next connection's t0 is known. Seeded by the caller where a
        # previous provider died still owing one: the marker belongs to the
        # span, not to the object that was streaming when it opened.
        self._gap = gap

    @property
    def pending_gap(self) -> PendingGap | None:
        """The span this stream stopped transcribing and never closed, if any.

        None in the ordinary case, where the next connection's first frame
        closed the gap. Not None when this provider ran out of reconnects with
        one open, which is exactly when the caller is about to build the next
        provider's stream: it hands this back in, and that stream's first
        written frame ends the span (see :meth:`_write`). Nothing else can,
        because a marker needs a moment where transcription resumed, and this
        object has none left.
        """
        return self._gap

    # -- input ------------------------------------------------------------

    def feed(self, pcm: bytes, ts: float, *, is_speech: bool) -> None:
        """Hand one captured frame to the stream. Never blocks, never raises.

        Called from the capture loop, which must keep emptying the device
        buffer whatever the network is doing, so this only ever enqueues.

        ``is_speech`` is the local VAD's verdict on the frame, and it is here
        for the watchdog alone: it is what lets "we sent speech and heard
        nothing back" be told apart from "nobody has said anything for a
        while". The vendor's own endpointing still decides the turns; Silero
        keeps deciding the WAV's utterance index.
        """
        if self._stopped:
            return
        if self._queue.full():
            with contextlib.suppress(asyncio.QueueEmpty):
                self._queue.get_nowait()
            self._stalled = True
        self._queue.put_nowait(_Frame(pcm=pcm, ts=ts, is_speech=is_speech))

    def stop(self) -> None:
        """No more frames are coming: flush the open turn and finish.

        Idempotent, and safe to call from the capture loop's ``finally``: the
        sentinel it queues is the only thing that ends :meth:`run` short of the
        provider dying, which is the guarantee ``_CAPTURE_DONE`` gives the
        utterance path.
        """
        if self._stopped:
            return
        self._stopped = True
        if self._queue.full():
            with contextlib.suppress(asyncio.QueueEmpty):
                self._queue.get_nowait()
        self._queue.put_nowait(_STOP)

    # -- the driving task -------------------------------------------------

    async def run(self) -> str:
        """Keep this provider streaming until the input ends or it is dead.

        Returns a :class:`StreamOutcome`. It does not raise for a vendor-side
        failure: a dead provider is an answer the caller acts on (fail over),
        not an exception it would have to classify.

        A stop is checked for before every attempt, not only after a connection
        served: Stop arriving while a provider is failing to connect would
        otherwise spend the whole reconnect budget and its backoff on audio
        that is not coming, and end a normal shutdown at ``DEAD`` - which,
        with no fallback behind it, fires the ERROR-level "Transcription
        stopped" alert on the way out of an evening that went fine.
        """
        attempts = 0
        while not self._stopped:
            try:
                await self._connector.open_stream(self._config.glossary)
            except StreamUnsupportedError as exc:
                log.warning(
                    "stream.model.unsupported",
                    provider=self._connector.config.name,
                    provider_id=self._connector.config.id,
                    error=str(exc),
                )
                return StreamOutcome.UNSUPPORTED
            except Exception as exc:
                attempts += 1
                log.warning(
                    "stream.connect.failed",
                    provider=self._connector.config.name,
                    provider_id=self._connector.config.id,
                    attempt=attempts,
                    error=str(exc),
                )
            else:
                await self._serve()
                if self._stopped:
                    return StreamOutcome.ENDED
                attempts = 0 if self._was_healthy() else attempts + 1
            if attempts > self._config.max_reconnects:
                log.error(
                    "stream.provider.dead",
                    provider=self._connector.config.name,
                    provider_id=self._connector.config.id,
                    attempts=attempts,
                )
                # The gap this provider opened stays open: it ends where
                # transcription resumes, which is the next provider's first
                # frame or nowhere. See :attr:`pending_gap`.
                return StreamOutcome.DEAD
            await asyncio.sleep(self._config.reconnect_backoff_s * max(1, attempts))
        return StreamOutcome.ENDED

    def _was_healthy(self) -> bool:
        """Whether the connection just closed had earned a fresh budget."""
        return time.monotonic() - self._opened_at >= self._config.healthy_after_s

    async def _serve(self) -> None:
        """One connection's whole life, ending in either input or the socket."""
        self._reset_connection()
        reader = asyncio.create_task(self._read())
        try:
            await self._send_loop(reader)
        except _ConnectionLostError as dead:
            log.warning(
                "stream.connection.lost",
                provider=self._connector.config.name,
                provider_id=self._connector.config.id,
                reason=str(dead),
            )
            self._open_gap()
        except Exception as exc:
            # Deliberately broad: the vendor SDKs raise their own exception
            # trees (websockets, httpx, google-genai) and every one of them
            # means the same thing here, "this socket is gone". Classifying
            # them is the failover path's job, one level up.
            log.warning(
                "stream.connection.failed",
                provider=self._connector.config.name,
                provider_id=self._connector.config.id,
                error=str(exc),
            )
            self._open_gap()
        finally:
            reader.cancel()
            with contextlib.suppress(BaseException):
                await reader
            await self._teardown()

    async def _teardown(self) -> None:
        """Close the socket and settle what was open, bounded and never raising.

        Every step here is best-effort, and for two reasons that both end the
        session badly if they are not. An exception escaping this escapes
        :meth:`run`, whose whole contract is that a provider failure is a
        return value; the caller would then skip its failover and handoff
        handling and the recording would carry on with nothing transcribing it,
        healthz green, until Stop turned the session into an ERROR.

        And it is bounded because the cancellation that arrives when a stop's
        drain times out is swallowed above (awaiting a cancelled reader
        consumes it), so without a deadline a hung vendor call here would hold
        the whole shutdown for as long as its own client timeout allows.
        """
        steps = (("close", self._connector.close_stream), ("settle", self._settle_open))
        for step, run_step in steps:
            # Bounded one at a time, so a socket that will not close still
            # costs only its own deadline and the open turns are settled after
            # it either way.
            try:
                async with asyncio.timeout(_TEARDOWN_TIMEOUT_S):
                    await run_step()
            except Exception as exc:
                log.warning(
                    "stream.teardown.failed",
                    step=step,
                    provider=self._connector.config.name,
                    provider_id=self._connector.config.id,
                    error=f"{type(exc).__name__}: {exc}",
                )

    async def _send_loop(self, reader: asyncio.Task[None]) -> None:
        """Drain the frame queue into the connection until input or it ends."""
        while True:
            item = await self._queue.get()
            if not isinstance(item, _Frame):
                await self._finish_turns(reader)
                return
            if reader.done():
                # The socket ended under us: whatever the reader raised, or the
                # fact that it simply returned, is this connection's death.
                raise _ConnectionLostError(_reader_reason(reader))
            await self._write(item)
            self._check_liveness()

    async def _write(self, frame: _Frame) -> None:
        """Resample one frame onto the connection, anchoring the clock on the first."""
        if self._t0 is None:
            self._t0 = frame.ts
            await self._publish_gap(frame.ts)
        self._last_ts = frame.ts
        if frame.is_speech:
            self._voiced_at = time.monotonic()
        if self._resampler is None:
            self._resampler = Pcm16Stream(self._capture_rate, self._connector.stream_rate)
        block = self._resampler.process(frame.pcm)
        if not block:
            return  # soxr held this one back; the next call carries it
        try:
            async with asyncio.timeout(self._config.send_timeout_s):
                await self._connector.send_audio(block)
        except TimeoutError as exc:
            raise _ConnectionLostError("a write blocked") from exc

    def _check_liveness(self) -> None:
        """Fail a connection written speech that has since gone quiet and said nothing."""
        if self._stalled:
            raise _ConnectionLostError("frames dropped: the connection is not draining")
        now = time.monotonic()
        quiet_for = now - self._last_rx
        if quiet_for < self._config.watchdog_s:
            return
        if self._voiced_at is None or self._voiced_at < self._last_rx:
            return  # nothing worth transcribing has gone out since we last heard
        if now - self._voiced_at < self._config.quiet_grace_s:
            return  # still mid-turn; see StreamConfig.quiet_grace_s
        raise _ConnectionLostError(f"nothing received for {quiet_for:.0f}s past a turn's end")

    async def _read(self) -> None:
        """Turn the vendor's messages into events, for this connection's life.

        Every signal refreshes the liveness watchdog, :class:`StreamAlive`
        included: what the watchdog asks is whether the socket is answering at
        all, and a connector that has nothing to report about a turn but was
        just spoken to answers exactly that.
        """
        async for signal in self._connector.signals():
            self._last_rx = time.monotonic()
            await self._apply(signal)

    # -- turn bookkeeping -------------------------------------------------

    async def _apply(self, signal: StreamSignal) -> None:
        match signal:
            case StreamAlive():
                # The whole signal: :meth:`_read` has already refreshed the
                # watchdog for it, and there is no turn to say anything about.
                pass
            case TurnStarted(at=at, ref=ref):
                await self._open(ref, self._clock(at))
            case TurnPartial(text=text, ref=ref, append=append):
                if self._is_closed(ref):
                    # A partial for a turn already settled on this connection.
                    # Opening one under that key would replace the final that
                    # is on screen with an interim, which the end of the
                    # session would then delete as a half-typed row.
                    log.debug("stream.partial.late", ref=ref)
                    return
                turn = await self._open(ref, self._clock(None))
                turn.text = turn.text + text if append else text
                await self._publish_interim(turn)
            case TurnEnded(at=at, ref=ref):
                end = self._clock(at)
                turn = self._turns.get(self._resolve(ref))
                if turn is None:
                    self._pending_end[ref] = end
                    _trim(self._pending_end, _MAX_PENDING_ENDS)
                else:
                    turn.end = end
            case TurnFinal(text=text, ref=ref, words=words, at=at, to=to):
                await self._close(ref, text, words, at, to)

    async def _open(self, ref: str, start: float) -> _OpenTurn:
        """The turn ``ref`` names, opening one at ``start`` when none is open."""
        key = self._resolve(ref)
        turn = self._turns.get(key)
        if turn is not None:
            return turn
        turn = _OpenTurn(
            ref=key,
            handle=_handle(key, start),
            start=start,
            end=self._pending_end.pop(key, None),
        )
        self._turns[key] = turn
        self._order.append(key)
        self._settled.clear()
        while len(self._order) > _MAX_OPEN_TURNS:
            await self._evict_oldest()
        return turn

    def _is_closed(self, ref: str) -> bool:
        """Whether ``ref`` names a turn this connection has already settled.

        Only ever true for a vendor that names its turns. One that does not has
        exactly one turn open at a time and no way to name an old one, so an
        unnamed partial after a final is the next turn beginning, not a late
        word about the last.
        """
        return bool(ref) and ref in self._closed

    async def _evict_oldest(self) -> None:
        """Settle the oldest open turn to keep the connection's books bounded.

        Reached only by a vendor that opens turns and never ends them. Settling
        rather than dropping, for the same reason :meth:`_settle_open` settles:
        a turn that published an interim and is then forgotten leaves a dimmed
        row that nothing will ever replace.
        """
        key = self._order.pop(0)
        turn = self._turns.pop(key)
        self._note_closed(key)
        log.warning("stream.turns.overflow", ref=key, open_turns=len(self._order) + 1)
        if turn.published:
            await self._emit(turn.handle, turn.text, start=turn.start, end=turn.end, final=True)

    def _note_closed(self, key: str) -> None:
        """Remember a settled turn's handle, for the late partial that names it."""
        if not key:
            return
        self._closed[key] = None
        _trim(self._closed, _MAX_CLOSED_REFS)

    def _resolve(self, ref: str) -> str:
        """Which turn a signal belongs to when the vendor named none.

        A protocol without turn ids has exactly one turn open at a time, so an
        unnamed signal belongs to the oldest open turn, and opens a new one
        when none is. Vendors that do name their turns never reach the fallback.
        """
        if ref or not self._order:
            return ref
        return self._order[0]

    async def _close(
        self,
        ref: str,
        text: str,
        words: tuple[Word, ...],
        at: float | None,
        to: float | None,
    ) -> None:
        """Publish a turn's settled text and forget the turn."""
        key = self._resolve(ref)
        turn = self._turns.pop(key, None)
        if turn is not None:
            self._order.remove(key)
        self._note_closed(key)
        # A turn can close without ever having been opened here: a vendor that
        # announces the end of a turn it never announced the start of, or one
        # whose two messages arrive out of order. Whatever end offset was
        # already stated for it stands either way.
        pending = self._pending_end.pop(key, None)
        known_end = turn.end if turn is not None and turn.end is not None else pending
        start = self._clock(at) if at is not None else (turn.start if turn else self._clock(None))
        end = self._clock(to) if to is not None else known_end
        if not text and turn is not None and turn.published:
            # A vendor that comes back empty for a turn it has been sending
            # interims for. Dropping this settles nothing, and the interim row
            # stays dimmed until the end of the session deletes it, taking the
            # only text there ever was with it. An empty final is routine
            # rather than exceptional - Deepgram's endpointing emits one per
            # near-silent lead-in - so the last interim is the answer here,
            # not silence.
            text = turn.text
            log.debug("stream.final.empty", ref=key)
        # The handle, not the key: ``at`` above may have moved this turn's
        # start, and the row every interim was written under must not move
        # with it (see :func:`_handle`).
        handle = turn.handle if turn is not None else _handle(key, start)
        await self._emit(handle, text, start=start, end=end, words=words, final=True)
        if not self._turns:
            self._settled.set()

    async def _publish_interim(self, turn: _OpenTurn) -> None:
        """Publish an open turn's text, at most once per configured interval.

        ``published`` is set only where a row was really written, because it is
        what :meth:`_close` and :meth:`_settle_open` read as "there is a dimmed
        row on screen that has to be replaced by something".
        """
        now = time.monotonic()
        if not turn.text or now < turn.next_interim:
            return
        turn.next_interim = now + self._config.interim_interval_s
        turn.published = True
        await self._emit(turn.handle, turn.text, start=turn.start, end=None, final=False)

    async def _emit(
        self,
        handle: str,
        text: str,
        *,
        start: float,
        end: float | None,
        final: bool,
        words: tuple[Word, ...] = (),
    ) -> None:
        if not text:
            return  # a vendor that heard nothing is not a segment
        shifted = [
            w.model_copy(update={"start": self._clock(w.start), "end": self._clock(w.end)})
            for w in words
        ]
        await self._publish(
            TranscriptEvent(
                session_id=self._config.session_id,
                source=self._connector.config.id,
                text=text,
                words=shifted,
                start_ts=start,
                end_ts=end if end is not None else max(start, self._last_ts),
                is_final=final,
                # Stable for a turn's whole life, interims included: it is what
                # a growing interim is replaced *by* rather than appended to,
                # in the transcript table and in both live feeds.
                turn_id=self._turn_id(handle),
            )
        )

    def _turn_id(self, handle: str) -> str:
        """This turn's id on the wire: where it is in this session's stream.

        The handle comes from :func:`_handle`, fixed when the turn opened, so
        nothing a later signal says about the turn can move the key it is
        written under.

        Both halves of the prefix earn their place. The provider id keeps two
        vendors' handles apart in a session that failed over. The connection
        counter keeps one vendor's handles apart across a reconnect: OpenAI
        numbers its items per session, so the first turn after a dropped socket
        is ``msg_001`` again, and without the counter it would replace the
        first turn of the evening rather than follow it.
        """
        return f"{self._connector.config.id}:{self._generation}:{handle}"

    def _clock(self, offset: float | None) -> float:
        """A vendor offset on the capture clock.

        ``t0`` is the capture timestamp of the first frame written to this
        connection and the vendor counts from the first byte it received, so
        the two name the same instant. None means the vendor stated no offset,
        in which case the last frame written is the best answer there is.
        """
        if offset is None or self._t0 is None:
            return self._last_ts
        return self._t0 + offset

    # -- endings ----------------------------------------------------------

    async def _finish_turns(self, reader: asyncio.Task[None]) -> None:
        """Flush the vendor and give the last finals a bounded time to arrive.

        Whatever is still open when that time is up is settled by
        :meth:`_settle_open`, which runs from :meth:`_serve`'s ``finally`` and
        so covers this ending and every other one alike.
        """
        if reader.done():
            return
        with contextlib.suppress(Exception):
            await self._connector.flush_input()
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(self._config.final_wait_s):
                while self._turns:
                    await self._settled.wait()

    def _open_gap(self) -> None:
        """Note where a dead connection started losing audio.

        From the open turn's start when one was in flight, because that turn's
        audio went out and came back as nothing; from the last frame written
        otherwise. The marker itself waits for the next connection's ``t0``, so
        that it spans exactly what was lost.
        """
        if self._t0 is None:
            # This connection never wrote a frame, so it lost nothing this
            # object can name an instant for. Any span the previous connection
            # opened is still open (only a write closes one) and already starts
            # in the right place; on a stream that never wrote at all,
            # ``_last_ts`` is zero, which is no instant on the capture clock.
            return
        oldest = self._turns[self._order[0]].start if self._order else self._last_ts
        if self._gap is None:
            self._gap = PendingGap(start=oldest, provider=self._connector.config.name)
        elif oldest < self._gap.start:
            # An older span is already open (this provider's previous
            # connection, or the one before this provider). It keeps its name:
            # the marker says who stopped transcribing where it starts.
            self._gap = replace(self._gap, start=oldest)

    async def _settle_open(self) -> None:
        """Publish what every open turn has so far as its final, and forget it.

        A turn still open when its connection dies, or when the microphone
        stops, has already been published as an interim if it had any text at
        all. Leaving it there would leave a dimmed row that never settles, on
        the screen and in the table; publishing it as a final replaces that row
        with the last thing the vendor actually said.
        """
        turns, self._turns, self._order = list(self._turns.values()), {}, []
        self._settled.set()
        for turn in turns:
            self._note_closed(turn.ref)
            if turn.published:
                await self._emit(turn.handle, turn.text, start=turn.start, end=turn.end, final=True)

    async def _publish_gap(self, until: float) -> None:
        """Mark the span a dead connection swallowed, if there was one.

        Called with the first frame of a working connection, which is the
        moment the span is known to have ended. A gap still open when this
        provider gives up outlives the object (see :attr:`pending_gap`).
        """
        gap, self._gap = self._gap, None
        event = gap_event(self._config.session_id, gap, until)
        if event is not None:
            await self._publish(event)

    def _reset_connection(self) -> None:
        """Forget everything that belonged to the connection just closed."""
        now = time.monotonic()
        self._generation += 1
        self._t0 = None
        self._opened_at = now
        self._last_rx = now
        self._voiced_at = None
        self._turns = {}
        self._order = []
        self._pending_end = {}
        self._closed = {}
        self._resampler = None
        self._stalled = False
        self._settled = asyncio.Event()
        self._settled.set()
        self._drain()

    def _drain(self) -> None:
        """Drop all but the tail of what was captured with nowhere to send it.

        See ``StreamConfig.resume_keep_frames`` for why it is a tail rather
        than all or nothing. What is dropped is what the gap marker covers: the
        marker runs up to the timestamp of the first frame actually written, so
        whatever survives here is outside it by construction.
        """
        while self._queue.qsize() > self._config.resume_keep_frames:
            try:
                item = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            if item is _STOP:
                self._queue.put_nowait(_STOP)
                return


def _trim[V](book: dict[str, V], limit: int) -> None:
    """Drop the oldest entries of an insertion-ordered book down to ``limit``.

    The books are small and only a misbehaving vendor ever fills one, so the
    oldest entry is both the least likely to still matter and the cheapest to
    identify.
    """
    while len(book) > limit:
        del book[next(iter(book))]


def _reader_reason(reader: asyncio.Task[None]) -> str:
    """Why the reader task stopped, as one line for the log."""
    if reader.cancelled():
        return "reader cancelled"
    exc = reader.exception()
    return f"{type(exc).__name__}: {exc}" if exc is not None else "the vendor closed the socket"
