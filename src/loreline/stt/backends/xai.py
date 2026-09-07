"""xAI live STT connector (WebSocket), in both connector shapes.

xAI's live transcription endpoint is one socket configured entirely by query
string: no setup frame, raw PCM in as binary frames, JSON events out. This
connector speaks it twice, because the same endpoint serves both of ADR 0006's
shapes and what separates them is two query parameters and when the socket is
closed.

**Streaming** (``StreamingConnector``, what a live capture takes) is what the
endpoint was built for, and it needs no vendor feature this connector has to
ask permission for: continuous audio with the vendor's own endpointing is the
documented default behaviour, so nothing here raises ``StreamUnsupportedError``.
``interim_results=true`` turns on partials roughly every 500 ms and
``endpointing`` sets the silence that closes an utterance:

    (connect)                  -> {"type": "transcript.created"}
    <raw PCM frames, for the life of the session>
                               -> transcript.partial is_final=false
                                  an interim; the text may still change
                               -> transcript.partial is_final=true
                                  speech_final=false: about three seconds of
                                  speech locked, the turn is still open
                               -> transcript.partial is_final=true
                                  speech_final=true: the speaker stopped, and
                                  this is the turn boundary
    {"type": "finalize"}       -> closes the open utterance as speech_final,
                                  on a socket that stays open
    {"type": "audio.done"}     -> {"type": "transcript.done"} and a hangup

So ``speech_final`` is the turn, ``finalize`` is :meth:`flush_input` and
``audio.done`` is :meth:`close_stream`. ``transcript.done`` is deliberately not
published: it carries the whole stream's transcript, which on a persistent
connection is the entire session and would repeat every turn already published.

Two facts about the events shape the translation. Word ``start``/``end`` are
"measured from the beginning of the audio stream", which is exactly the offset
base ``TranscriptStream`` maps onto the capture clock, so they are forwarded
unshifted. And ``text`` is *cumulative*, never a delta, so every partial goes
out with ``append=False``.

Cumulative over what, though, is the one thing the documentation says two ways:
"complete transcript accumulated since the stream began" in the API reference,
"complete stitched utterance" in the guide's ``speech_final`` row. The
difference only shows from the second turn on, and it is the difference between
a clean transcript and one that repeats the whole session in every row, so this
connector is written to be right either way: whatever was already published as
a final is stripped from the front of the next event's text, and words that end
before the last settled turn did are dropped. Under the per-utterance reading
both are no-ops, because a fresh utterance neither repeats the last one's text
nor carries its words. The one case that reading costs is a turn whose text is
a verbatim extension of the turn before it ("Okay." then "Okay. I attack."),
which loses the repeated opening; the alternative loses the session.

**One utterance per call** (``Connector``, ADR 0005) is kept for reprocessing,
which replays a stored file, and for the call-shaped fallback path. There
``interim_results`` stays off, one connection carries one utterance, and
``audio.done`` both flushes it and ends the socket. See :meth:`transcribe_one`
for why that connection is not shared.

UNVERIFIED: written from xAI's documentation and exercised only against
``mocks/xai_ws.py``, never against api.x.ai, because this environment has no
xAI key. The streaming shape adds two things a maintainer with one should check
first: which of the two readings of a cumulative ``text`` is the real one, and
that a ``finalize`` really answers with a ``speech_final`` partial rather than
closing the socket. See also the note above the grok-stt-1.0 entry in
capabilities.yaml.

Docs: https://docs.x.ai/developers/model-capabilities/audio/speech-to-text
      https://docs.x.ai/developers/rest-api-reference/inference/voice
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
from loreline.stt.backends._ws import as_dict, get_bool, get_float, get_str
from loreline.stt.backends._xai import parse_words, stt_params
from loreline.stt.base import Connector, Transcription, glossary_terms, secret_for
from loreline.stt.registry import register
from loreline.stt.streaming import (
    StreamingConnector,
    TurnFinal,
    TurnPartial,
    TurnSignal,
    TurnStarted,
)

log = get_logger(__name__)

# Safety net per received frame on the utterance shape. audio.done ->
# transcript.done is the real end-of-flush signal and the service closes
# straight after it; this only bounds a socket that goes quiet without either.
# The streaming shape needs no counterpart: TranscriptStream's own watchdog is
# what bounds a connection that stops answering there.
_RECV_TIMEOUT_S = 10.0

# What the capture pipeline sends: 16-bit little-endian PCM, one channel. The
# socket has no container to read this from, unlike the batch endpoint, so the
# query string has to spell it out.
_ENCODING = "pcm"

# Silence that closes a turn, in milliseconds, on the streaming shape. This is
# the vendor's own default, restated so that changing it is a change with a
# reason rather than a silent inheritance - and it is the number ADR 0006 is
# about: 400 ms against the local chunker's 800 ms, before the round trip the
# utterance path pays on top. `smart_turn`, the vendor's model-based end-of-turn
# detector, is left off: it is a confidence threshold with no documented
# default, so picking one without a key to measure against would be a guess
# dressed as a setting.
_ENDPOINTING_MS = 400

# How long open_stream waits for the transcript.created greeting. The docs say
# to wait for it before sending audio, and waiting here rather than in the
# reader keeps a refused socket out of the signal loop, where it would look
# like a connection that simply never said anything.
_READY_TIMEOUT_S = 10.0

# How long close_stream will spend on the parting audio.done. It is a courtesy
# to the vendor, not something any transcript depends on, so a socket that has
# already gone away must not delay the session's ending for it.
_CLOSE_TIMEOUT_S = 2.0

# A word this far inside an already-settled turn belongs to that turn. Only
# guards float noise in the vendor's own offsets; a real gap between turns is
# orders of magnitude larger.
_WORD_EPSILON_S = 0.001


class XaiBackend(Connector[str], StreamingConnector):
    """Live transcription with inline diarization via xAI, in both shapes.

    See the module docstring for what each shape does. They never share a
    socket: the utterance shape opens one per call and ends it with
    ``audio.done``, while the streaming shape holds one open for the whole
    capture, and the two want different query strings anyway.

    The prepared value of the utterance shape is the socket URL with its query
    string built. The streaming shape builds its own in :meth:`open_stream`,
    from the same parameters plus the two that make the endpoint continuous.
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
        # Kept for the log line only. This endpoint takes no model parameter,
        # so nothing downstream sends it; see _xai.py.
        self._model = model
        self._caps = caps
        self._endpoint = surface_for(config, Interaction.TRANSCRIBE, "realtime")
        self._url = self._endpoint.url
        self._stream_ws: ClientConnection | None = None
        # Connection-scoped, reset by open_stream: what the cumulative text and
        # words of the next event are read against. See the module docstring.
        self._settled_text = ""
        self._settled_until = 0.0

    def prepare(self, glossary: Glossary | None) -> str:
        """The socket URL for one utterance, glossary and all."""
        return self._socket_url(glossary, streaming=False)

    def _socket_url(self, glossary: Glossary | None, *, streaming: bool) -> str:
        """The whole configuration of a connection, as a query string.

        Streaming-only: the batch endpoint reads the audio format from the
        container it is posted, while a raw PCM socket has nothing to read it
        from. ``interim_results`` and ``endpointing`` are what make a connection
        continuous, so they are added for the streaming shape and only there:
        the utterance shape wants one locked answer per call, and half-formed
        text on a socket that carries a single utterance is frames spent on
        something no caller can publish.
        """
        params = stt_params(
            caps=self._caps,
            language=self._language,
            terms=glossary_terms(glossary),
            realtime=True,
        )
        params.extend(
            [
                ("encoding", _ENCODING),
                ("sample_rate", str(self.config.sample_rate)),
                ("channels", "1"),
            ]
        )
        if streaming:
            params.extend(
                [
                    ("interim_results", "true"),
                    ("endpointing", str(_ENDPOINTING_MS)),
                ]
            )
        return f"{self._url}?{urlencode(params)}"

    @property
    def _headers(self) -> dict[str, str]:
        return self._endpoint.request_headers(self._api_key)

    async def transcribe_one(self, utterance: Utterance, prepared: str) -> Transcription:
        # One connection per utterance, closed with audio.done: that is the
        # documented flush signal, and the service answers it with
        # transcript.done and hangs up. `finalize` is the other control
        # message, and on this shape it would be the wrong one - it closes the
        # current utterance on a socket that stays open, so a shared connection
        # would let a late frame land in the next caller's read window. The
        # streaming shape has no next window, which is exactly why it is the
        # one that uses `finalize`; see flush_input.
        text = ""
        words: list[Word] = []
        # The fallback described in the module docstring: locked partials, kept
        # only in case transcript.done arrives empty.
        partial_parts: list[str] = []
        partial_words: list[Word] = []
        async with connect(prepared, additional_headers=self._headers) as ws:
            await ws.send(utterance.pcm)
            await ws.send(json.dumps({"type": "audio.done"}))
            while True:
                try:
                    async with asyncio.timeout(_RECV_TIMEOUT_S):
                        raw = await ws.recv()
                except (TimeoutError, ConnectionClosedOK):
                    break
                message = as_dict(raw)
                kind = get_str(message, "type")
                if kind == "transcript.done":
                    text = get_str(message, "text")
                    words = parse_words(message, offset=utterance.start)
                    break
                if kind == "transcript.partial" and get_bool(message, "is_final"):
                    part = get_str(message, "text")
                    if part:
                        partial_parts.append(part)
                    partial_words.extend(parse_words(message, offset=utterance.start))
                elif kind == "error":
                    # The vendor's own words. Not raised: an utterance that
                    # produced no transcript is a None the router already
                    # handles, and failing the whole session over one rejected
                    # utterance is the harsher answer.
                    log.warning(
                        "stt.xai.error_frame",
                        provider=self.config.name,
                        provider_id=self.config.id,
                        detail=get_str(message, "message") or "no detail",
                    )
                    break
        if text:
            return Transcription(text=text, words=words)
        return Transcription(text=" ".join(partial_parts), words=partial_words)

    # -- the streaming shape ---------------------------------------------

    @property
    def stream_rate(self) -> int:
        """The rate the query string declares, so the two cannot disagree.

        Nothing negotiates this: the socket believes whatever ``sample_rate``
        says, so a stream resampled to one rate and declared at another is
        audio the vendor plays back at the wrong speed rather than an error.
        """
        return self.config.sample_rate

    async def open_stream(self, glossary: Glossary | None) -> None:
        """Open one continuous transcription socket and wait until it is ready.

        The greeting matters: the docs say to wait for ``transcript.created``
        before sending audio, so a connection is not "open" until it arrives.
        An ``error`` instead is a plain failure, which is a reconnect against
        the stream's budget rather than a :class:`StreamUnsupportedError` -
        every model this vendor has reaches one engine over one socket, so
        there is no per-model refusal to discover here.
        """
        await self.close_stream()  # a reconnect must not leak the dead socket
        url = self._socket_url(glossary, streaming=True)
        ws = await connect(url, additional_headers=self._headers)
        try:
            await _await_ready(ws)
        except BaseException:
            with contextlib.suppress(Exception):
                await ws.close()
            raise
        self._stream_ws = ws
        self._settled_text = ""
        self._settled_until = 0.0

    async def send_audio(self, pcm: bytes) -> None:
        """Write PCM as one raw binary frame: no base64, no JSON envelope."""
        ws = self._stream_ws
        if ws is None:
            msg = "send_audio before open_stream"
            raise RuntimeError(msg)
        await ws.send(pcm)

    async def signals(self) -> AsyncIterator[TurnSignal]:
        """Translate this connection's events into turn signals.

        ``transcript.partial`` is the only event that carries text, and which
        of the three it is comes from two booleans: an interim, a locked chunk
        of an open turn, or the turn's end (``speech_final``). The first two are
        interims to the stream and the third closes the turn.

        The other two endings are endings. ``transcript.done`` only follows an
        ``audio.done``, which this shape sends when it is already closing, so
        seeing one means the vendor is about to hang up; its text is the whole
        stream's and publishing it would repeat every turn of the session.
        An ``error`` is not going to start working on the next frame, so it
        stops the iteration too, which is what lets the stream reconnect or
        fail over rather than write into a socket that is answering nothing.
        """
        ws = self._stream_ws
        if ws is None:
            return
        async for raw in ws:
            message = as_dict(raw)
            kind = get_str(message, "type")
            if kind == "transcript.partial":
                for signal in self._partial(message):
                    yield signal
            elif kind == "transcript.done":
                log.debug(
                    "stt.xai.stream_done",
                    provider=self.config.name,
                    provider_id=self.config.id,
                )
                return
            elif kind == "error":
                log.warning(
                    "stt.xai.error_frame",
                    provider=self.config.name,
                    provider_id=self.config.id,
                    detail=get_str(message, "message") or "no detail",
                )
                return
            # transcript.created and anything else is consumed and not
            # translated, which is all the liveness watchdog needs: it counts
            # messages received, and a connection that only greets is alive.

    def _partial(self, message: dict[str, object]) -> list[TurnSignal]:
        """One ``transcript.partial`` as signals, on this connection's clock.

        ``TurnStarted`` goes out ahead of every partial rather than once per
        turn, because the vendor announces no turn start of its own and the
        first word's offset is the only honest answer to when one began.
        Repeating it is free: the stream opens a turn on the first and reads
        the rest as naming the turn already open. Without it a turn would start
        at the frame its first partial happened to arrive on, which is the
        vendor's latency late, and the timeline dots key off that number.
        """
        words = self._turn_words(parse_words(message, offset=0.0))
        text = _since(self._settled_text, get_str(message, "text"))
        start = words[0].start if words else _offset(message, "start")
        if not get_bool(message, "speech_final"):
            if not text:
                return []
            return [TurnStarted(at=start), TurnPartial(text=text, append=False)]
        end = words[-1].end if words else _span_end(message)
        self._settled_text = get_str(message, "text")
        if words:
            self._settled_until = words[-1].end
        return [TurnStarted(at=start), TurnFinal(text=text, words=words, at=start, to=end)]

    def _turn_words(self, words: list[Word]) -> tuple[Word, ...]:
        """Only the words of the turn still open; see the module docstring.

        A no-op under the reading where each event carries its own utterance,
        because a word of the next turn cannot end before the last one did.
        """
        return tuple(w for w in words if w.end > self._settled_until + _WORD_EPSILON_S)

    async def flush_input(self) -> None:
        """Close the open utterance without closing the socket.

        ``finalize`` is documented as forcing the current utterance to finalize
        as ``speech_final`` immediately, which is precisely "no more audio is
        coming for this turn" and nothing more: the answer is one last
        ``transcript.partial``, and the connection stays up for
        :meth:`close_stream` to end properly. ``audio.done`` would flush the
        same turn and hang up, which is why it lives one step further on.
        """
        ws = self._stream_ws
        if ws is not None:
            # Lowercase is the spelling the guide's own table uses; the API
            # reference lists "finalize / Finalize" as one message, so either
            # is accepted.
            await ws.send(json.dumps({"type": "finalize"}))

    async def close_stream(self) -> None:
        """Say the audio has ended, then drop the connection.

        ``audio.done`` is the documented end of a stream and the vendor answers
        it by flushing and hanging up. Nothing waits for that answer: every turn
        worth publishing has been settled by :meth:`flush_input` already, and
        ``transcript.done`` carries the whole session rather than a turn. So
        this is a courtesy, bounded, and skipped in effect on a socket that has
        already died - which is the only kind this is called on after a failure.
        """
        ws, self._stream_ws = self._stream_ws, None
        if ws is None:
            return
        with contextlib.suppress(Exception):
            async with asyncio.timeout(_CLOSE_TIMEOUT_S):
                await ws.send(json.dumps({"type": "audio.done"}))
        with contextlib.suppress(Exception):
            await ws.close()

    async def aclose(self) -> None:
        await self.close_stream()


async def _await_ready(ws: ClientConnection) -> None:
    """Read until the server says it is ready for audio, or say why it is not."""
    async with asyncio.timeout(_READY_TIMEOUT_S):
        async for raw in ws:
            message = as_dict(raw)
            kind = get_str(message, "type")
            if kind == "transcript.created":
                return
            if kind == "error":
                raise RuntimeError(get_str(message, "message") or "the socket was refused")
    msg = "the socket closed before it was ready for audio"
    raise RuntimeError(msg)


def _since(settled: str, text: str) -> str:
    """``text`` with an already-published transcript stripped off the front.

    See the module docstring: the vendor's ``text`` is cumulative and the docs
    do not settle cumulative over what, so this is written to be a no-op under
    the per-utterance reading and exact under the per-stream one. Only a
    strictly longer text is stripped, so a turn that repeats the last one word
    for word survives as itself rather than as nothing.
    """
    if not settled or len(text) <= len(settled) or not text.startswith(settled):
        return text
    return text[len(settled) :].lstrip()


def _offset(message: dict[str, object], key: str) -> float | None:
    """A vendor offset in seconds, or None where the vendor stated none.

    None matters: it is the difference between "this turn began 1.2s in" and
    "this vendor did not say", and the stream falls back to the last frame
    written for the second rather than pinning the turn to zero.
    """
    return get_float(message, key) if key in message else None


def _span_end(message: dict[str, object]) -> float | None:
    """Where a partial's span ends, from the ``start``/``duration`` pair."""
    start = _offset(message, "start")
    if start is None or "duration" not in message:
        return None
    return start + get_float(message, "duration")


@register(ProviderKind.XAI, realtime=True)
def _factory(  # pyright: ignore[reportUnusedFunction]
    config: ProviderConfig,
    secrets: SecretStore,
    model: str | None,
    caps: TranscribeCapabilities | None,
) -> XaiBackend:
    return XaiBackend(config, model=model, caps=caps, api_key=secret_for(config, secrets))
