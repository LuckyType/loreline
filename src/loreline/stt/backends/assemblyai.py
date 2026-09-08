"""AssemblyAI Universal-Streaming (v3) STT connector, in both connector shapes.

One WebSocket protocol, two ways of using it, because this repo has two
connector contracts and AssemblyAI serves both well.

**Streaming** (:class:`~loreline.stt.streaming.StreamingConnector`, what a live
capture takes, ADR 0006) is the shape v3 was built for: one socket for the whole
session, audio in continuously, and AssemblyAI's own endpointing deciding where
the turns are. What it sends back, and what each message becomes here:

* ``Turn`` with ``end_of_turn=false`` is an interim. Its ``transcript`` is the
  *whole* turn so far, not the new words, so it becomes a ``TurnPartial`` with
  ``append=False``; a partial may also revise words it already sent ("away
  totally." became "a totally new life."), which is the same reason.
* ``Turn`` with ``end_of_turn=true`` settles the turn: one ``TurnFinal``
  carrying ``transcript`` and the words, each with its ``speaker``.
* ``turn_order`` is the handle every message of one turn shares, so it is the
  ``ref`` the stream correlates by, and the key an interim is replaced under.
  It also makes the ``format_turns`` duplicate free: a turn re-sent formatted
  after it ended arrives as a second ``TurnFinal`` under the same ref, and
  replacing a row is what the streaming path does with those anyway.
* ``SpeakerRevision`` is a whole-session diarization pass AssemblyAI runs at
  the end, and the only place a *published* turn's speakers can still improve:
  it re-states the words of the turns whose labels changed. Each revision
  becomes another ``TurnFinal`` under the turn's own ref, so the stored row is
  replaced with the corrected speakers. Best effort by nature, since it lands
  while the session is already shutting down.
* ``SpeechStarted`` says nothing about a turn: it carries a timestamp but no
  ``turn_order``, so there is nothing to attach it to, and the turn's start
  comes from the first word offset of its own first ``Turn`` message, which
  arrives in the same instant and is exact. It becomes ``StreamAlive``, as do
  ``Begin``, ``Termination`` and anything else this connector does not
  recognise: the stream's liveness watchdog counts signals it was told about,
  and a session that is merely between turns is a session that is alive.

Word offsets are milliseconds from the first byte of audio *this connection*
received, which is the base the stream's capture clock mapping expects, so
nothing here converts to session time.

**A v3 session is capped and a table is not.** ``Begin`` states ``expires_at``,
the wall-clock instant the server will cut the session off at (three hours out
today), so this connector leaves shortly before it and at a turn boundary,
the way the Gemini connector leaves on ``goAway``: the stream above reconnects
and marks what the reconnect swallowed with a gap row, which costs seconds of
silence rather than the middle of somebody's sentence.

**One utterance per call** (``Connector``, ADR 0005) is kept for the callers
that still hand over a finished utterance: re-processing a stored recording,
the call-shaped fallback path, and a session whose model this vendor will not
stream. See :meth:`transcribe_one` for why that shape opens a socket per
utterance and this one does not.

Diarization is requested unconditionally (``speaker_labels=true``, a paid
add-on): each final word then carries a ``speaker``, and the router decides
whether to use those labels or its own diarizer. Labels are session-scoped, so
a persistent stream is what makes them consistent across turns at all, which
is the reason this vendor was worth migrating.

Docs:
- https://www.assemblyai.com/docs/speech-to-text/universal-streaming
- https://www.assemblyai.com/docs/streaming/api-spec/streaming-websocket
- https://www.assemblyai.com/docs/streaming/label-speakers-and-separate-channels
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import time
from collections.abc import AsyncIterator
from urllib.parse import urlencode

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosedOK

from loreline.audio.chunker import Utterance
from loreline.capabilities import surface_for
from loreline.capability_config import TranscribeCapabilities
from loreline.logging import get_logger
from loreline.models import Glossary, Interaction, ProviderConfig, ProviderKind, Word
from loreline.secrets import SecretStore
from loreline.stt.backends._assemblyai import glossary_for, parse_words
from loreline.stt.backends._ws import (
    as_dict,
    as_list,
    as_obj_dict,
    close_socket,
    get_bool,
    get_float,
    get_str,
    next_frame,
)
from loreline.stt.base import Connector, Transcription, glossary_terms, secret_for
from loreline.stt.registry import register
from loreline.stt.streaming import (
    StreamAlive,
    StreamingConnector,
    StreamSignal,
    StreamUnsupportedError,
    TurnFinal,
    TurnPartial,
    TurnSignal,
    TurnStarted,
)

log = get_logger(__name__)

# The v3 endpoint rejects any single audio message outside 50-1000 ms (close
# code 3007, verified live: "Input Duration Violation: 20.0 ms"), so both
# shapes re-block their audio before sending. The utterance shape aims high,
# since it has the whole utterance in hand and fewer messages is cheaper; the
# streaming shape aims at the floor, since every millisecond it holds back is a
# millisecond later the words appear.
_CHUNK_MS = 800
_MIN_CHUNK_MS = 50
_MAX_CHUNK_MS = 1000
# Safety net per received frame; the protocol's own Termination reply is the
# real end-of-flush signal, and partial-turn updates keep arriving every few
# seconds while the server is still transcribing, so a healthy session never
# goes quiet this long.
_RECV_TIMEOUT_S = 15.0
# How long the streaming shape waits for the Begin frame that confirms the
# session, or for the Error frame that refuses it.
_BEGIN_TIMEOUT_S = 10.0
# ...and how long it waits, after Terminate, for the server to finish saying
# what it still has to say. Measured against the real service: the last turn's
# final and the SpeakerRevision both landed within 1.0 s of Terminate and the
# Termination reply 1.3 s after it.
#
# This is one budget rather than two, which is the whole reason it is not
# larger. ``StreamConfig.final_wait_s`` documents how long Stop may take, and
# the stream spends it *after* flush_input returns, so a connector that waited
# three seconds of its own on top made a five second Stop an eight second one.
# It ends the moment ``Termination`` arrives, which is also the moment every
# turn has settled, so the stream's own window costs nothing behind it; this
# number is only the cap for a session that stops answering, and 1.5 s is
# still headroom on the 1.3 s that was measured.
_TERMINATE_TIMEOUT_S = 1.5
# How long before the session's own expiry to leave, at a turn boundary. v3
# caps a session (three hours today) and states the exact instant in ``Begin``,
# so a table that runs longer than that gets the ending chosen here rather than
# the server's, which lands mid-turn. Big enough for a turn to finish inside
# it, small enough that the reconnect it costs is once every three hours.
_SESSION_END_MARGIN_S = 60.0
# "User Input Validation Error": something in the query string the endpoint
# will not accept. Which thing decides what it means, and the message names it
# in single quotes: ``Invalid 'speech_model'`` is this model saying it will not
# stream (verified live against universal-2), which is the refusal
# StreamUnsupportedError is for, while ``Invalid 'language_codes'`` or a
# refused keyterms prompt is a nicety the session is better off without than
# ending over. Anything else is a plain failure and spends a reconnect.
_VALIDATION_ERROR_CODE = 3006
_MODEL_PARAM = "speech_model"
# Query parameters worth dropping to keep a session. `sample_rate`, `encoding`
# and `speech_model` are absent on purpose: a session without them is not the
# session that was asked for.
_OPTIONAL_PARAMS = frozenset(
    {"language_codes", "keyterms_prompt", "speaker_labels", "continuous_partials", "format_turns"}
)
_PARAM_PATTERN = re.compile(r"'([a-z_]+)'")
# The only streaming model the vendor documents `language_codes` for. It is
# also the endpoint's own default speech_model, so a config naming no model
# still gets it; universal-streaming-english (English only) and
# universal-streaming-multilingual (auto code-switches its own six languages)
# document no language parameter at all, so nothing is sent for them rather
# than risking a validation error for a key that model does not accept.
# https://www.assemblyai.com/docs/api-reference/streaming-api/streaming-api
_LANGUAGE_CODES_MODEL = "universal-3-5-pro"


class AssemblyAIBackend(Connector[str], StreamingConnector):
    """AssemblyAI v3 transcription with inline diarization, both shapes.

    See the module docstring for what each shape does. The prepared value of
    the utterance shape is the socket URL with its query string built; the
    streaming shape builds its own, because a live session asks for two things
    a bounded utterance has no use for (see :meth:`_params`).
    """

    def __init__(
        self,
        config: ProviderConfig,
        *,
        model: str | None = None,
        caps: TranscribeCapabilities | None = None,
        api_key: str | None = None,
    ) -> None:
        super().__init__(config)
        self._api_key = api_key
        self._model = model
        self._caps = caps
        self._language = config.language
        self._endpoint = surface_for(config, Interaction.TRANSCRIBE, "realtime")
        self._url = self._endpoint.url
        self._stream_ws: ClientConnection | None = None
        # Audio held back only until it reaches the endpoint's 50 ms floor.
        self._pending = b""
        # The settled text of each turn this connection has finalized, by
        # turn_order. Kept because SpeakerRevision re-states a turn's words and
        # not its transcript, so republishing a corrected turn needs the text
        # from when it closed. Reset per connection, along with turn_order.
        self._finals: dict[str, str] = {}
        # Set once the server has said everything it is going to say on this
        # connection; see :meth:`flush_input`, which is the only thing waiting.
        self._terminated = asyncio.Event()
        # Whether a turn is open right now, which is what the session-cap exit
        # waits for. Per connection, like everything else about turns here.
        self._turn_open = False
        # When this connection's own reader should leave, on the monotonic
        # clock, from the ``expires_at`` the server states in ``Begin``. None
        # until a session says so; see :meth:`signals`.
        self._leave_by: float | None = None
        # Query parameters the endpoint refused, left out of every request this
        # backend makes from then on. Kept across connections on purpose, the
        # way OpenAI's `_prompt_rejected` is: a parameter this model will not
        # take is one it will not take on the next socket either. Shared with
        # `prepare`, so the utterance path stops sending it too - that shape
        # used to be refused for exactly the parameter the streaming shape had
        # already been refused for.
        self._dropped: set[str] = set()

    def _params(self, glossary: Glossary | None, *, streaming: bool) -> list[tuple[str, str]]:
        params: list[tuple[str, str]] = [
            ("sample_rate", str(self.config.sample_rate)),
            ("encoding", "pcm_s16le"),
            ("format_turns", "true"),
        ]
        # `language_codes` is a JSON array in the query string (for example
        # ?language_codes=["de"]), not the plain `language=de` this connector
        # used to send: v3 has no `language` parameter, so that value was
        # silently dropped and the vendor picked up nothing. An empty
        # `self._language` means no language configured, which gets the same
        # native code-switching as omitting the field outright.
        if self._language and self._model in (None, _LANGUAGE_CODES_MODEL):
            params.append(("language_codes", json.dumps([self._language])))
        # Sent only when the GM picked one: omitted, the endpoint applies its
        # own current default (universal-3-5-pro), which is a better thing to
        # inherit than a value pinned here. Until this was wired the model
        # picker had no effect at all on what AssemblyAI ran.
        if self._model:
            params.append(("speech_model", self._model))
        # Requested unconditionally, matching the Deepgram connector: the
        # backend always asks for speakers and the router decides whether to
        # use them (see stt/router.py's DiarizationMode.INLINE branch), so the
        # words already carry labels whichever mode the session ends up in.
        # Note AssemblyAI bills streaming diarization as a paid add-on.
        params.append(("speaker_labels", "true"))
        terms = glossary_for(self._caps, glossary_terms(glossary), realtime=True)
        if terms:
            params.append(("keyterms_prompt", json.dumps(terms)))
        if streaming:
            # Partials every few seconds *during* a turn rather than only as
            # new words settle. This is the whole user-visible win of the
            # streaming shape, and the parameter defaults off when speaker
            # labels are on, so it is asked for rather than assumed.
            params.append(("continuous_partials", "true"))
        # Endpointing is deliberately left at the vendor's defaults
        # (end_of_turn_confidence_threshold, min_turn_silence, max_turn_silence):
        # the point of the streaming shape is that AssemblyAI decides the turns
        # from audio it can hear, and a number invented here would be this app
        # guessing at that from the outside.
        return [pair for pair in params if pair[0] not in self._dropped]

    def prepare(self, glossary: Glossary | None) -> str:
        return f"{self._url}?{urlencode(self._params(glossary, streaming=False))}"

    @property
    def _headers(self) -> dict[str, str]:
        return self._endpoint.request_headers(self._api_key)

    async def transcribe_one(self, utterance: Utterance, prepared: str) -> Transcription:
        # One session per utterance, closed with Terminate. Not because the
        # protocol forces it - the streaming shape below keeps one session for
        # a whole capture - but because a *caller-bounded* utterance has to be
        # flushed, and Terminate is the only flush v3 defines that also tells
        # you it is done (it answers with Termination). ForceEndpoint ends the
        # open turn without ending the session, but leaves no way to tell
        # "flush done" from "still transcribing": real update gaps run 2-3 s,
        # so any quiet-gap heuristic either drops utterance tails or stalls
        # every utterance. A stream with no utterance in it has nothing to
        # flush per utterance, which is why the other shape needs none of this.
        # With format_turns the server may resend a turn it already ended as
        # a formatted duplicate, so keep the last message per turn_order.
        turns: dict[int, tuple[str, list[Word]]] = {}
        async with connect(prepared, additional_headers=self._headers) as ws:
            for chunk in _audio_chunks(utterance.pcm, self.config.sample_rate):
                await ws.send(chunk)
            await ws.send(json.dumps({"type": "Terminate"}))
            while True:
                try:
                    async with asyncio.timeout(_RECV_TIMEOUT_S):
                        raw = await ws.recv()
                except (TimeoutError, ConnectionClosedOK):
                    break
                message = as_dict(raw)
                kind = get_str(message, "type")
                if kind == "Turn" and get_bool(message, "end_of_turn"):
                    turns[int(get_float(message, "turn_order"))] = (
                        get_str(message, "transcript"),
                        parse_words(message.get("words"), offset=utterance.start),
                    )
                elif kind == "Termination":
                    break
        ordered = [turns[order] for order in sorted(turns)]
        return Transcription(
            text=" ".join(text for text, _ in ordered if text),
            words=[word for _, turn_words in ordered for word in turn_words],
        )

    # -- the streaming shape ----------------------------------------------

    @property
    def stream_rate(self) -> int:
        """The capture rate, unchanged: v3 takes 8-96 kHz and is told which."""
        return self.config.sample_rate

    async def open_stream(self, glossary: Glossary | None) -> None:
        """Open one v3 session for a whole capture and read its Begin frame.

        Reading Begin here rather than in :meth:`signals` is what makes a
        refused session a refusal instead of a socket that accepts audio
        forever and answers nothing. The endpoint accepts the upgrade first and
        validates the query string after, so its "no" arrives as a frame:
        ``{"type": "Error", "error_code": 3006, "error": "Invalid
        'speech_model': ..."}``. Which parameter that message names is what
        decides between the three answers:

        * ``speech_model`` is this model saying it will not stream, which no
          retry changes, so :class:`StreamUnsupportedError` and the session
          runs this same provider one utterance at a time;
        * a parameter this connector can go without - the language, the
          keyterms prompt, speaker labels, continuous partials - is dropped
          for good and the session opened again at once, because losing a
          nicety is worth a session and losing the session over one is not;
        * anything else is a plain failure and spends one of the stream's
          reconnects.

        Begin also echoes the configuration the server actually applied, which
        is the only way to notice that speaker labels were dropped: unknown
        query parameters are ignored silently, and diarization is a paid
        add-on, so an account without it would otherwise just quietly return
        words with no speakers. And it states ``expires_at``, the instant this
        session is cut off, which :meth:`signals` leaves before rather than at.
        """
        await self.close_stream()  # a reconnect must not leak the dead socket
        self._terminated = asyncio.Event()
        # Twice at most: the second attempt is the one that follows dropping a
        # parameter, and a second refusal is a refusal.
        for attempt in (1, 2):
            ws = await connect(
                f"{self._url}?{urlencode(self._params(glossary, streaming=True))}",
                additional_headers=self._headers,
            )
            try:
                await self._read_begin(ws)
            except BaseException as exc:
                await close_socket(ws)
                if attempt == 1 and isinstance(exc, _RefusedParameterError) and self._drop(exc):
                    continue
                raise
            self._stream_ws = ws
            return

    def _drop(self, refused: _RefusedParameterError) -> bool:
        """Stop sending the parameter a refusal named, if it can be gone without.

        True when something was dropped and the session is worth opening again.
        """
        param = refused.param
        if param not in _OPTIONAL_PARAMS or param in self._dropped:
            return False
        self._dropped.add(param)
        log.warning(
            "assemblyai.stream.parameter_dropped",
            provider=self.config.id,
            parameter=param,
            model=self._model,
            detail=str(refused),
        )
        return True

    async def _read_begin(self, ws: ClientConnection) -> None:
        """Wait for the session confirmation, or for the refusal instead."""
        async with asyncio.timeout(_BEGIN_TIMEOUT_S):
            raw = await ws.recv()
        message = as_dict(raw)
        if get_str(message, "type").lower() == "error":
            raise _refusal(message, self._model)
        self._leave_by = _leave_by(get_float(message, "expires_at"))
        applied = as_obj_dict(message.get("configuration"))
        if applied and not get_bool(applied, "speaker_labels"):
            log.warning(
                "assemblyai.stream.no_speaker_labels",
                provider=self.config.id,
                model=get_str(applied, "model"),
            )

    async def send_audio(self, pcm: bytes) -> None:
        """Write PCM, held only until it reaches the endpoint's 50 ms floor.

        The one place a connector is allowed to buffer, and only because the
        vendor closes the socket on anything shorter (code 3007). Capture hands
        over 20 ms frames, so this sends at 60 ms and adds at most 40 ms; the
        alternative is not lower latency, it is a dead connection.
        """
        ws = self._stream_ws
        if ws is None:
            msg = "send_audio before open_stream"
            raise RuntimeError(msg)
        self._pending += pcm
        while len(self._pending) >= self._min_bytes:
            block = self._pending[: self._max_bytes]
            self._pending = self._pending[self._max_bytes :]
            await ws.send(block)

    async def signals(self) -> AsyncIterator[StreamSignal]:
        """Translate this session's messages into signals, until it ends.

        Every offset yielded is the vendor's own count of milliseconds into the
        audio this connection received, as seconds; the stream maps them onto
        the capture clock. A turn's span comes from its own words rather than
        from ``SpeechStarted``, which carries a timestamp but no ``turn_order``
        to attach it to.

        Three things end the iteration, and all three mean this connection is
        over rather than this session. An ``Error`` frame, because a session
        that rejected something will not start working on the next frame.
        ``Termination``, because it is the server's own "that was everything"
        and the stream should not spend its final wait on a socket that has
        finished. And the session's own ``expires_at``: v3 caps a session and a
        table runs for hours, so this leaves at the next turn boundary shortly
        before the cap rather than being cut off inside a turn, exactly as the
        Gemini connector leaves on ``goAway``. The stream above reads any of
        them as a lost connection, reconnects, and marks what the reconnect
        swallowed with a gap row.

        Everything that says nothing about a turn - ``Begin``,
        ``SpeechStarted``, ``Termination``, heartbeats, anything unrecognised -
        is yielded as :class:`StreamAlive`. The liveness watchdog counts
        signals it was told about, so consuming an ack silently left a merely
        quiet connection looking dead.
        """
        ws = self._stream_ws
        if ws is None:
            return
        try:
            async for raw in self._frames(aiter(ws)):
                if isinstance(raw, bytes):
                    continue  # v3 speaks JSON downstream; binary would be its own bug
                signals, carry_on = self._message_signals(as_dict(raw))
                for signal in signals:
                    yield signal
                if not carry_on:
                    return
        finally:
            # However this ended - Termination, an error, a dead socket, the
            # stream cancelling the reader - nothing more is coming, and
            # flush_input must not sit out its timeout waiting for it.
            self._terminated.set()

    async def _frames(self, frames: AsyncIterator[str | bytes]) -> AsyncIterator[str | bytes]:
        """This connection's frames, ending where the session's own cap does.

        The deadline only bounds a read taken *between* turns: a table that has
        gone quiet produces no frames at all, so a cap checked only as frames
        arrive is a cap the server reaches first, and a turn in progress is
        worth the few seconds it takes to finish.
        """
        while True:
            deadline = None if self._turn_open else self._leave_by
            try:
                raw = await next_frame(frames, deadline=deadline)
            except StopAsyncIteration:
                return
            if raw is None:
                log.info("assemblyai.stream.session_expiring", provider=self.config.id)
                return
            yield raw

    def _message_signals(self, message: dict[str, object]) -> tuple[list[StreamSignal], bool]:
        """One message as signals, and whether this connection has more to say."""
        kind = get_str(message, "type")
        if kind == "Turn":
            self._turn_open = not get_bool(message, "end_of_turn")
            return list(self._turn_signals(message)), True
        if kind == "SpeakerRevision":
            return list(self._revision_signals(message)), True
        if kind.lower() == "error":
            log.warning(
                "assemblyai.stream.error",
                provider=self.config.id,
                error_code=get_float(message, "error_code"),
                detail=get_str(message, "error") or get_str(message, "message"),
            )
            return [], False
        if kind == "SpeechStarted":
            self._turn_open = True
        # Termination is the server's own "that was everything", so the socket
        # is still alive and there is nothing left on it.
        return [StreamAlive()], kind != "Termination"

    def _turn_signals(self, message: dict[str, object]) -> list[TurnSignal]:
        """One ``Turn`` message: an interim while it is open, a final when not."""
        ref = _ref(message)
        text = get_str(message, "transcript")
        # offset=0.0 keeps the vendor's own base, which is what the stream shifts.
        words = parse_words(message.get("words"), offset=0.0)
        start = words[0].start if words else None
        if not get_bool(message, "end_of_turn"):
            # TurnStarted before every partial, not only the first: opening a
            # turn is idempotent in the stream, so this states the turn's real
            # start without this connector tracking which turns it has seen.
            return [TurnStarted(at=start, ref=ref), TurnPartial(text=text, ref=ref, append=False)]
        self._finals[ref] = text
        return [
            TurnFinal(
                text=text,
                ref=ref,
                words=tuple(words),
                at=start,
                to=words[-1].end if words else None,
            )
        ]

    def _revision_signals(self, message: dict[str, object]) -> list[TurnSignal]:
        """AssemblyAI's end-of-session speaker pass, as finals to replace with.

        A revision names a turn and re-states its words; the text is the one
        this connection already published for that turn. A revision for a turn
        that has not been finalized yet is dropped, because the final still to
        come already carries the corrected labels (verified against the real
        service: the last turn's revision and its final agreed word for word).
        """
        signals: list[TurnSignal] = []
        for entry in as_list(message.get("revisions")):
            revision = as_obj_dict(entry)
            ref = _ref(revision)
            text = self._finals.get(ref)
            words = parse_words(revision.get("words"), offset=0.0)
            if not text or not words:
                continue
            signals.append(
                TurnFinal(
                    text=text,
                    ref=ref,
                    words=tuple(words),
                    at=words[0].start,
                    to=words[-1].end,
                )
            )
        return signals

    async def flush_input(self) -> None:
        """Send the audio tail, then ``Terminate``.

        ``Terminate`` rather than ``ForceEndpoint``, although both end the open
        turn, because this is called exactly once and only when the microphone
        has stopped: the session is ending anyway, and ``Terminate`` is the one
        that also makes the server run its final speaker pass and send the
        ``SpeakerRevision`` this connector turns into corrected finals.

        The tail is whatever sat below the 50 ms floor, padded with silence
        rather than dropped, so the last word keeps its ending. Then this waits
        for the session's own ``Termination``, bounded, because what the server
        sends between the two is the point of sending it - and bounded
        *short*, because the stream spends ``StreamConfig.final_wait_s`` after
        this returns and the two are meant to be one budget rather than two.
        See ``_TERMINATE_TIMEOUT_S``.
        """
        ws = self._stream_ws
        if ws is None:
            return
        if self._pending:
            padding = b"\x00" * max(0, self._min_bytes - len(self._pending))
            await ws.send(self._pending + padding)
            self._pending = b""
        await ws.send(json.dumps({"type": "Terminate"}))
        # Wait for the server's own ending. Everything AssemblyAI still owes
        # this session arrives between Terminate and Termination - the open
        # turn's final, then the speaker pass - and the caller stops waiting as
        # soon as the last turn settles, which without this would cancel the
        # reader in the moment the revision is on the wire.
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(_TERMINATE_TIMEOUT_S):
                await self._terminated.wait()

    async def close_stream(self) -> None:
        """Drop the socket, bounded, and forget everything it was carrying.

        The books cleared here are the whole of what one connection knew: the
        audio tail below the 50 ms floor, the settled text a SpeakerRevision
        would have republished, which turn was open and when the session was
        due to expire. All of it is stated per connection by the server, so a
        reconnect that inherited any of it would be reading the dead session's
        answers into the new one.
        """
        ws, self._stream_ws = self._stream_ws, None
        self._pending = b""
        self._finals = {}
        self._turn_open = False
        self._leave_by = None
        if ws is not None:
            await close_socket(ws)

    async def aclose(self) -> None:
        await self.close_stream()

    @property
    def _min_bytes(self) -> int:
        return self.config.sample_rate * 2 * _MIN_CHUNK_MS // 1000

    @property
    def _max_bytes(self) -> int:
        return self.config.sample_rate * 2 * _MAX_CHUNK_MS // 1000


class _RefusedParameterError(RuntimeError):
    """v3 refused one named parameter, and it is not the model.

    Internal: :meth:`AssemblyAIBackend.open_stream` reads it as "drop this and
    try once more" where the parameter is one this connector can go without,
    and lets it out as the plain ``RuntimeError`` it is otherwise, which spends
    one of the stream's reconnects.
    """

    def __init__(self, param: str, detail: str) -> None:
        super().__init__(f"AssemblyAI refused the session: {detail}")
        self.param = param


def _refusal(message: dict[str, object], model: str | None) -> Exception:
    """What an ``Error`` frame in place of ``Begin`` means. Always an exception.

    3006 is "User Input Validation Error", which the connector used to read as
    one answer: this model cannot be streamed. It is not one answer. The
    message names the offending parameter in single quotes, and only
    ``speech_model`` is about the model; a refused ``language_codes`` or
    keyterms prompt is a nicety, and taking a whole session off the streaming
    path for one - and off the utterance path too, since both shapes build one
    query string - is the failure this splits apart.
    """
    detail = get_str(message, "error") or get_str(message, "message")
    if int(get_float(message, "error_code")) != _VALIDATION_ERROR_CODE:
        return RuntimeError(f"AssemblyAI refused the session: {detail}")
    match = _PARAM_PATTERN.search(detail)
    param = match.group(1) if match else ""
    if param == _MODEL_PARAM:
        return StreamUnsupportedError(
            f"{model or 'this model'} cannot be streamed as configured: {detail}"
        )
    return _RefusedParameterError(param, detail)


def _leave_by(expires_at: float) -> float | None:
    """When to leave this session, on the monotonic clock, or None.

    ``expires_at`` is the wall-clock second v3 will cut the session off at, and
    a session cap that is hours away is still a cap a table reaches. None for a
    session that states none, or one already past its own expiry, which is the
    server's to end rather than something to leave over on connect.
    """
    if expires_at <= 0.0:
        return None
    remaining = expires_at - time.time() - _SESSION_END_MARGIN_S
    return time.monotonic() + remaining if remaining > 0.0 else None


def _ref(message: dict[str, object]) -> str:
    """A turn's handle: ``turn_order`` as a string, the stream's ``ref``."""
    return str(int(get_float(message, "turn_order")))


def _audio_chunks(pcm: bytes, sample_rate: int) -> list[bytes]:
    """Split s16le PCM into messages the endpoint accepts (50-1000 ms each)."""
    bytes_per_ms = sample_rate * 2 // 1000
    min_len = bytes_per_ms * _MIN_CHUNK_MS
    if len(pcm) < min_len:  # pad ultra-short utterances up to the server minimum
        pcm += b"\x00" * (min_len - len(pcm))
    step = bytes_per_ms * _CHUNK_MS
    chunks: list[bytes] = []
    pos = 0
    while pos < len(pcm):
        end = pos + step
        if len(pcm) - end < min_len:  # fold a sub-minimum tail into the last chunk
            end = len(pcm)
        chunks.append(pcm[pos:end])
        pos = end
    return chunks


@register(ProviderKind.ASSEMBLYAI, realtime=True)
def _factory(  # pyright: ignore[reportUnusedFunction]
    config: ProviderConfig,
    secrets: SecretStore,
    model: str | None,
    caps: TranscribeCapabilities | None,
) -> AssemblyAIBackend:
    return AssemblyAIBackend(config, model=model, caps=caps, api_key=secret_for(config, secrets))
