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
* ``SpeechStarted`` is consumed and translated into nothing. It carries a
  timestamp but no ``turn_order``, so there is no turn to attach it to; the
  turn's start comes from the first word offset of its own first ``Turn``
  message, which arrives in the same instant and is exact.

Word offsets are milliseconds from the first byte of audio *this connection*
received, which is the base the stream's capture clock mapping expects, so
nothing here converts to session time.

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
# Termination reply 1.3 s after it, so this is threefold headroom on a wait
# that only ever happens once, as the session ends.
_TERMINATE_TIMEOUT_S = 3.0
# "User Input Validation Error": a parameter the endpoint will not accept.
# Naming a model it does not stream is one (verified: universal-2 answers
# "Invalid 'speech_model'"), which is the refusal StreamUnsupportedError is for.
_VALIDATION_ERROR_CODE = 3006
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
        return params

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
        'speech_model': ..."}``. That is a fact about the model or the
        parameters, which no retry changes, so it raises
        :class:`StreamUnsupportedError` and the session runs this same provider
        one utterance at a time.

        Begin also echoes the configuration the server actually applied, which
        is the only way to notice that speaker labels were dropped: unknown
        query parameters are ignored silently, and diarization is a paid
        add-on, so an account without it would otherwise just quietly return
        words with no speakers.
        """
        await self.close_stream()  # a reconnect must not leak the dead socket
        self._terminated = asyncio.Event()
        ws = await connect(
            f"{self._url}?{urlencode(self._params(glossary, streaming=True))}",
            additional_headers=self._headers,
        )
        try:
            await self._read_begin(ws)
        except BaseException:
            with contextlib.suppress(Exception):
                await ws.close()
            raise
        self._stream_ws = ws

    async def _read_begin(self, ws: ClientConnection) -> None:
        """Wait for the session confirmation, or for the refusal instead."""
        async with asyncio.timeout(_BEGIN_TIMEOUT_S):
            raw = await ws.recv()
        message = as_dict(raw)
        if get_str(message, "type").lower() == "error":
            detail = get_str(message, "error") or get_str(message, "message")
            if int(get_float(message, "error_code")) == _VALIDATION_ERROR_CODE:
                raise StreamUnsupportedError(
                    f"{self._model or 'this model'} cannot be streamed as configured: {detail}"
                )
            msg = f"AssemblyAI refused the session: {detail}"
            raise RuntimeError(msg)
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

    async def signals(self) -> AsyncIterator[TurnSignal]:
        """Translate this session's messages into turn signals.

        Every offset yielded is the vendor's own count of milliseconds into the
        audio this connection received, as seconds; the stream maps them onto
        the capture clock. A turn's span comes from its own words rather than
        from ``SpeechStarted``, which carries a timestamp but no ``turn_order``
        to attach it to.

        An ``Error`` frame ends the iteration rather than being skipped: a
        session that rejected something will not start working on the next
        frame, and ending here is what lets the stream reconnect or fail over
        instead of streaming into silence. Everything else (``Begin``,
        ``SpeechStarted``, ``Termination``, heartbeats) is consumed and
        translated into nothing, which still feeds the liveness watchdog.
        """
        ws = self._stream_ws
        if ws is None:
            return
        try:
            async for raw in ws:
                if isinstance(raw, bytes):
                    continue  # v3 speaks JSON downstream; binary would be its own bug
                message = as_dict(raw)
                kind = get_str(message, "type")
                if kind == "Turn":
                    for signal in self._turn_signals(message):
                        yield signal
                elif kind == "SpeakerRevision":
                    for signal in self._revision_signals(message):
                        yield signal
                elif kind == "Termination":
                    self._terminated.set()
                elif kind.lower() == "error":
                    log.warning(
                        "assemblyai.stream.error",
                        provider=self.config.id,
                        error_code=get_float(message, "error_code"),
                        detail=get_str(message, "error") or get_str(message, "message"),
                    )
                    return
        finally:
            # However this ended - Termination, an error, a dead socket, the
            # stream cancelling the reader - nothing more is coming, and
            # flush_input must not sit out its timeout waiting for it.
            self._terminated.set()

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
        sends between the two is the point of sending it.
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
        ws, self._stream_ws = self._stream_ws, None
        self._pending = b""
        self._finals = {}
        if ws is not None:
            with contextlib.suppress(Exception):
                await ws.close()

    async def aclose(self) -> None:
        await self.close_stream()

    @property
    def _min_bytes(self) -> int:
        return self.config.sample_rate * 2 * _MIN_CHUNK_MS // 1000

    @property
    def _max_bytes(self) -> int:
        return self.config.sample_rate * 2 * _MAX_CHUNK_MS // 1000


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
