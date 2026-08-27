"""OpenAI Realtime transcription connector (WebSocket).

OpenAI's Realtime API offers a transcription-only session (``type:
"transcription"``) that streams transcript deltas as audio arrives. We open a
session per voiced utterance, configure it for manual commit (no server VAD),
append the utterance's PCM as a single base64 chunk, commit the buffer, and emit
the final ``conversation.item.input_audio_transcription.completed`` transcript as
one ``TranscriptEvent``.

The default model is ``gpt-realtime-whisper`` (the natively-streaming
transcription model). ``gpt-4o-transcribe`` / ``gpt-4o-mini-transcribe`` /
``whisper-1`` also work for non-streaming workflows via the batch
``openai_compat`` backend instead.

Docs:
- https://developers.openai.com/api/docs/guides/realtime-transcription
- https://developers.openai.com/api/docs/guides/realtime-websocket
"""

from __future__ import annotations

import base64
import contextlib
import json
from collections.abc import AsyncIterator

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import WebSocketException

from loreline.audio.chunker import Utterance
from loreline.audio.resample import resample_pcm16
from loreline.logging import get_logger
from loreline.models import Glossary, ProviderConfig, ProviderKind, TranscriptEvent
from loreline.secrets import SecretStore
from loreline.stt.backends._ws import as_dict, get_str, probe_health
from loreline.stt.base import glossary_terms
from loreline.stt.registry import register

log = get_logger(__name__)

_DEFAULT_URL = "wss://api.openai.com/v1/realtime?intent=transcription"
_DEFAULT_MODEL = "gpt-realtime-whisper"
_COMPLETED = "conversation.item.input_audio_transcription.completed"
_FAILED = "conversation.item.input_audio_transcription.failed"
# OpenAI Realtime rejects input sample rates below 24 kHz, while our capture
# pipeline is locked to 16 kHz by Silero VAD. Upsample to this rate on the way out.
_OUTPUT_RATE = 24_000


class OpenAIRealtimeBackend:
    """Streaming transcription via an OpenAI Realtime transcription session."""

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
        self._out_rate = max(config.sample_rate, _OUTPUT_RATE)
        self._ws: ClientConnection | None = None
        self._prompt: str | None = None

    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}

    def _session_update(self) -> str:
        transcription: dict[str, object] = {
            "model": self._model,
            "language": self._language,
        }
        if self._prompt:
            # Bias recognition toward the campaign glossary (spell/char/place names).
            transcription["prompt"] = self._prompt
        return json.dumps(
            {
                "type": "session.update",
                "session": {
                    "type": "transcription",
                    "audio": {
                        "input": {
                            "format": {
                                "type": "audio/pcm",
                                "rate": self._out_rate,
                            },
                            "transcription": transcription,
                            "turn_detection": None,
                        }
                    },
                },
            }
        )

    async def transcribe(
        self,
        audio: AsyncIterator[Utterance],
        *,
        session_id: str,
        glossary: Glossary | None = None,
    ) -> AsyncIterator[TranscriptEvent]:
        self._prompt = ", ".join(glossary_terms(glossary)) or None
        async for utterance in audio:
            event = await self._transcribe_one(utterance, session_id=session_id)
            if event is not None:
                yield event

    async def _ensure_ws(self) -> ClientConnection:
        """Open + configure the transcription session, reusing it across calls.

        OpenAI's transcription session stays open and emits one completed event per
        ``input_audio_buffer.commit``, so a single connection serves the whole
        session - no fresh WebSocket handshake per utterance.
        """
        if self._ws is None:
            ws = await connect(self._url, additional_headers=self._headers)
            await ws.send(self._session_update())
            self._ws = ws
        return self._ws

    async def _reset_ws(self) -> None:
        ws, self._ws = self._ws, None
        if ws is not None:
            with contextlib.suppress(Exception):
                await ws.close()

    async def _transcribe_one(
        self, utterance: Utterance, *, session_id: str
    ) -> TranscriptEvent | None:
        pcm = resample_pcm16(utterance.pcm, self.config.sample_rate, self._out_rate)
        transcript = ""
        try:
            ws = await self._ensure_ws()
            await ws.send(
                json.dumps(
                    {
                        "type": "input_audio_buffer.append",
                        "audio": base64.b64encode(pcm).decode("ascii"),
                    }
                )
            )
            await ws.send(json.dumps({"type": "input_audio_buffer.commit"}))
            async for raw in ws:
                message = as_dict(raw)
                kind = get_str(message, "type")
                if kind == _COMPLETED:
                    transcript = get_str(message, "transcript")
                    break
                if kind in {_FAILED, "error"}:
                    detail = message.get("error", message)
                    log.warning(
                        "openai.realtime.error",
                        provider=self.config.id,
                        event_type=kind,
                        detail=detail,
                    )
                    break
        except (OSError, WebSocketException):
            await self._reset_ws()  # drop the dead session; the next utterance reconnects
            raise
        if not transcript:
            return None
        return TranscriptEvent(
            session_id=session_id,
            source=self.config.id,
            text=transcript,
            words=[],
            speaker=None,
            start_ts=utterance.start,
            end_ts=utterance.end,
            is_final=True,
        )

    async def health(self) -> bool:
        try:
            async with connect(self._url, additional_headers=self._headers) as ws:
                return await probe_health(ws, self._session_update())
        except (OSError, WebSocketException):
            return False

    async def aclose(self) -> None:
        await self._reset_ws()


@register(ProviderKind.OPENAI)
def _factory(  # pyright: ignore[reportUnusedFunction]
    config: ProviderConfig, secrets: SecretStore
) -> OpenAIRealtimeBackend:
    api_key = secrets.get(config.auth_ref) if config.auth_ref else None
    return OpenAIRealtimeBackend(config, api_key=api_key)
