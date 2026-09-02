"""AssemblyAI streaming STT connector (WebSocket, Universal-Streaming v3).

Speaker labels are requested via ``speaker_labels=true``; each final word in a
Turn then carries a ``speaker`` field ("A", "B", or "PENDING" when the model
has too little audio to attribute it). Supported on all three streaming models.
Docs: https://www.assemblyai.com/docs/streaming/label-speakers-and-separate-channels

AssemblyAI's v3 streaming endpoint accepts raw PCM (s16le) over a WebSocket and
returns ``Turn`` messages containing the running transcript and per-word data;
a turn with ``end_of_turn=true`` marks a completed segment. Each utterance is
streamed on its own session, closed with ``Terminate``, and its completed
turns are emitted as one ``TranscriptEvent``.

Docs: https://www.assemblyai.com/docs/speech-to-text/universal-streaming
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

_DEFAULT_URL = "wss://streaming.assemblyai.com/v3/ws"
_MS_PER_S = 1000.0
# The v3 endpoint rejects any single audio message outside 50-1000 ms (close
# code 3007), so utterances are re-chunked before sending.
_CHUNK_MS = 800
_MIN_CHUNK_MS = 50
# Safety net per received frame; the protocol's own Termination reply is the
# real end-of-flush signal, and partial-turn updates keep arriving every few
# seconds while the server is still transcribing, so a healthy session never
# goes quiet this long.
_RECV_TIMEOUT_S = 15.0


class AssemblyAIBackend:
    """Streaming transcription with inline diarization via AssemblyAI v3."""

    def __init__(self, config: ProviderConfig, *, api_key: str | None = None) -> None:
        self.config = config
        self._api_key = api_key
        self._language = config.language
        self._url = config.base_url or _DEFAULT_URL

    def _build_url(self, glossary: Glossary | None) -> str:
        params: list[tuple[str, str]] = [
            ("sample_rate", str(self.config.sample_rate)),
            ("encoding", "pcm_s16le"),
            ("format_turns", "true"),
            ("language", self._language),
        ]
        # Sent only when the GM picked one: omitted, the endpoint applies its
        # own current default (universal-3-5-pro), which is a better thing to
        # inherit than a value pinned here. Until this was wired the model
        # picker had no effect at all on what AssemblyAI ran.
        if self.config.model:
            params.append(("speech_model", self.config.model))
        # Requested unconditionally, matching the Deepgram connector: the
        # backend always asks for speakers and the router decides whether to
        # use them (see stt/router.py's DiarizationMode.INLINE branch), so the
        # words already carry labels whichever mode the session ends up in.
        # Note AssemblyAI bills streaming diarization as a paid add-on.
        params.append(("speaker_labels", "true"))
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

    async def _transcribe_one(
        self, url: str, utterance: Utterance, *, session_id: str
    ) -> TranscriptEvent | None:
        # One session per utterance, closed with Terminate: that is the only
        # flush signal the v3 protocol defines - the server transcribes
        # everything it received (possibly as several end-of-turn messages),
        # then answers with Termination. (Reusing a session and force-
        # endpointing instead leaves no way to tell "flush done" from "still
        # transcribing": real update gaps run 2-3 s, so any quiet-gap
        # heuristic either drops utterance tails or stalls every utterance.)
        # With format_turns the server may resend a turn it already ended as
        # a formatted duplicate, so keep the last message per turn_order.
        turns: dict[int, tuple[str, list[Word]]] = {}
        async with connect(url, additional_headers=self._headers) as ws:
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
                        _parse_words(message, offset=utterance.start),
                    )
                elif kind == "Termination":
                    break
        ordered = [turns[order] for order in sorted(turns)]
        transcript = " ".join(text for text, _ in ordered if text)
        words = [word for _, turn_words in ordered for word in turn_words]
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
        """Nothing is held between utterances (one session per utterance)."""


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


@register(ProviderKind.ASSEMBLYAI, realtime=True)
def _factory(  # pyright: ignore[reportUnusedFunction]
    config: ProviderConfig, secrets: SecretStore
) -> AssemblyAIBackend:
    api_key = secrets.get(config.auth_ref) if config.auth_ref else None
    return AssemblyAIBackend(config, api_key=api_key)
