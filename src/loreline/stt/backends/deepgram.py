"""Deepgram streaming STT connector (WebSocket).

Deepgram's live transcription API accepts raw linear16 PCM over a WebSocket and
returns ``Results`` messages with per-word speaker labels when ``diarize=true``
(inline diarization). Each voiced utterance is streamed on its own connection,
closed with a ``CloseStream`` control message, and the final transcript is
emitted as one ``TranscriptEvent``.

Docs: https://developers.deepgram.com/docs/streaming
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import AsyncIterator
from urllib.parse import urlencode

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import WebSocketException

from loreline.audio.chunker import Utterance
from loreline.logging import get_logger
from loreline.models import Glossary, ProviderConfig, ProviderKind, TranscriptEvent, Word
from loreline.secrets import SecretStore
from loreline.stt.backends._ws import (
    as_dict,
    as_list,
    as_obj_dict,
    get_bool,
    get_float,
    get_str,
    probe_health,
)
from loreline.stt.base import glossary_terms
from loreline.stt.registry import register

log = get_logger(__name__)

_DEFAULT_URL = "wss://api.deepgram.com/v1/listen"
_DEFAULT_MODEL = "nova-2"


class DeepgramBackend:
    """Streaming transcription with inline diarization via Deepgram."""

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
        self._ws: ClientConnection | None = None

    def _build_url(self, glossary: Glossary | None) -> str:
        params: list[tuple[str, str]] = [
            ("model", self._model),
            ("language", self._language),
            ("encoding", "linear16"),
            ("sample_rate", str(self.config.sample_rate)),
            ("channels", "1"),
            ("diarize", "true"),
            ("punctuate", "true"),
        ]
        params.extend(("keyterm", term) for term in glossary_terms(glossary))
        return f"{self._url}?{urlencode(params)}"

    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Token {self._api_key}"} if self._api_key else {}

    async def transcribe(
        self,
        audio: AsyncIterator[Utterance],
        *,
        session_id: str,
        glossary: Glossary | None = None,
    ) -> AsyncIterator[TranscriptEvent]:
        url = self._build_url(glossary)
        async for utterance in audio:
            event = await self._transcribe_one(url, utterance, session_id=session_id)
            if event is not None:
                yield event

    async def _ensure_ws(self, url: str) -> ClientConnection:
        """Open the live-transcription stream once and reuse it across utterances.

        Each utterance is flushed with a ``Finalize`` control message (which forces
        the final transcript but keeps the socket open), so the whole session runs
        on one connection instead of a fresh WebSocket handshake per utterance.
        """
        if self._ws is None:
            self._ws = await connect(url, additional_headers=self._headers)
        return self._ws

    async def _reset_ws(self) -> None:
        ws, self._ws = self._ws, None
        if ws is not None:
            with contextlib.suppress(Exception):
                await ws.close()

    async def _transcribe_one(
        self, url: str, utterance: Utterance, *, session_id: str
    ) -> TranscriptEvent | None:
        words: list[Word] = []
        transcript = ""
        try:
            ws = await self._ensure_ws(url)
            await ws.send(utterance.pcm)
            await ws.send(json.dumps({"type": "Finalize"}))
            async for raw in ws:
                message = as_dict(raw)
                kind = get_str(message, "type")
                if kind == "Results":
                    if get_bool(message, "is_final"):  # flushed final for this utterance
                        transcript, words = _parse_results(message, offset=utterance.start)
                        break
                elif kind in {"Metadata", "Close"}:
                    break
        except (OSError, WebSocketException):
            await self._reset_ws()  # drop the dead stream; the next utterance reconnects
            raise
        if not transcript:
            return None
        speaker = words[0].speaker if words else None
        return TranscriptEvent(
            session_id=session_id,
            source=self.config.id,
            text=transcript,
            words=words,
            speaker=speaker,
            start_ts=utterance.start,
            end_ts=utterance.end,
            is_final=True,
        )

    async def health(self) -> bool:
        try:
            async with connect(self._url, additional_headers=self._headers) as ws:
                return await probe_health(ws, json.dumps({"type": "CloseStream"}))
        except (OSError, WebSocketException):
            return False

    async def aclose(self) -> None:
        ws = self._ws
        if ws is not None:
            with contextlib.suppress(Exception):
                await ws.send(json.dumps({"type": "CloseStream"}))
        await self._reset_ws()


def _parse_results(message: dict[str, object], *, offset: float) -> tuple[str, list[Word]]:
    channel = as_obj_dict(message.get("channel"))
    alternatives = as_list(channel.get("alternatives"))
    if not alternatives:
        return "", []
    alt_map = as_obj_dict(alternatives[0])
    transcript = get_str(alt_map, "transcript")
    words: list[Word] = []
    for raw_word in as_list(alt_map.get("words")):
        word_map = as_obj_dict(raw_word)
        if not word_map:
            continue
        speaker_raw = word_map.get("speaker")
        speaker = f"Speaker {speaker_raw}" if speaker_raw is not None else None
        words.append(
            Word(
                text=get_str(word_map, "punctuated_word") or get_str(word_map, "word"),
                start=get_float(word_map, "start") + offset,
                end=get_float(word_map, "end") + offset,
                confidence=get_float(word_map, "confidence") or None,
                speaker=speaker,
            )
        )
    return transcript, words


@register(ProviderKind.DEEPGRAM)
def _factory(  # pyright: ignore[reportUnusedFunction]
    config: ProviderConfig, secrets: SecretStore
) -> DeepgramBackend:
    api_key = secrets.get(config.auth_ref) if config.auth_ref else None
    return DeepgramBackend(config, api_key=api_key)
