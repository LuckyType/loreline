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
from urllib.parse import urlencode

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosedOK, WebSocketException

from loreline.audio.chunker import Utterance
from loreline.capabilities import surface_for
from loreline.health import PROBE_TIMEOUT_S, HealthReport, HealthStatus
from loreline.logging import get_logger
from loreline.models import Glossary, Interaction, ProviderConfig, ProviderKind, Word
from loreline.secrets import SecretStore
from loreline.stt.backends._deepgram import listen_params, parse_alternative
from loreline.stt.backends._ws import (
    as_dict,
    as_list,
    as_obj_dict,
    classify_handshake_error,
    get_bool,
    get_str,
    probe_health,
)
from loreline.stt.base import Connector, Transcription, glossary_terms, secret_for
from loreline.stt.registry import register

log = get_logger(__name__)

# Safety net per received frame; CloseStream -> Metadata is the real
# end-of-flush signal, and results stream back within a couple of seconds.
_RECV_TIMEOUT_S = 10.0


class DeepgramBackend(Connector[str]):
    """Streaming transcription with inline diarization via Deepgram.

    The prepared value is the socket URL with its query string built.
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

    def prepare(self, glossary: Glossary | None) -> str:
        params = listen_params(
            model=self._model,
            language=self._language,
            terms=glossary_terms(glossary),
            realtime=True,
        )
        # Streaming-only: the batch endpoint reads these from the WAV header,
        # while a raw PCM socket has no container to read them from.
        params.extend(
            [
                ("encoding", "linear16"),
                ("sample_rate", str(self.config.sample_rate)),
                ("channels", "1"),
            ]
        )
        return f"{self._url}?{urlencode(params)}"

    @property
    def _headers(self) -> dict[str, str]:
        return self._endpoint.request_headers(self._api_key)

    async def transcribe_one(self, utterance: Utterance, prepared: str) -> Transcription:
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
        async with connect(prepared, additional_headers=self._headers) as ws:
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
        return Transcription(text=" ".join(parts), words=words)

    async def health(self) -> HealthReport:
        """Open the socket and read one frame, without raising.

        A rejected upgrade is a plain HTTP response, so a bad key surfaces as a
        status code on the handshake and grades exactly like an HTTP probe -
        which is what makes "wrong key" distinguishable from "wrong host" here
        at all. Before, both were a bare False.
        """
        try:
            async with asyncio.timeout(PROBE_TIMEOUT_S):
                async with connect(self._url, additional_headers=self._headers) as ws:
                    return await probe_health(ws, json.dumps({"type": "CloseStream"}))
        except TimeoutError:
            return HealthReport(HealthStatus.UNREACHABLE, "the socket did not open in time")
        except (OSError, WebSocketException) as exc:
            return classify_handshake_error(exc)


def _parse_results(message: dict[str, object], *, offset: float) -> tuple[str, list[Word]]:
    """A streaming ``Results`` frame: one channel, its first alternative."""
    channel = as_obj_dict(message.get("channel"))
    alternatives = as_list(channel.get("alternatives"))
    if not alternatives:
        return "", []
    return parse_alternative(as_obj_dict(alternatives[0]), offset=offset)


@register(ProviderKind.DEEPGRAM, realtime=True)
def _factory(  # pyright: ignore[reportUnusedFunction]
    config: ProviderConfig, secrets: SecretStore, model: str | None
) -> DeepgramBackend:
    return DeepgramBackend(config, model=model, api_key=secret_for(config, secrets))
