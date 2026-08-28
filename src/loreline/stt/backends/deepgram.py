"""Deepgram streaming STT connector (WebSocket).

Deepgram's live transcription API accepts raw linear16 PCM over a WebSocket and
returns ``Results`` messages with per-word speaker labels when ``diarize=true``
(inline diarization). Each voiced utterance is streamed on its own connection,
closed with a ``CloseStream`` control message, and the final transcript is
emitted as one ``TranscriptEvent``.

Docs: https://developers.deepgram.com/docs/streaming
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from urllib.parse import urlencode

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosedOK, WebSocketException

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
# Safety net per received frame; CloseStream -> Metadata is the real
# end-of-flush signal, and results stream back within a couple of seconds.
_RECV_TIMEOUT_S = 10.0


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

    async def _transcribe_one(
        self, url: str, utterance: Utterance, *, session_id: str
    ) -> TranscriptEvent | None:
        # One connection per utterance, closed with CloseStream: that is the
        # only flush signal Deepgram defines unconditionally - the server
        # finalizes buffered audio, streams the remaining final Results, then
        # Metadata and a clean close. (A shared stream flushed with Finalize
        # has no such guarantee: the from_finalize ack is conditional and can
        # arrive seconds late, where it poisons the next utterance's reads.)
        # Deepgram's own endpointing splits one utterance into several final
        # Results frames, and the first is routinely a near-silent lead-in
        # with an empty transcript - accumulate them all, not just the first.
        parts: list[str] = []
        words: list[Word] = []
        async with connect(url, additional_headers=self._headers) as ws:
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
        transcript = " ".join(parts)
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
        """Nothing is held between utterances (one connection per utterance)."""


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
