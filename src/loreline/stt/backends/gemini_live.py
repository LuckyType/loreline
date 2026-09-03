"""Gemini Live API transcription connector (WebSocket, ``BidiGenerateContent``).

VERIFIED against the real service (45 s of human speech, one session): the
wire names below are the ones Google actually sends, all lowerCamelCase.
Two service behaviours the docs do not mention shape this connector, and both
were measured rather than guessed: a turn ends with generationComplete and
never with turnComplete (see _TurnState), and audio pushed faster than
realtime desynchronises the service's turn machinery (see _CHUNK_MS).

Deliberately raw WebSocket rather than the ``google-genai`` SDK: this app
targets a Raspberry Pi and shed ``google-cloud-speech`` specifically to stay
light, the three existing streaming connectors (Deepgram, AssemblyAI, OpenAI
Realtime) already speak ``websockets`` directly, and only a raw connector can
be pointed at the local mock servers those connectors are tested against
(``config.base_url``).

Protocol: the client sends a ``setup`` message, then ``realtimeInput`` audio
chunks (raw 16-bit PCM, base64, ~100 ms each - which is precisely what the
capture pipeline produces at 16 kHz s16le, no resampling needed), then
``audioStreamEnd`` to flush. The server answers with ``serverContent`` frames
whose ``interimInputTranscription`` is the low-latency partial and
``inputTranscription`` the finalized text. One session per voiced utterance,
like the Deepgram and AssemblyAI connectors: ``audioStreamEnd`` is the only
documented "no more audio" signal, and a fresh session per utterance keeps a
late frame from poisoning the next utterance's reads.

Server-side VAD gates the whole pipeline: synthetic speech (espeak-ng and
friends) is never classified as speech, so a session fed it returns
``setupComplete`` and nothing else. That is not a connector fault, and it is
why the mock in ``mocks/gemini_live_ws.py`` replays recorded real frames.

No words, no speakers: Google states plainly that "Speaker diarization is not
supported in live streaming sessions" (the batch ``gemini-3.5-transcribe``
diarizes; this model does not - see loreline.capabilities).

Docs: https://ai.google.dev/gemini-api/docs/live-api/live-transcribe
"""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from urllib.parse import urlencode

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosedOK, WebSocketException

from loreline.audio.chunker import Utterance
from loreline.health import PROBE_TIMEOUT_S, HealthReport, HealthStatus
from loreline.logging import get_logger
from loreline.models import Glossary, ProviderConfig, ProviderKind, TranscriptEvent
from loreline.secrets import SecretStore
from loreline.stt.backends._ws import (
    as_dict,
    as_obj_dict,
    classify_handshake_error,
    get_bool,
    get_str,
    probe_health,
)
from loreline.stt.base import glossary_terms
from loreline.stt.registry import register

log = get_logger(__name__)

_DEFAULT_URL = (
    "wss://generativelanguage.googleapis.com/ws/"
    "google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent"
)
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
        elif self._ends_turn(content):
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

    @staticmethod
    def _ends_turn(content: dict[str, object]) -> bool:
        return any(
            get_bool(content, name)
            for name in (
                "generationComplete",
                "generation_complete",
                "turnComplete",
                "turn_complete",
            )
        )


class GeminiLiveBackend:
    """Streaming transcription (no diarization) via the Gemini Live API."""

    def __init__(
        self,
        config: ProviderConfig,
        *,
        model: str | None = None,
        api_key: str | None = None,
        language: str | None = None,
    ) -> None:
        self.config = config
        self._api_key = api_key
        self._language = language or config.language
        self._model = model
        self._url = config.base_url or _DEFAULT_URL

    def _session_url(self) -> str:
        # The Live API authenticates with the key as a URL query parameter,
        # not a header (unlike the batch Gemini connector's x-goog-api-key).
        if not self._api_key:
            return self._url
        return f"{self._url}?{urlencode({'key': self._api_key})}"

    def _setup(self) -> dict[str, object]:
        # The SDK's LiveConnectConfig(response_modalities=["TEXT"],
        # input_audio_transcription=AudioTranscriptionConfig(language_codes=[]))
        # in wire form. An empty languageCodes list means auto-detect, which is
        # what a provider configured with no language should get.
        transcription: dict[str, object] = {
            "languageCodes": [self._language] if self._language else []
        }
        setup: dict[str, object] = {
            "generationConfig": {"responseModalities": ["TEXT"]},
            "inputAudioTranscription": transcription,
        }
        # Required by the protocol, and this kind always resolves one (see the
        # Gemini default in capabilities.yaml). Omitted rather than replaced
        # with a guess if that marker ever goes missing: the service then says
        # which field is absent, where a substituted model id would run the
        # wrong one silently.
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

    async def transcribe(
        self,
        audio: AsyncIterator[Utterance],
        *,
        session_id: str,
        glossary: Glossary | None = None,
    ) -> AsyncIterator[TranscriptEvent]:
        # The Live API documents no custom-vocabulary or prompt parameter for
        # transcription sessions, so a glossary cannot bias recognition here.
        # Say so once instead of silently ignoring the GM's toggle.
        if glossary_terms(glossary):
            log.warning("gemini.live.glossary_unsupported", provider=self.config.id)
        async for utterance in audio:
            event = await self._transcribe_one(utterance, session_id=session_id)
            if event is not None:
                yield event

    async def _transcribe_one(
        self, utterance: Utterance, *, session_id: str
    ) -> TranscriptEvent | None:
        state = _TurnState()
        loop = asyncio.get_running_loop()
        async with connect(self._session_url()) as ws:
            await ws.send(json.dumps(self._setup()))
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
        transcript = state.transcript()
        if not transcript:
            return None
        return TranscriptEvent(
            session_id=session_id,
            source=self.config.id,
            text=transcript,
            words=[],  # the Live API returns no word timings and no speakers
            speaker=None,
            start_ts=utterance.start,
            end_ts=utterance.end,
            is_final=True,
        )

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

    async def health(self) -> HealthReport:
        """Open the Live session and send the setup frame, without raising.

        A bad key fails the HTTP handshake before the socket upgrades, so the
        rejection arrives as a status code on the upgrade response and is
        graded like any other probe answer; a session that accepts the setup,
        or that simply stays quiet, is healthy.

        Note the key rides in the query string of the session URL here, which
        is why nothing in this path echoes the URL into a detail message.
        """
        try:
            async with asyncio.timeout(PROBE_TIMEOUT_S):
                async with connect(self._session_url()) as ws:
                    return await probe_health(ws, json.dumps(self._setup()))
        except TimeoutError:
            return HealthReport(HealthStatus.UNREACHABLE, "the socket did not open in time")
        except (OSError, WebSocketException) as exc:
            return classify_handshake_error(exc)

    async def aclose(self) -> None:
        """Nothing is held between utterances (one session per utterance)."""


def _audio_chunks(pcm: bytes, sample_rate: int) -> list[bytes]:
    """Split s16le PCM into the ~100 ms messages the docs prescribe."""
    step = max(2, sample_rate * 2 * _CHUNK_MS // _MS_PER_S)
    return [pcm[pos : pos + step] for pos in range(0, len(pcm), step)] or [pcm]


@register(ProviderKind.GEMINI, realtime=True)
def _factory(  # pyright: ignore[reportUnusedFunction]
    config: ProviderConfig, secrets: SecretStore, model: str | None
) -> GeminiLiveBackend:
    api_key = secrets.get(config.auth_ref) if config.auth_ref else None
    return GeminiLiveBackend(config, model=model, api_key=api_key)
