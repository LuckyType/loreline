"""AssemblyAI streaming STT connector (WebSocket, Universal-Streaming v3).

AssemblyAI's v3 streaming endpoint accepts raw PCM (s16le) over a WebSocket and
returns ``Turn`` messages containing the running transcript and per-word data;
a turn with ``end_of_turn=true`` marks a completed segment. Each utterance is
streamed and terminated, and the final formatted turn is emitted as one
``TranscriptEvent``.

Docs: https://www.assemblyai.com/docs/speech-to-text/universal-streaming
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

_DEFAULT_URL = "wss://streaming.assemblyai.com/v3/ws"
_MS_PER_S = 1000.0


class AssemblyAIBackend:
    """Streaming transcription with inline diarization via AssemblyAI v3."""

    def __init__(self, config: ProviderConfig, *, api_key: str | None = None) -> None:
        self.config = config
        self._api_key = api_key
        self._language = config.language
        self._url = config.base_url or _DEFAULT_URL
        self._ws: ClientConnection | None = None

    def _build_url(self, glossary: Glossary | None) -> str:
        params: list[tuple[str, str]] = [
            ("sample_rate", str(self.config.sample_rate)),
            ("encoding", "pcm_s16le"),
            ("format_turns", "true"),
            ("language", self._language),
        ]
        terms = glossary_terms(glossary)
        if terms:
            params.append(("keyterms_prompt", json.dumps(terms)))
        return f"{self._url}?{urlencode(params)}"

    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": self._api_key} if self._api_key else {}

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
        """Open the streaming session once and reuse it across utterances.

        Each utterance is closed out with a ``ForceEndpoint`` control message
        (which ends the current turn but keeps the session open), so the whole
        session runs on one connection rather than reconnecting per utterance.
        ``Terminate`` is reserved for ``aclose`` (it ends the billable session).
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
        transcript = ""
        words: list[Word] = []
        try:
            ws = await self._ensure_ws(url)
            await ws.send(utterance.pcm)
            await ws.send(json.dumps({"type": "ForceEndpoint"}))
            async for raw in ws:
                message = as_dict(raw)
                kind = get_str(message, "type")
                if kind == "Turn" and get_bool(message, "end_of_turn"):
                    transcript = get_str(message, "transcript")
                    words = _parse_words(message, offset=utterance.start)
                    break
                if kind == "Termination":
                    break
        except (OSError, WebSocketException):
            await self._reset_ws()  # drop the dead session; the next utterance reconnects
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
                return await probe_health(ws, None)  # server greets with a Begin frame
        except (OSError, WebSocketException):
            return False

    async def aclose(self) -> None:
        ws = self._ws
        if ws is not None:
            with contextlib.suppress(Exception):
                await ws.send(json.dumps({"type": "Terminate"}))
        await self._reset_ws()


def _parse_words(message: dict[str, object], *, offset: float) -> list[Word]:
    words: list[Word] = []
    for raw_word in as_list(message.get("words")):
        word_map = as_obj_dict(raw_word)
        if not word_map:
            continue
        speaker_raw = word_map.get("speaker")
        speaker = f"Speaker {speaker_raw}" if speaker_raw is not None else None
        words.append(
            Word(
                text=get_str(word_map, "text"),
                start=get_float(word_map, "start") / _MS_PER_S + offset,
                end=get_float(word_map, "end") / _MS_PER_S + offset,
                confidence=get_float(word_map, "confidence") or None,
                speaker=speaker,
            )
        )
    return words


@register(ProviderKind.ASSEMBLYAI)
def _factory(  # pyright: ignore[reportUnusedFunction]
    config: ProviderConfig, secrets: SecretStore
) -> AssemblyAIBackend:
    api_key = secrets.get(config.auth_ref) if config.auth_ref else None
    return AssemblyAIBackend(config, api_key=api_key)
