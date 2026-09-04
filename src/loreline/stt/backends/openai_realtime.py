"""OpenAI Realtime transcription connector (WebSocket).

OpenAI's Realtime API offers a transcription-only session (``type:
"transcription"``) that streams transcript deltas as audio arrives. We open a
session per voiced utterance, configure it for manual commit (no server VAD),
append the utterance's PCM as a single base64 chunk, commit the buffer, and emit
the final ``conversation.item.input_audio_transcription.completed`` transcript as
one ``TranscriptEvent``.

Which OpenAI transcription models reach this connector rather than the batch
one is decided per model in capabilities.yaml: gpt-live-transcribe and
gpt-realtime-whisper stream, while gpt-transcribe / gpt-4o-transcribe /
whisper-1 post through the ``openai_compat`` backend instead.

Docs:
- https://developers.openai.com/api/docs/guides/realtime-transcription
- https://developers.openai.com/api/docs/guides/realtime-websocket
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import WebSocketException

from loreline.audio.chunker import Utterance
from loreline.audio.resample import resample_pcm16
from loreline.capabilities import surface_for
from loreline.capability_config import TranscribeCapabilities
from loreline.logging import get_logger
from loreline.models import Glossary, Interaction, ProviderConfig, ProviderKind
from loreline.secrets import SecretStore
from loreline.stt.backends._ws import (
    as_dict,
    as_obj_dict,
    get_str,
)
from loreline.stt.base import Connector, Transcription, glossary_terms, secret_for
from loreline.stt.registry import register

log = get_logger(__name__)

_COMPLETED = "conversation.item.input_audio_transcription.completed"
_FAILED = "conversation.item.input_audio_transcription.failed"
# OpenAI Realtime rejects input sample rates below 24 kHz, while our capture
# pipeline is locked to 16 kHz by Silero VAD. Upsample to this rate on the way out.
_OUTPUT_RATE = 24_000
# OpenAI rejects a transcription prompt beyond this ("string_above_max_length"),
# and the rejection voids the whole session.update - language included.
_PROMPT_MAX_CHARS = 1024
_CONFIGURE_TIMEOUT_S = 10.0


def _capped_prompt(terms: list[str]) -> tuple[str | None, int]:
    """Join glossary terms into a prompt within OpenAI's length limit.

    Keeps whole leading terms only (glossary order is priority order); returns
    the prompt and how many trailing terms were dropped to fit.
    """
    kept: list[str] = []
    length = 0
    for term in terms:
        addition = len(term) + (2 if kept else 0)  # ", " separator
        if length + addition > _PROMPT_MAX_CHARS:
            break
        kept.append(term)
        length += addition
    return ", ".join(kept) or None, len(terms) - len(kept)


class OpenAIRealtimeBackend(Connector[None]):
    """Streaming transcription via an OpenAI Realtime transcription session.

    Nothing is prepared per call: the glossary prompt lives on the instance
    (``_prompt``) because the reusable socket's ``session.update`` reads it,
    so :meth:`prepare` sets it as a side effect and returns None.
    """

    def __init__(
        self,
        config: ProviderConfig,
        *,
        model: str | None = None,
        api_key: str | None = None,
        language: str | None = None,
    ) -> None:
        super().__init__(config)
        self._api_key = api_key
        self._language = language or config.language
        self._model = model
        self._endpoint = surface_for(config, Interaction.TRANSCRIBE, "realtime")
        self._url = self._endpoint.url
        self._out_rate = max(config.sample_rate, _OUTPUT_RATE)
        self._ws: ClientConnection | None = None
        self._prompt: str | None = None
        self._prompt_rejected = False  # model refused the prompt param; stop sending it

    @property
    def _headers(self) -> dict[str, str]:
        return self._endpoint.request_headers(self._api_key)

    def _session_update(self) -> str:
        transcription: dict[str, object] = {"language": self._language}
        # A transcription session with no model named runs OpenAI's own
        # default, which is the right thing to inherit when nobody chose.
        if self._model:
            transcription["model"] = self._model
        if self._prompt and not self._prompt_rejected:
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

    def prepare(self, glossary: Glossary | None) -> None:
        """Set the session prompt from the glossary; see the class docstring.

        Truncation is logged once per glossary rather than per utterance,
        which is what comparing against the prompt already set achieves.
        """
        prompt, dropped = _capped_prompt(glossary_terms(glossary))
        if dropped and prompt != self._prompt:
            log.warning(
                "openai.realtime.glossary_truncated",
                provider=self.config.id,
                dropped_terms=dropped,
                max_chars=_PROMPT_MAX_CHARS,
            )
        self._prompt = prompt

    async def _ensure_ws(self) -> ClientConnection:
        """Open + configure the transcription session, reusing it across calls.

        OpenAI's transcription session stays open and emits one completed event per
        ``input_audio_buffer.commit``, so a single connection serves the whole
        session - no fresh WebSocket handshake per utterance.
        """
        if self._ws is None:
            ws = await connect(self._url, additional_headers=self._headers)
            try:
                await self._configure(ws)
            except BaseException:
                with contextlib.suppress(Exception):
                    await ws.close()
                raise
            self._ws = ws
        return self._ws

    async def _configure(self, ws: ClientConnection) -> None:
        """Send ``session.update`` and wait until the server settles it.

        Draining the config handshake here keeps a rejection out of the
        per-utterance receive loop, where it would silently swallow an
        utterance's transcript (and count as success, so no fallback fires).
        A rejected prompt - a model without prompt support - downgrades the
        session once to a promptless update, so the language/format config
        still applies instead of being voided along with the prompt.
        """
        await ws.send(self._session_update())
        async with asyncio.timeout(_CONFIGURE_TIMEOUT_S):
            async for raw in ws:
                message = as_dict(raw)
                kind = get_str(message, "type")
                if kind.endswith("session.updated"):
                    return
                if kind != "error":
                    continue  # session.created and other chatter
                detail = as_obj_dict(message.get("error", message))
                if not self._prompt_rejected and "transcription.prompt" in get_str(detail, "param"):
                    self._prompt_rejected = True
                    log.warning(
                        "openai.realtime.prompt_rejected",
                        provider=self.config.id,
                        detail=detail,
                    )
                    await ws.send(self._session_update())
                    continue
                log.warning(
                    "openai.realtime.error",
                    provider=self.config.id,
                    event_type=kind,
                    detail=detail,
                )
                return

    async def _reset_ws(self) -> None:
        ws, self._ws = self._ws, None
        if ws is not None:
            with contextlib.suppress(Exception):
                await ws.close()

    async def transcribe_one(self, utterance: Utterance, prepared: None) -> Transcription:
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
        return Transcription(text=transcript)

    async def aclose(self) -> None:
        await self._reset_ws()


@register(ProviderKind.OPENAI, realtime=True)
def _factory(  # pyright: ignore[reportUnusedFunction]
    config: ProviderConfig,
    secrets: SecretStore,
    model: str | None,
    # Unused: this connector's prompt has no per-model ceiling in the yaml to
    # read, so the resolved capabilities say nothing it acts on.
    _caps: TranscribeCapabilities | None,
) -> OpenAIRealtimeBackend:
    return OpenAIRealtimeBackend(config, model=model, api_key=secret_for(config, secrets))
