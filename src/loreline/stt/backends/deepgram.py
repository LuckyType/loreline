"""Deepgram live STT connector (WebSocket), in both connector shapes.

Deepgram's live endpoint takes raw linear16 PCM over a WebSocket and answers
with ``Results`` messages: a growing interim for the segment it is working on,
then that same segment again with ``is_final``, and per-word speaker labels
when ``diarize=true`` (inline diarization). Word and segment offsets are
seconds from the first byte of audio on the connection, which is exactly the
base the streaming contract measures ``at``/``to`` in.

**Streaming** (``StreamingConnector``, what a live capture takes, ADR 0006) is
one socket for the whole session, with Deepgram's own endpointing deciding the
turns. The protocol fact that shapes everything here is that one turn is
*several* ``Results``:

* interim frames (``is_final=false``) restate the whole current segment every
  time, so they replace rather than append (``TurnPartial(append=False)``);
* a segment is settled by ``is_final=true``, and a turn can hold several of
  them, the first routinely a near-silent lead-in with an empty transcript;
* the turn itself ends at ``speech_final=true`` (endpointing heard a pause) or
  at an ``UtteranceEnd`` message (a long enough gap between word timings),
  whichever comes first. Deepgram documents using both, because ``speech_final``
  rides on silence detection and a noisy room never goes silent, while
  ``UtteranceEnd`` reads word gaps and ignores door slams and street noise.
  An ``UtteranceEnd`` can arrive *late*, describing a turn ``speech_final``
  already closed, and the documented ``last_word_end: -1`` marker for that is
  not set when it does. Measured over two runs and 150 seconds of real speech:
  four arrived, not one carried ``-1``, and every one named a time before the
  turn that was open by then (8.8 against 9.6, in both runs; 13.2 against
  13.85; 55.07 against 55.72). So the timestamp is what decides: one behind
  the open turn's start belongs to an earlier turn and is dropped. Acting on
  them instead ended a turn that had settled nothing, orphaning its interims
  as a dimmed row that never resolves.

So this connector holds one turn's settled segments, publishes the joined text
as an interim every time it grows, and emits one ``TurnFinal`` per turn with
every word of it, speakers included. Deepgram's speaker numbers are consistent
for a connection's whole life, which is what makes inline diarization mean
something across a session rather than only inside one utterance: on 90 seconds
built from two readers alternated in 6 to 15 second pieces, 91% of words
outside the splices carried the label of the reader who actually said them,
and the same reader kept the same number from the first turn to the last. A
reconnect starts the numbering again, which is the same span the gap marker
covers.

A turn's start is the ``start`` of the first segment that carried text. The
segments of a connection are contiguous, so that offset is where the previous
segment ended and it includes the silence in between, which is a few hundred
milliseconds early and is the price of a start that never moves. It has to
never move: the stream keys every revision of a turn on the start it opened
with, so a start the vendor refines writes a second row instead of replacing
the first.

``vad_events=true`` is deliberately not asked for, which one measured run
settled. ``SpeechStarted`` sounds like the better answer and is not: over 60
seconds of read speech it arrived twelve times against seven turns, because it
fires per segment rather than per turn, and each timestamp *lagged* the segment
start it belonged to (10.03 against 9.600). It is a voice-onset marker, not a
turn boundary, and using it moved a turn's start later and made it depend on
which onset happened to be last.

``Finalize`` is the flush: it makes the server process what it has buffered and
answer with ``from_finalize: true``, which closes the open turn here. It is not
guaranteed to arrive when little audio is buffered, which is why the stream
above also bounds how long it waits. ``CloseStream`` is the close, the only
flush Deepgram defines unconditionally.

**One utterance per call** (``Connector``, ADR 0005) is kept for re-processing,
for the call-shaped fallback path and for every batch caller. See
:meth:`transcribe_one` for why that shape opens a connection per utterance and
why that is not an argument against the shared stream above it.

Docs:
- https://developers.deepgram.com/reference/speech-to-text-api/listen-streaming
- https://developers.deepgram.com/docs/understand-endpointing-interim-results
- https://developers.deepgram.com/docs/understanding-end-of-speech-detection
- https://developers.deepgram.com/docs/utterance-end
- https://developers.deepgram.com/docs/finalize
- https://developers.deepgram.com/docs/audio-keep-alive
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import AsyncIterator
from http import HTTPStatus
from urllib.parse import urlencode

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosedOK, InvalidStatus

from loreline.audio.chunker import Utterance
from loreline.capabilities import surface_for
from loreline.capability_config import TranscribeCapabilities
from loreline.health import error_message
from loreline.logging import get_logger
from loreline.models import Glossary, Interaction, ProviderConfig, ProviderKind, Word
from loreline.secrets import SecretStore
from loreline.stt.backends._deepgram import listen_params, parse_alternative
from loreline.stt.backends._ws import (
    as_dict,
    as_list,
    as_obj_dict,
    get_bool,
    get_float,
    get_str,
)
from loreline.stt.base import Connector, Transcription, glossary_terms, secret_for
from loreline.stt.registry import register
from loreline.stt.streaming import (
    StreamingConnector,
    StreamUnsupportedError,
    TurnFinal,
    TurnPartial,
    TurnSignal,
    TurnStarted,
)

log = get_logger(__name__)

# Safety net per received frame; CloseStream -> Metadata is the real
# end-of-flush signal, and results stream back within a couple of seconds.
_RECV_TIMEOUT_S = 10.0

# Milliseconds of silence after which Deepgram finalizes the segment and flags
# it ``speech_final``. Deepgram's own default is 10 ms, which cuts a turn at
# every breath; 500 ms matches what the OpenAI connector asks its server VAD
# for, so the two migrated connectors cut turns at comparable places, and it
# is well under the 800 ms of trailing silence the local chunker needs before
# the utterance path even sends a request.
_ENDPOINTING_MS = 500
# The backstop for a room where background noise keeps the silence detector
# from ever firing: Deepgram measures the gap between word timings instead.
# 1000 ms is the documented minimum, and it only ever wins where
# ``speech_final`` did not, since whichever arrives first closes the turn.
_UTTERANCE_END_MS = 1000
# Deepgram closes a live socket that has seen neither audio nor a KeepAlive for
# ten seconds (NET-0001). The stream above writes *every* captured frame,
# silence included, so audio never stops while a microphone is running and this
# never fires in the ordinary case. It covers the windows where no frame is in
# flight anyway: the wait for the last finals after ``Finalize``, and a capture
# device that stalls. Wall clock, deliberately not the capture clock, which is
# the stream's to own.
_KEEPALIVE_IDLE_S = 5.0
# ``UtteranceEnd`` reports this when the segment was already finalized before
# its gap condition was met, and the docs say to disregard the message.
_ALREADY_FINAL = -1.0

# What the streaming shape adds to the query both shapes share. Interim results
# are the feature, and they are also required for ``utterance_end_ms`` to work
# at all. ``vad_events`` is absent on purpose; see the module docstring.
_STREAM_PARAMS: list[tuple[str, str]] = [
    ("interim_results", "true"),
    ("endpointing", str(_ENDPOINTING_MS)),
    ("utterance_end_ms", str(_UTTERANCE_END_MS)),
]


class _TurnState:
    """Deepgram's several ``Results`` per turn, translated into turn signals.

    Split out from the socket so the translation can be driven by recorded
    frames (see ``tests/unit/test_deepgram_frames.py``). It holds exactly what
    the protocol forces it to hold and nothing else: a turn's settled segments,
    because Deepgram states them one at a time and only says "that was the
    turn" afterwards.
    """

    def __init__(self) -> None:
        self.error: str | None = None
        self._open = False
        self._start = 0.0  # where the open turn's first segment began
        self._end: float | None = None
        self._parts: list[str] = []  # this turn's settled segments
        self._interim = ""  # the unsettled segment's latest text, if any
        self._words: list[Word] = []

    def apply(self, raw: str | bytes) -> list[TurnSignal]:
        """One message from the socket, as zero or more signals."""
        message = as_dict(raw)
        kind = get_str(message, "type")
        if kind == "Results":
            return self._results(message)
        if kind == "UtteranceEnd":
            return self._utterance_end(message)
        if kind.lower() == "error":
            self.error = next(
                (get_str(message, key) for key in ("description", "message") if message.get(key)),
                "Deepgram returned an error frame",
            )
        return []

    def _results(self, message: dict[str, object]) -> list[TurnSignal]:
        """A transcript frame: interim, settled segment, or the turn's end."""
        transcript, words = _parse_results(message)
        is_final = get_bool(message, "is_final")
        signals: list[TurnSignal] = []
        if transcript and not self._open:
            self._open = True
            self._start = get_float(message, "start")
            signals.append(TurnStarted(at=self._start))
        if is_final:
            if transcript:
                self._parts.append(transcript)
            self._words.extend(words)
            self._interim = ""  # this segment is settled; its interims are spent
            self._end = get_float(message, "start") + get_float(message, "duration")
        elif transcript:
            self._interim = transcript
        if not self._open:
            return signals
        # speech_final is endpointing's own "that was a turn"; from_finalize is
        # the answer to the Finalize we send when the microphone stops, and it
        # ends the turn for the same reason: no more audio is coming for it.
        if get_bool(message, "speech_final") or get_bool(message, "from_finalize"):
            signals.append(self._close(self._end))
        elif transcript:
            # Interims restate the current segment in full, so the turn's text
            # is the settled segments plus this one, replacing what was there.
            signals.append(TurnPartial(text=self._text()))
        return signals

    def _utterance_end(self, message: dict[str, object]) -> list[TurnSignal]:
        """A gap between word timings closes the turn endpointing did not.

        Two ways this message is about a turn that is already gone. One is
        documented and was never once observed: ``last_word_end: -1``, defined
        as "already finalized, disregard". The other is what all four of them
        looked like across 150 seconds of real speech: a real timestamp landing
        *before* the open turn began, which makes it a late message about the
        turn before. Acting on either ends a turn that has settled nothing.
        """
        end = get_float(message, "last_word_end", _ALREADY_FINAL)
        if not self._open or end <= self._start:
            return []
        return [self._close(end)]

    def _close(self, end: float | None) -> TurnFinal:
        """Settle the open turn and forget it.

        ``at`` is deliberately left off: the stream keys every revision of a
        turn on the start it was opened with, so restating it here would write
        the final as a second row rather than as the interims' replacement.
        """
        final = TurnFinal(text=self._text(), words=tuple(self._words), to=end)
        self._open = False
        self._start = 0.0
        self._end = None
        self._parts = []
        self._interim = ""
        self._words = []
        return final

    def _text(self) -> str:
        """The turn so far: its settled segments, plus the one still in hand.

        Deepgram's own advice for a turn that ends on ``UtteranceEnd`` is to
        take the last transcript received, which is what including the unsettled
        segment does. It cannot double a segment, because settling one clears
        the interims it superseded.
        """
        return " ".join(part for part in [*self._parts, self._interim] if part)


class DeepgramBackend(Connector[str], StreamingConnector):
    """Live transcription with inline diarization via Deepgram, both shapes.

    See the module docstring for what each shape does. They hold separate
    sockets: the streaming one asks for interim results and Deepgram's own
    endpointing, the per-call one wants neither, and one connection cannot be
    opened both ways because the configuration is in the query string.

    The prepared value of the per-call shape is the socket URL with its query
    string built; the streaming shape builds the same URL and adds its own
    parameters to it, so the two cannot drift on model, language, diarization
    or the glossary field.
    """

    def __init__(
        self,
        config: ProviderConfig,
        *,
        model: str | None = None,
        caps: TranscribeCapabilities | None = None,
        api_key: str | None = None,
        language: str | None = None,
    ) -> None:
        super().__init__(config)
        self._api_key = api_key
        self._language = language or config.language
        self._model = model
        self._caps = caps
        self._endpoint = surface_for(config, Interaction.TRANSCRIBE, "realtime")
        self._url = self._endpoint.url
        self._stream_ws: ClientConnection | None = None
        self._keepalive: asyncio.Task[None] | None = None
        self._last_send = 0.0

    def prepare(self, glossary: Glossary | None) -> str:
        params = listen_params(
            model=self._model,
            caps=self._caps,
            language=self._language,
            terms=glossary_terms(glossary),
            realtime=True,
        )
        # Streaming-only: the batch endpoint reads these from the WAV header,
        # while a raw PCM socket has no container to read them from.
        params.extend(
            [
                ("encoding", "linear16"),
                ("sample_rate", str(self.config.sample_rate)),
                ("channels", "1"),
            ]
        )
        return f"{self._url}?{urlencode(params)}"

    @property
    def _headers(self) -> dict[str, str]:
        return self._endpoint.request_headers(self._api_key)

    async def transcribe_one(self, utterance: Utterance, prepared: str) -> Transcription:
        # One connection per utterance, closed with CloseStream: that is the
        # only flush signal Deepgram defines unconditionally - the server
        # finalizes buffered audio, streams the remaining final Results, then
        # Metadata and a clean close. Finalize would not do here, because its
        # from_finalize ack is conditional ("not guaranteed if there is no
        # significant amount of audio data to process") and can arrive late,
        # where on a shared socket it would poison the next utterance's reads.
        # That is a fact about flushing a *caller-bounded* utterance through a
        # shared stream, and it is why this shape reconnects; the streaming
        # shape above has no per-utterance flush and one reader for the life of
        # the session, so the same facts cost it nothing (ADR 0006).
        # Deepgram's own endpointing splits one utterance into several final
        # Results frames, and the first is routinely a near-silent lead-in
        # with an empty transcript - accumulate them all, not just the first.
        parts: list[str] = []
        words: list[Word] = []
        async with connect(prepared, additional_headers=self._headers) as ws:
            await ws.send(utterance.pcm)
            await ws.send(json.dumps({"type": "CloseStream"}))
            while True:
                try:
                    async with asyncio.timeout(_RECV_TIMEOUT_S):
                        raw = await ws.recv()
                except (TimeoutError, ConnectionClosedOK):
                    break
                message = as_dict(raw)
                kind = get_str(message, "type")
                if kind == "Results" and get_bool(message, "is_final"):
                    text, more = _parse_results(message, offset=utterance.start)
                    if text:
                        parts.append(text)
                    words.extend(more)
                elif kind in {"Metadata", "Close"}:
                    break
        return Transcription(text=" ".join(parts), words=words)

    # -- the streaming shape ---------------------------------------------

    @property
    def stream_rate(self) -> int:
        """The capture rate itself: Deepgram takes whatever we declare.

        ``sample_rate`` is a query parameter rather than something the service
        insists on, so the honest answer is "no resampling", and the value here
        is the one :meth:`prepare` puts in the query string.
        """
        return self.config.sample_rate

    async def open_stream(self, glossary: Glossary | None) -> None:
        """Open one live socket for a whole capture, endpointing switched on.

        Deepgram acks no configuration: the query string is the configuration,
        so a model or a parameter it refuses is refused on the HTTP upgrade,
        as a 400 with the reason in the body. That is an answer no retry can
        change, hence :class:`StreamUnsupportedError` rather than a failed
        attempt - Deepgram's Flux models are the live case, since they speak a
        different protocol on ``/v2/listen`` and are not in this endpoint's
        model enum at all.
        """
        await self.close_stream()  # a reconnect must not leak the dead socket
        url = f"{self.prepare(glossary)}&{urlencode(_STREAM_PARAMS)}"
        try:
            self._stream_ws = await connect(url, additional_headers=self._headers)
        except InvalidStatus as exc:
            refusal = _refusal(exc, self._model)
            if refusal is None:
                raise
            raise refusal from exc
        self._last_send = time.monotonic()
        self._keepalive = asyncio.create_task(self._keep_alive())

    async def send_audio(self, pcm: bytes) -> None:
        ws = self._stream_ws
        if ws is None:
            msg = "send_audio before open_stream"
            raise RuntimeError(msg)
        await ws.send(pcm)
        self._last_send = time.monotonic()

    async def signals(self) -> AsyncIterator[TurnSignal]:
        """Translate this connection's messages into turn signals.

        Every offset Deepgram states is seconds into the audio it has received
        on this connection, which is the base the stream's ``t0`` mapping
        expects, so nothing here converts to the session clock.

        An error frame ends the iteration rather than being skipped: a live
        request that was rejected is not going to start working on the next
        frame, and ending here is what lets the stream reconnect or fail over
        instead of streaming into silence. ``Metadata`` and the keepalive acks
        are consumed and ignored, which is what keeps the liveness watchdog
        seeing a connection that is merely quiet as alive.
        """
        ws = self._stream_ws
        if ws is None:
            return
        state = _TurnState()
        async for raw in ws:
            for signal in state.apply(raw):
                yield signal
            if state.error is not None:
                log.warning("deepgram.stream.error", provider=self.config.id, detail=state.error)
                return

    async def flush_input(self) -> None:
        """Finalize what is buffered, so the last turn is transcribed.

        Deepgram's endpointing settles every turn but the one that was open
        when the microphone stopped, and that one has no trailing silence to
        end it. ``Finalize`` makes the server process what it holds and answer
        with ``from_finalize: true``, which closes the turn here. The docs do
        not guarantee that answer when little audio is buffered, which is
        exactly the case where there was nothing left to say.
        """
        ws = self._stream_ws
        if ws is not None:
            await ws.send(json.dumps({"type": "Finalize"}))

    async def close_stream(self) -> None:
        """Say CloseStream, then drop the socket.

        The stream has already stopped reading by the time this runs, so the
        results Deepgram flushes in reply reach nobody; it is sent anyway
        because it is how this protocol says "that was all the audio", and an
        aborted TCP connection is not.
        """
        task, self._keepalive = self._keepalive, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(BaseException):
                await task
        ws, self._stream_ws = self._stream_ws, None
        if ws is not None:
            with contextlib.suppress(Exception):
                await ws.send(json.dumps({"type": "CloseStream"}))
            with contextlib.suppress(Exception):
                await ws.close()

    async def _keep_alive(self) -> None:
        """Hold the socket open across a lull in the frames. See ``_KEEPALIVE_IDLE_S``."""
        while True:
            await asyncio.sleep(_KEEPALIVE_IDLE_S / 2)
            ws = self._stream_ws
            if ws is None:
                return
            if time.monotonic() - self._last_send < _KEEPALIVE_IDLE_S:
                continue
            try:
                await ws.send(json.dumps({"type": "KeepAlive"}))
            except Exception:  # a dead socket is the reader's to report, not this task's
                return
            self._last_send = time.monotonic()

    async def aclose(self) -> None:
        await self.close_stream()


def _refusal(exc: InvalidStatus, model: str | None) -> StreamUnsupportedError | None:
    """Grade a rejected upgrade, or None where it is a connection to retry.

    400 is what Deepgram answers a query string it will not serve - an unknown
    model, an illegal parameter combination - and no retry changes it. Anything
    else (401, 429, 5xx, a proxy) may well work on the next attempt, so it
    stays the exception it was and spends one of the stream's reconnects.
    """
    if exc.response.status_code != HTTPStatus.BAD_REQUEST:
        return None
    detail = error_message(exc.response.body.decode("utf-8", "replace"))
    return StreamUnsupportedError(
        f"Deepgram will not stream {model or 'this request'}: "
        f"{detail or exc.response.reason_phrase}"
    )


def _parse_results(message: dict[str, object], *, offset: float = 0.0) -> tuple[str, list[Word]]:
    """A streaming ``Results`` frame: one channel, its first alternative."""
    channel = as_obj_dict(message.get("channel"))
    alternatives = as_list(channel.get("alternatives"))
    if not alternatives:
        return "", []
    return parse_alternative(as_obj_dict(alternatives[0]), offset=offset)


@register(ProviderKind.DEEPGRAM, realtime=True)
def _factory(  # pyright: ignore[reportUnusedFunction]
    config: ProviderConfig,
    secrets: SecretStore,
    model: str | None,
    caps: TranscribeCapabilities | None,
) -> DeepgramBackend:
    return DeepgramBackend(config, model=model, caps=caps, api_key=secret_for(config, secrets))
