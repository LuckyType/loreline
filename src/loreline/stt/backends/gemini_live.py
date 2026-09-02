"""Gemini Live API transcription connector (WebSocket, ``BidiGenerateContent``).

UNVERIFIED: this connector has never been run against the real service, only
against a mock that mirrors the documented protocol. The model it serves,
``gemini-3.5-transcribe-live``, is therefore hidden from the pickers until a
verification run with a real API key succeeds - see the gate in
``loreline.stt.catalog._CURATED``. A config that names the model explicitly
still reaches this connector, which is how that run is switched on.

Deliberately raw WebSocket rather than the ``google-genai`` SDK: this app
targets a Raspberry Pi and shed ``google-cloud-speech`` specifically to stay
light, the three existing streaming connectors (Deepgram, AssemblyAI, OpenAI
Realtime) already speak ``websockets`` directly, and only a raw connector can
be pointed at the local mock servers those connectors are tested against
(``config.base_url``). The cost is that the wire field names below are read
from Google's docs rather than encoded by the SDK, so they are exactly what
the verification run has to confirm.

Protocol: the client sends a ``setup`` message, then ``realtimeInput`` audio
chunks (raw 16-bit PCM, base64, ~100 ms each - which is precisely what the
capture pipeline produces at 16 kHz s16le, no resampling needed), then
``audioStreamEnd`` to flush. The server answers with ``serverContent`` frames
whose ``interimInputTranscription`` is the low-latency partial and
``inputTranscription`` the finalized text. One session per voiced utterance,
like the Deepgram and AssemblyAI connectors: ``audioStreamEnd`` is the only
documented "no more audio" signal, and a fresh session per utterance keeps a
late frame from poisoning the next utterance's reads.

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
from urllib.parse import urlencode

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosedOK, WebSocketException

from loreline.audio.chunker import Utterance
from loreline.logging import get_logger
from loreline.models import Glossary, ProviderConfig, ProviderKind, TranscriptEvent
from loreline.secrets import SecretStore
from loreline.stt.backends._ws import as_dict, as_obj_dict, get_bool, get_str, probe_health
from loreline.stt.base import glossary_terms
from loreline.stt.registry import register

log = get_logger(__name__)

_DEFAULT_URL = (
    "wss://generativelanguage.googleapis.com/ws/"
    "google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent"
)
_DEFAULT_MODEL = "gemini-3.5-transcribe-live"
# The documented chunk cadence; at 16 kHz s16le that is 3200 bytes per message.
_CHUNK_MS = 100
_MS_PER_S = 1000
# The server acks setup before it accepts audio (the SDK's connect() blocks on
# this ack too); a session that never acks is broken, so raising the timeout
# out of transcribe lets the router's failover take over.
_SETUP_TIMEOUT_S = 10.0
# Safety net per received frame. turnComplete is the expected end-of-flush
# marker, but the docs define none specifically for transcription-only
# sessions - if the real service ends turns differently, this bounds the
# stall per utterance instead of hanging the session.
_RECV_TIMEOUT_S = 10.0


def _wire(mapping: dict[str, object], name: str, alt: str) -> object:
    """Read a proto-JSON field by either spelling.

    Google's proto-JSON mapping emits lowerCamelCase, but nobody has seen this
    service's frames from this code yet - accepting the snake_case original
    too costs one dict lookup and halves the ways a guessed casing can lose
    the transcript.
    """
    value = mapping.get(name)
    return value if value is not None else mapping.get(alt)


class GeminiLiveBackend:
    """Streaming transcription (no diarization) via the Gemini Live API."""

    def __init__(
        self,
        config: ProviderConfig,
        *,
        api_key: str | None = None,
        language: str | None = None,
    ) -> None:
        self.config = config
        self._api_key = api_key
        self._language = language or config.language
        self._model = config.model or _DEFAULT_MODEL
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
        model = self._model if "/" in self._model else f"models/{self._model}"
        return {
            "setup": {
                "model": model,
                "generationConfig": {"responseModalities": ["TEXT"]},
                "inputAudioTranscription": transcription,
            }
        }

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
        parts: list[str] = []
        async with connect(self._session_url()) as ws:
            await ws.send(json.dumps(self._setup()))
            await self._await_setup_ack(ws)
            for chunk in _audio_chunks(utterance.pcm, self.config.sample_rate):
                await ws.send(json.dumps(self._audio_message(chunk)))
            await ws.send(json.dumps({"realtimeInput": {"audioStreamEnd": True}}))
            while True:
                try:
                    async with asyncio.timeout(_RECV_TIMEOUT_S):
                        raw = await ws.recv()
                except (TimeoutError, ConnectionClosedOK):
                    break
                content = as_obj_dict(_wire(as_dict(raw), "serverContent", "server_content"))
                final = as_obj_dict(_wire(content, "inputTranscription", "input_transcription"))
                # interimInputTranscription is deliberately unused: this app
                # emits one final event per utterance, and the finalized
                # fragments alone compose the whole transcript.
                text = get_str(final, "text")
                if text:
                    parts.append(text)
                if get_bool(content, "turnComplete") or get_bool(content, "turn_complete"):
                    break
        # Finalized fragments are pieces of one continuous transcript (they
        # carry their own spacing), so they concatenate rather than join.
        transcript = "".join(parts).strip()
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

    async def health(self) -> bool:
        # A bad key fails the HTTP handshake before the socket upgrades, which
        # connect() raises; a session that accepts the setup (or just stays
        # quiet) counts as reachable.
        try:
            async with connect(self._session_url()) as ws:
                return await probe_health(ws, json.dumps(self._setup()))
        except (OSError, WebSocketException):
            return False

    async def aclose(self) -> None:
        """Nothing is held between utterances (one session per utterance)."""


def _audio_chunks(pcm: bytes, sample_rate: int) -> list[bytes]:
    """Split s16le PCM into the ~100 ms messages the docs prescribe."""
    step = max(2, sample_rate * 2 * _CHUNK_MS // _MS_PER_S)
    return [pcm[pos : pos + step] for pos in range(0, len(pcm), step)] or [pcm]


@register(ProviderKind.GEMINI, realtime=True)
def _factory(  # pyright: ignore[reportUnusedFunction]
    config: ProviderConfig, secrets: SecretStore
) -> GeminiLiveBackend:
    api_key = secrets.get(config.auth_ref) if config.auth_ref else None
    return GeminiLiveBackend(config, api_key=api_key)
