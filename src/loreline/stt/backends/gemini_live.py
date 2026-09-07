"""Gemini Live API transcription connector (WebSocket, ``BidiGenerateContent``).

Both connector shapes over one protocol, on two separate sockets, because the
two want opposite things from the session: one is bounded by the caller's
utterance and ends with ``audioStreamEnd``, the other outlives every turn in it.

**Streaming** (``StreamingConnector``, what a live capture takes, ADR 0006):
one session for the whole capture, with Google's own endpointing deciding the
turns. ``setup.realtimeInputConfig.automaticActivityDetection`` is what turns
that on (see _AUTOMATIC_VAD), and a model that refuses it gets
``StreamUnsupportedError`` rather than a session that would accept audio
forever and answer nothing. Frames arrive at capture pace from the stream above,
which is what the pacing note under _CHUNK_MS is about: this shape does not
pace anything itself because it is never handed audio faster than a microphone
produces it.

Manual activity signals (``activityStart`` / ``activityEnd``) exist and are
deliberately unused: they would put a local VAD back in charge of the turn
boundaries, which is precisely what the streaming shape exists to stop doing.

**One utterance per call** (``Connector``, ADR 0005) is kept for re-processing
stored audio, for the call-shaped fallback path, and for the tests that predate
streaming. It opens a session per utterance, paces its own send, and flushes
with ``audioStreamEnd``. That per-utterance session is not a preference: on a
caller-bounded utterance ``audioStreamEnd`` is the only "no more audio" signal
there is, and a fresh session keeps a late frame from poisoning the next
utterance's reads. Neither fact says anything about a stream with no caller
boundary in it, where nothing is flushed per turn and one reader consumes the
vendor's own finals for the life of the session.

VERIFIED against the real service (45 s of human speech, one session): the wire
names below are the ones Google actually sends, all lowerCamelCase. Two service
behaviours the docs do not mention were measured rather than guessed: a turn
ends with generationComplete and never with turnComplete (see _TurnState), and
audio pushed faster than realtime desynchronises the service's turn machinery
(see _CHUNK_MS).

Deliberately raw WebSocket rather than the ``google-genai`` SDK: this app
targets a Raspberry Pi and shed ``google-cloud-speech`` specifically to stay
light, the other realtime connectors already speak ``websockets`` directly, and
only a raw connector can be pointed at the local mock servers those connectors
are tested against (``config.base_url``).

Protocol: the client sends a ``setup`` message, then ``realtimeInput`` audio
chunks (raw 16-bit PCM at 16 kHz, base64), then ``audioStreamEnd`` to say the
audio has stopped. The server answers with ``serverContent`` frames whose
``interimInputTranscription`` is the low-latency partial (cumulative within a
turn, so it replaces rather than appends) and ``inputTranscription`` the
finalized text, and with ``goAway`` and ``sessionResumptionUpdate`` frames for
the session lifetime described below.

**Sessions are capped in minutes and a table runs for hours.** Google documents
10 minutes for live transcription and 15 for audio-only sessions generally, so a
connection ending is normal operation here rather than a fault. ``goAway`` is
the announcement, and this connector ends its signal iteration on it, which the
stream above reads as a lost connection: it settles the open turn from its last
interim, reconnects, and marks the second or so of audio that the reconnect
swallowed with a gap row. ``sessionResumption`` is asked for in every setup and
the newest handle is carried across connections, so the vendor sees one
continuing session rather than a fresh one every ten minutes.

Server-side VAD gates the whole pipeline: synthetic speech (espeak-ng and
friends) is never classified as speech, so a session fed it returns
``setupComplete`` and nothing else. That is not a connector fault, and it is
why the mock in ``mocks/gemini_live_ws.py`` replays recorded real frames.

No words, no speakers, and no timing of any kind: Google states plainly that
"Speaker diarization is not supported in live streaming sessions" (the batch
``gemini-3.5-transcribe`` diarizes; this model does not - see
loreline.capabilities), and no frame carries an offset into the audio. A turn's
start and end therefore come from the stream's fallback for a vendor that states
none, the capture timestamp of the last frame written when the signal arrived,
which is late by Google's own latency and is the best answer available without
one (ADR 0006, Decision 5).

A glossary does work here, contrary to what this connector used to claim:
``setup.inputAudioTranscription.customVocabulary`` is accepted and it measurably
changes the transcript (see _setup and _vocabulary_for).

Docs:
- https://ai.google.dev/gemini-api/docs/live-api/live-transcribe
- https://ai.google.dev/gemini-api/docs/live-guide
- https://ai.google.dev/gemini-api/docs/live-session
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, ConnectionClosedOK

from loreline.audio.chunker import Utterance
from loreline.capabilities import surface_for
from loreline.capability_config import TranscribeCapabilities
from loreline.logging import get_logger
from loreline.models import Glossary, Interaction, ProviderConfig, ProviderKind
from loreline.secrets import SecretStore
from loreline.stt.backends._ws import (
    as_dict,
    as_obj_dict,
    get_bool,
    get_str,
)
from loreline.stt.base import (
    Connector,
    Transcription,
    glossary_terms,
    glossary_terms_for,
    secret_for,
)
from loreline.stt.registry import register
from loreline.stt.streaming import (
    StreamingConnector,
    StreamUnsupportedError,
    TurnFinal,
    TurnPartial,
    TurnSignal,
)

log = get_logger(__name__)

# The documented chunk cadence; at 16 kHz s16le that is 3200 bytes per message.
# It is also the send *rate*: this connector waits a chunk's worth of time
# between chunks. That is not politeness, it is what makes the service's turn
# structure mean anything. The same 45 s clip, blasted against paced at 1x:
#
#   blasted: 223 of 438 chars. The turn still open when the service caught up
#            never finalized, and the service left as much as 2.34 s of
#            silence between finishing one turn and starting the next, so a
#            client cannot tell a gap between turns from the end of the
#            session and has nothing to end a session on but a timeout.
#   paced:   438 chars in four turns, each closed by generationComplete, the
#            last one arriving 0.8 s after audioStreamEnd.
#
# The chunker hands this connector a complete utterance, live capture
# included, so pacing costs the utterance's own duration: 45.8 s for that
# clip. Blasting it cost 27.4 s and half the text, and it could only end the
# session by burning the whole _RECV_TIMEOUT_S below, which is 10 s of that
# 27.4 s. So pacing is also the faster of the two for any utterance shorter
# than about 15 s, which is what the live path actually sees.
_CHUNK_MS = 100
_MS_PER_S = 1000
# The server acks setup before it accepts audio (the SDK's connect() blocks on
# this ack too); a session that never acks is broken, so raising the timeout
# out of transcribe lets the router's failover take over.
_SETUP_TIMEOUT_S = 10.0
# Safety net per received frame, for a session that says nothing at all (a
# rejected key that still upgrades the socket, audio the server-side VAD hears
# as silence). It bounds the stall per utterance instead of hanging the
# session; it is not how a healthy session ends, which is generationComplete
# arriving after audioStreamEnd (see _read_last_turn).
_RECV_TIMEOUT_S = 10.0
# What the Live API accepts, and the only rate it accepts: "raw, little-endian,
# 16-bit PCM" at 16 kHz. It happens to be what Silero pins capture to as well,
# so the stream's resampler is a pass-through for this vendor.
_INPUT_RATE = 16_000
# Google's own endpointing, which is what the streaming shape is for: it decides
# turns from the audio it is hearing rather than from a local VAD that has never
# heard the model. Every value is stated rather than inherited, so a change here
# is a change with a reason.
#
# VERIFIED accepted by gemini-3.5-transcribe-live, setup only, no audio: the
# service rejects unknown fields and bad enum values by closing with 1007 and
# naming the field path in snake_case ("Invalid value at 'setup.
# realtime_input_config.automatic_activity_detection.start_of_speech_
# sensitivity'"), so setupComplete over this block is evidence rather than
# silence. That naming is also what _refusal below reads.
_AUTOMATIC_VAD: dict[str, object] = {
    "disabled": False,
    # A table has people at different distances from one microphone, and a turn
    # start the service misses is speech nobody ever sees; a false start costs
    # an empty turn, which the stream drops because it has no text.
    "startOfSpeechSensitivity": "START_SENSITIVITY_HIGH",
    # The other way round at the end of a turn: cutting eagerly splits one
    # sentence across two rows, and the silence threshold below is the knob that
    # is supposed to decide when a turn is over.
    "endOfSpeechSensitivity": "END_SENSITIVITY_LOW",
    # Google recommends 500-800 ms. 500 is deliberate, and it is the whole
    # latency argument for this shape: the utterance path cannot answer before
    # 800 ms of trailing silence has elapsed, because that is when its chunker
    # hands the utterance over.
    "silenceDurationMs": 500,
    "prefixPaddingMs": 300,
}
# How the service names the two setup blocks in a rejection. A refused
# automaticActivityDetection is this model saying it will not stream, which no
# retry can change; a refused sessionResumption only costs the resumption.
_VAD_FIELDS = ("automatic_activity_detection", "realtime_input_config")
_RESUMPTION_FIELDS = ("session_resumption", "session not found")


def _vocabulary_for(caps: TranscribeCapabilities | None, terms: list[str]) -> list[str]:
    """Glossary terms for ``customVocabulary``, capped for this model.

    The ceiling is enforced here rather than discovered at the vendor because
    the service rejects the whole *setup* over it, closing the socket with
    1007 "custom_vocabulary cannot contain more than 1000 entries." That costs
    the entire utterance, not just the surplus terms. Measured: 1000 entries
    ack, 1001 close. The number lives in capabilities.yaml, which is also what
    the UI renders the glossary toggle from.
    """
    return glossary_terms_for(caps, terms, realtime=True)


def _wire(mapping: dict[str, object], name: str, alt: str) -> object:
    """Read a proto-JSON field by either spelling.

    Google's proto-JSON mapping emits lowerCamelCase, and the verification run
    confirmed that is what this service sends. Accepting the snake_case
    original too costs one dict lookup and keeps a proto-JSON gateway that
    emits the other spelling from losing the transcript.
    """
    value = mapping.get(name)
    return value if value is not None else mapping.get(alt)


@dataclass
class _TurnState:
    """Everything one session's ``serverContent`` frames have said so far.

    Kept apart from the socket so the frame handling can be tested against
    frames recorded from the real service (tests/unit/test_gemini_live_frames)
    rather than from a mock built out of the docs, which is how the two
    behaviours below were missed in the first place.

    Frame shapes, verbatim from a real session::

        {"setupComplete": {}}
        {"serverContent": {"interimInputTranscription": {"text": "Marseille"}}}
        {"serverContent": {"inputTranscription": {"text": "Marseille: The Arrival"}}}
        {"serverContent": {"generationComplete": true}}
        {"serverContent": {}}

    turnComplete never appears: not once in 200+ frames across three runs.
    generationComplete is the turn end, and it is still accepted alongside
    turnComplete because the docs define that one and either means the same
    thing to this loop.

    The empty ``{"serverContent": {}}`` frames are padding and are ignored,
    deliberately: one follows every generationComplete, a second one precedes
    every turn that follows, and one also arrives right after setupComplete.
    So their count differs between a turn that ends the session (one) and a
    turn with another behind it (two), which makes them useless as a marker
    and harmless to skip.
    """

    parts: list[str] = field(default_factory=list[str])
    # The newest interim of the turn now open. Interims are cumulative within
    # a turn, so the newest one is the whole turn, and it is only ever used as
    # a fallback for a turn the service never finalizes (see _flush_open_turn).
    interim: str = ""
    turn_ended: bool = False

    def apply(self, raw: str | bytes) -> None:
        """Fold one server frame into the state."""
        content = as_obj_dict(_wire(as_dict(raw), "serverContent", "server_content"))
        final = get_str(
            as_obj_dict(_wire(content, "inputTranscription", "input_transcription")), "text"
        )
        interim = get_str(
            as_obj_dict(_wire(content, "interimInputTranscription", "interim_input_transcription")),
            "text",
        )
        if final:
            self.parts.append(final)
            self.interim = ""
        elif interim:
            self.interim = interim
        if final or interim:
            self.turn_ended = False
        elif _ends_turn(content):
            self._flush_open_turn()
            self.turn_ended = True

    def transcript(self) -> str:
        """The utterance text, once the session is over."""
        self._flush_open_turn()
        # Each final is one whole turn of speech with no leading space of its
        # own ("Marseille: The Arrival" then "signaled the Three Master"), so
        # they are joined with a space, exactly as the batch connector joins
        # its events. Concatenating them ran words together.
        return " ".join(self.parts).strip()

    def _flush_open_turn(self) -> None:
        """Keep the interim text of a turn the service never finalized.

        Insurance, not the normal path: a paced session finalizes every turn.
        A session that outruns the service does not, and the recorded blast
        run is what that looks like, 223 chars of finals with another 212 sat
        in interims the loop threw away. Since interims are cumulative, the
        newest one is that turn's text, so keeping it turns a silent
        truncation into slightly rougher wording.
        """
        if self.interim:
            self.parts.append(self.interim)
            self.interim = ""


def _ends_turn(content: dict[str, object]) -> bool:
    """Whether a ``serverContent`` body closes the turn it belongs to.

    generationComplete is the one the service actually sends; turnComplete is
    the one the docs define and is still honoured, since either means the same
    thing to both shapes of this connector.
    """
    return any(
        get_bool(content, name)
        for name in (
            "generationComplete",
            "generation_complete",
            "turnComplete",
            "turn_complete",
        )
    )


@dataclass
class _StreamTurns:
    """One persistent session's frames, as the turn signals ADR 0006 defines.

    The same frames :class:`_TurnState` folds into one utterance's text, read
    the other way round: each one becomes zero or one signal for the stream
    above, which owns the clock, the turn ids and the reconnects. Kept apart
    from the socket for the same reason as _TurnState, so the translation can be
    tested against frames recorded from the real service.

    Two frames need state, and it is the minimum the protocol forces:

    * ``interimInputTranscription`` is *cumulative within a turn*, so it is a
      ``TurnPartial`` with ``append=False``. Appending them would repeat every
      word as many times as the service revised the turn.
    * ``generationComplete`` arrives *after* the ``inputTranscription`` that
      settles a turn, so on its own it must publish nothing, or every turn would
      be written twice. It is not useless, though: a turn the service never
      finalizes ends here too, and then the newest interim is that turn's text
      (the recorded blast run left 212 characters sitting in interims). Which of
      the two happened is what :attr:`owes_final` remembers.

    The empty ``{"serverContent": {}}`` padding frames yield nothing at all,
    deliberately: their count differs between a turn with another behind it and
    a turn that ends the session, which makes them useless as a marker.
    """

    # The newest interim of the turn now open, and whether that turn still owes
    # the transcript its text.
    interim: str = ""
    owes_final: bool = False
    # Set once the server announces that this connection is about to end, with
    # goAway.timeLeft as Google phrased it. The reader stops on it; the stream
    # above reconnects, which is what a session cap of minutes needs.
    go_away: str = ""
    # The newest session resumption handle, kept by the connector across
    # connections so a reconnect continues the same vendor session.
    handle: str = ""

    def apply(self, raw: str | bytes) -> list[TurnSignal]:
        """Translate one server frame into the signals it means."""
        message = as_dict(raw)
        go_away = _wire(message, "goAway", "go_away")
        if go_away is not None:
            left = as_obj_dict(go_away)
            self.go_away = get_str(left, "timeLeft") or get_str(left, "time_left") or "soon"
            return []
        update = as_obj_dict(_wire(message, "sessionResumptionUpdate", "session_resumption_update"))
        if update:
            self._remember(update)
            return []
        content = as_obj_dict(_wire(message, "serverContent", "server_content"))
        final = get_str(
            as_obj_dict(_wire(content, "inputTranscription", "input_transcription")), "text"
        )
        interim = get_str(
            as_obj_dict(_wire(content, "interimInputTranscription", "interim_input_transcription")),
            "text",
        )
        if final:
            self.interim, self.owes_final = "", False
            return [TurnFinal(text=final)]
        if interim:
            self.interim, self.owes_final = interim, True
            return [TurnPartial(text=interim)]
        if _ends_turn(content):
            return self._close_turn()
        return []

    def _close_turn(self) -> list[TurnSignal]:
        """What generationComplete means for a turn that was never finalized."""
        text, owed = self.interim, self.owes_final
        self.interim, self.owes_final = "", False
        return [TurnFinal(text=text)] if owed and text else []

    def _remember(self, update: dict[str, object]) -> None:
        """Keep a resumption handle the server offered, if it offered one.

        ``resumable: false`` is the server saying this session cannot be picked
        up again, so the handle it may have sent earlier is dropped rather than
        presented to a reconnect that would be refused for it.
        """
        handle = get_str(update, "newHandle") or get_str(update, "new_handle")
        if handle:
            self.handle = handle
        elif not get_bool(update, "resumable", default=True):
            self.handle = ""


class GeminiLiveBackend(Connector[list[str]], StreamingConnector):
    """Streaming transcription (no diarization) via the Gemini Live API.

    Both connector shapes; see the module docstring for what each one does and
    why they cannot share a socket. The prepared value is the
    ``customVocabulary`` list, capped for the model, and the streaming shape
    prepares it in :meth:`open_stream` because the session carries it.

    Two things are deliberately kept across connections, and only two: the
    session resumption handle, which is the whole point of resumption, and
    ``_resumption_refused``, because a model that will not resume will not
    resume on the next socket either and re-learning it costs a whole
    connection.
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
        # The streaming shape's socket and its per-connection frame state. Both
        # are None/fresh between connections; see the class docstring for the
        # two things that are not.
        self._stream_ws: ClientConnection | None = None
        self._turns = _StreamTurns()
        self._resume_handle = ""
        self._resumption_refused = False

    def _session_url(self) -> str:
        # The Live API authenticates with the key as a URL query parameter,
        # not a header (unlike the batch Gemini connector's x-goog-api-key);
        # the surface says so, and the endpoint spells it.
        return self._endpoint.url_with_key(self._api_key)

    def _setup(self, vocabulary: list[str]) -> dict[str, object]:
        # The SDK's LiveConnectConfig(response_modalities=["TEXT"],
        # input_audio_transcription=AudioTranscriptionConfig(language_codes=[]))
        # in wire form. An empty languageCodes list means auto-detect, which is
        # what a provider configured with no language should get.
        transcription: dict[str, object] = {
            "languageCodes": [self._language] if self._language else []
        }
        # VERIFIED against the real service, since this connector previously
        # claimed the opposite. setup.inputAudioTranscription.customVocabulary
        # is a real field: sending it acks with setupComplete, and the service
        # rejects unknown fields rather than ignoring them, so the ack means
        # something. A probe sending it as an object drew "Invalid value at
        # 'setup.input_audio_transcription' (custom_vocabulary), Starting an
        # object on a scalar field", which names the field, while a made-up
        # sibling drew 'Unknown name "zzzNotAField" ... Cannot find field.'
        # It also changes the transcript, which is the part that matters,
        # since an accepted-and-ignored field would ack just the same. Same
        # 50 s clip, twice each way, byte-identical within each condition:
        # "Cape Morgion" became "Cape Morgiou", "Rion Island" became "Riou
        # Island", and "the old Foxy docks" became "the old Phocee docks",
        # each matching what the batch model returns unprompted. Omitted when
        # empty so a session with no glossary sends exactly what it sent
        # before.
        if vocabulary:
            transcription["customVocabulary"] = vocabulary
        setup: dict[str, object] = {
            "generationConfig": {"responseModalities": ["TEXT"]},
            "inputAudioTranscription": transcription,
        }
        # Required by the protocol, and every caller of this connector has
        # chosen one. Omitted rather than replaced with a guess if none came:
        # the service then says which field is absent, where a substituted
        # model id would run the wrong one silently.
        if self._model:
            setup["model"] = self._model if "/" in self._model else f"models/{self._model}"
        return {"setup": setup}

    def _audio_message(self, chunk: bytes) -> dict[str, object]:
        return {
            "realtimeInput": {
                "audio": {
                    "data": base64.b64encode(chunk).decode("ascii"),
                    "mimeType": f"audio/pcm;rate={self.config.sample_rate}",
                }
            }
        }

    def prepare(self, glossary: Glossary | None) -> list[str]:
        return _vocabulary_for(self._caps, glossary_terms(glossary))

    async def transcribe_one(self, utterance: Utterance, prepared: list[str]) -> Transcription:
        state = _TurnState()
        loop = asyncio.get_running_loop()
        async with connect(self._session_url()) as ws:
            await ws.send(json.dumps(self._setup(prepared)))
            await self._await_setup_ack(ws)
            open_socket = True
            # The send is paced at the capture cadence, and the wait between
            # chunks doubles as the read window, which is where every turn but
            # the last one is transcribed and closed. See _CHUNK_MS for why
            # the pacing is not optional.
            for chunk in _audio_chunks(utterance.pcm, self.config.sample_rate):
                if not open_socket:
                    break
                await ws.send(json.dumps(self._audio_message(chunk)))
                open_socket = await self._read_until(
                    ws, state, deadline=loop.time() + _CHUNK_MS / _MS_PER_S
                )
            if open_socket:
                await ws.send(json.dumps({"realtimeInput": {"audioStreamEnd": True}}))
                await self._read_last_turn(ws, state)
        # No words: the Live API returns no word timings and no speakers.
        return Transcription(text=state.transcript())

    async def _read_until(
        self, ws: ClientConnection, state: _TurnState, *, deadline: float
    ) -> bool:
        """Fold in whatever the server sends before ``deadline``.

        Returns False once the socket is closed, so the send loop stops
        pushing audio into a session that has gone away.
        """
        loop = asyncio.get_running_loop()
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return True
            try:
                async with asyncio.timeout(remaining):
                    raw = await ws.recv()
            except TimeoutError:
                return True
            except ConnectionClosedOK:
                return False
            state.apply(raw)

    async def _read_last_turn(self, ws: ClientConnection, state: _TurnState) -> None:
        """Read the flush that ``audioStreamEnd`` triggers, then stop.

        generationComplete is an end-of-*session* signal only here, and only
        because the send was paced: every earlier turn closed while audio was
        still going out, and no audio is left to open another one. The turn
        ends that happened during the send are therefore forgotten first, or a
        turn that closed on the last chunk would end the session before the
        flush it was waiting for arrived.

        Measured: a clip padded with a full second of digital silence, which
        is what the VAD chunker hands over, still finalized its last turn 0.22
        s *after* audioStreamEnd rather than on the silence. _RECV_TIMEOUT_S
        covers the case that produces no marker at all, a session whose audio
        the server-side VAD never heard as speech.
        """
        state.turn_ended = False
        while not state.turn_ended:
            try:
                async with asyncio.timeout(_RECV_TIMEOUT_S):
                    raw = await ws.recv()
            except (TimeoutError, ConnectionClosedOK):
                return
            state.apply(raw)

    async def _await_setup_ack(self, ws: ClientConnection) -> None:
        """Drain frames until the server acknowledges the session setup.

        Kept out of the main receive loop so a session the server never
        configures fails here, loudly (TimeoutError -> the router's failover),
        rather than counting as an utterance that transcribed to nothing.
        """
        async with asyncio.timeout(_SETUP_TIMEOUT_S):
            while True:
                message = as_dict(await ws.recv())
                if _wire(message, "setupComplete", "setup_complete") is not None:
                    return

    # -- the streaming shape ---------------------------------------------

    @property
    def stream_rate(self) -> int:
        return _INPUT_RATE

    def _stream_setup(self, vocabulary: list[str]) -> dict[str, object]:
        """The per-utterance setup plus what a session outliving a turn needs.

        Built on :meth:`_setup` rather than beside it, so the model, the
        language and the custom vocabulary can only ever be spelled once.
        """
        setup = as_obj_dict(self._setup(vocabulary)["setup"])
        setup["realtimeInputConfig"] = {"automaticActivityDetection": dict(_AUTOMATIC_VAD)}
        if not self._resumption_refused:
            # An empty config asks the server to start issuing handles; a handle
            # asks it to continue the session that one names.
            setup["sessionResumption"] = (
                {"handle": self._resume_handle} if self._resume_handle else {}
            )
        return {"setup": setup}

    async def open_stream(self, glossary: Glossary | None) -> None:
        """Open one automatic-VAD session for a whole capture.

        Runs again after every disconnect, which is routine for this vendor
        rather than exceptional: see the module docstring on the session cap.
        The glossary rides in the setup, so a reconnect re-applies it because
        this runs again, and the resumption handle from the connection that just
        died is presented here.
        """
        await self.close_stream()  # a reconnect must not leak the dead socket
        # Seeded with the handle the dead connection left, so a connection the
        # server never sends an update on still resumes on the one after it.
        self._turns = _StreamTurns(handle=self._resume_handle)
        setup = json.dumps(self._stream_setup(self.prepare(glossary)))
        ws = await connect(self._session_url())
        try:
            await ws.send(setup)
            await self._await_setup_ack(ws)
        except BaseException as exc:
            with contextlib.suppress(Exception):
                await ws.close()
            if isinstance(exc, ConnectionClosed):
                self._classify_refusal(exc)
            raise
        self._stream_ws = ws

    def _classify_refusal(self, closed: ConnectionClosed) -> None:
        """Read a rejected setup out of the close frame the service sent.

        This service says no by closing with 1007 and naming the offending
        field path in snake_case, rather than by an error message on an open
        socket. Two of those names matter here and the rest are ordinary
        connection failures for the stream above to retry.

        A refused ``automaticActivityDetection`` is this model saying it will
        not stream, which is an answer no retry can change, so it becomes
        :class:`StreamUnsupportedError` and the session runs this same connector
        one utterance at a time. A refused ``sessionResumption`` is only the
        resumption failing: the handle is dropped and the field is left out from
        here on, and the next attempt connects as a fresh session. A stale
        handle says so differently, 1008 "session not found", and is the same
        answer for the same reason - handles expire two hours after the session
        they name.
        """
        reason = (closed.rcvd.reason if closed.rcvd is not None else "").lower()
        if any(name in reason for name in _VAD_FIELDS):
            msg = (
                f"{self._model or 'this model'} does not support server-side turn "
                f"detection: {closed.rcvd.reason if closed.rcvd else closed}"
            )
            raise StreamUnsupportedError(msg)
        if any(name in reason for name in _RESUMPTION_FIELDS):
            log.warning(
                "gemini.live.resumption_refused",
                provider=self.config.id,
                had_handle=bool(self._resume_handle),
                detail=closed.rcvd.reason if closed.rcvd else str(closed),
            )
            # A handle that was merely stale is worth dropping on its own; the
            # field is only abandoned when the service refused the field itself.
            self._resume_handle = ""
            self._resumption_refused = "session not found" not in reason

    async def send_audio(self, pcm: bytes) -> None:
        """Write one block of 16 kHz s16le PCM as a ``realtimeInput`` frame.

        One frame in, one message out, deliberately: the stream above feeds at
        capture pace, so nothing here has to pace or batch, and buffering to the
        documented ~100 ms cadence would only add up to 100 ms to the latency
        this whole shape exists to cut. The rate in the mime type is
        :attr:`stream_rate` and not ``config.sample_rate``, because the stream
        has already resampled to it.
        """
        ws = self._stream_ws
        if ws is None:
            msg = "send_audio before open_stream"
            raise RuntimeError(msg)
        await ws.send(
            json.dumps(
                {
                    "realtimeInput": {
                        "audio": {
                            "data": base64.b64encode(pcm).decode("ascii"),
                            "mimeType": f"audio/pcm;rate={_INPUT_RATE}",
                        }
                    }
                }
            )
        )

    async def signals(self) -> AsyncIterator[TurnSignal]:
        """Translate this session's frames into turn signals until it ends.

        No offsets are yielded because the service states none: every signal
        leaves ``at`` unset and the stream above dates it from the last frame
        written. No refs either, since the protocol names no turns, which the
        stream reads as one turn open at a time.

        ``goAway`` ends the iteration rather than raising. The connection really
        is over, the stream above treats a reader that returned exactly as it
        treats one that failed, and this way the reconnect happens at a moment
        we chose instead of whenever the server hangs up.
        """
        ws = self._stream_ws
        if ws is None:
            return
        async for raw in ws:
            for signal in self._turns.apply(raw):
                yield signal
            # Kept off the frame state and on the connector, because it is the
            # one thing about this connection that outlives it.
            self._resume_handle = self._turns.handle
            if self._turns.go_away:
                log.info(
                    "gemini.live.go_away",
                    provider=self.config.id,
                    time_left=self._turns.go_away,
                    resumable=bool(self._resume_handle),
                )
                return

    async def flush_input(self) -> None:
        """Say the audio has stopped, so the open turn finalizes now.

        ``audioStreamEnd`` is what the docs prescribe for a microphone going
        away, and the service answers it by finalizing without waiting out its
        own silence threshold. Only ever the last turn: every earlier one was
        closed by the service's own endpointing while audio was still arriving.
        """
        ws = self._stream_ws
        if ws is not None:
            await ws.send(json.dumps({"realtimeInput": {"audioStreamEnd": True}}))

    async def close_stream(self) -> None:
        ws, self._stream_ws = self._stream_ws, None
        if ws is not None:
            with contextlib.suppress(Exception):
                await ws.close()

    async def aclose(self) -> None:
        await self.close_stream()


def _audio_chunks(pcm: bytes, sample_rate: int) -> list[bytes]:
    """Split s16le PCM into the ~100 ms messages the docs prescribe."""
    step = max(2, sample_rate * 2 * _CHUNK_MS // _MS_PER_S)
    return [pcm[pos : pos + step] for pos in range(0, len(pcm), step)] or [pcm]


@register(ProviderKind.GEMINI, realtime=True)
def _factory(  # pyright: ignore[reportUnusedFunction]
    config: ProviderConfig,
    secrets: SecretStore,
    model: str | None,
    caps: TranscribeCapabilities | None,
) -> GeminiLiveBackend:
    return GeminiLiveBackend(config, model=model, caps=caps, api_key=secret_for(config, secrets))
