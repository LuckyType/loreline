"""xAI streaming STT connector (WebSocket).

xAI's live transcription API accepts raw PCM as binary WebSocket frames and
answers with ``transcript.partial`` events while audio is still arriving and one
``transcript.done`` once the client says the audio has ended. Each voiced
utterance is streamed on its own connection, closed with an ``audio.done``
control message, and the final transcript is emitted as one ``TranscriptEvent``.

The lifecycle, which is the whole reason this connector reads the way it does:

    (connect)                -> {"type": "transcript.created"}
    <raw PCM frames>
    {"type": "audio.done"}   -> {"type": "transcript.done", "text": …, "words": …}
                                and the service closes the socket.

``transcript.done`` is documented as "Final transcript after audio.done" and the
vendor's own worked example prints it as the full transcript of the session, so
this connector takes it as the whole utterance rather than stitching the
``transcript.partial`` events itself. Those are read anyway, but only as a
fallback for a ``transcript.done`` that arrives with no text: partials are
free to collect (they are already on the socket) and the alternative to keeping
them would be dropping an utterance whenever this reading of the docs is wrong.
Interim results stay off - one final event per utterance is the contract every
connector here satisfies, so half-formed text is not worth the frames.

UNVERIFIED: written from xAI's documentation and exercised only against
``mocks/xai_ws.py``, never against api.x.ai, because this environment has no xAI
key. See the note above the grok-stt-1.0 entry in capabilities.yaml for what a
maintainer with one should check.

Docs: https://docs.x.ai/developers/model-capabilities/audio/speech-to-text
"""

from __future__ import annotations

import asyncio
import json
from urllib.parse import urlencode

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosedOK

from loreline.audio.chunker import Utterance
from loreline.capabilities import surface_for
from loreline.capability_config import TranscribeCapabilities
from loreline.logging import get_logger
from loreline.models import Glossary, Interaction, ProviderConfig, ProviderKind, Word
from loreline.secrets import SecretStore
from loreline.stt.backends._ws import as_dict, get_bool, get_str
from loreline.stt.backends._xai import parse_words, stt_params
from loreline.stt.base import Connector, Transcription, glossary_terms, secret_for
from loreline.stt.registry import register

log = get_logger(__name__)

# Safety net per received frame. audio.done -> transcript.done is the real
# end-of-flush signal and the service closes straight after it; this only
# bounds a socket that goes quiet without either.
_RECV_TIMEOUT_S = 10.0

# What the capture pipeline sends: 16-bit little-endian PCM, one channel. The
# socket has no container to read this from, unlike the batch endpoint, so the
# query string has to spell it out.
_ENCODING = "pcm"


class XaiBackend(Connector[str]):
    """Streaming transcription with inline diarization via xAI.

    The prepared value is the socket URL with its query string built.
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
        # Kept for the log line only. This endpoint takes no model parameter,
        # so nothing downstream sends it; see _xai.py.
        self._model = model
        self._caps = caps
        self._endpoint = surface_for(config, Interaction.TRANSCRIBE, "realtime")
        self._url = self._endpoint.url

    def prepare(self, glossary: Glossary | None) -> str:
        params = stt_params(
            caps=self._caps,
            language=self._language,
            terms=glossary_terms(glossary),
            realtime=True,
        )
        # Streaming-only: the batch endpoint reads these from the container it
        # is posted, while a raw PCM socket has nothing to read them from.
        # `interim_results` is left at its default of false, so the partials
        # that do arrive are the ones already locked (is_final=true).
        params.extend(
            [
                ("encoding", _ENCODING),
                ("sample_rate", str(self.config.sample_rate)),
                ("channels", "1"),
            ]
        )
        return f"{self._url}?{urlencode(params)}"

    @property
    def _headers(self) -> dict[str, str]:
        return self._endpoint.request_headers(self._api_key)

    async def transcribe_one(self, utterance: Utterance, prepared: str) -> Transcription:
        # One connection per utterance, closed with audio.done: that is the
        # documented flush signal, and the service answers it with
        # transcript.done and hangs up. `Finalize` is the other control
        # message and is deliberately not used - it finalizes the current
        # utterance on a socket that stays open, which would leave this
        # connector reading a shared stream where a late frame lands in the
        # next utterance's window.
        text = ""
        words: list[Word] = []
        # The fallback described in the module docstring: locked partials, kept
        # only in case transcript.done arrives empty.
        partial_parts: list[str] = []
        partial_words: list[Word] = []
        async with connect(prepared, additional_headers=self._headers) as ws:
            await ws.send(utterance.pcm)
            await ws.send(json.dumps({"type": "audio.done"}))
            while True:
                try:
                    async with asyncio.timeout(_RECV_TIMEOUT_S):
                        raw = await ws.recv()
                except (TimeoutError, ConnectionClosedOK):
                    break
                message = as_dict(raw)
                kind = get_str(message, "type")
                if kind == "transcript.done":
                    text = get_str(message, "text")
                    words = parse_words(message, offset=utterance.start)
                    break
                if kind == "transcript.partial" and get_bool(message, "is_final"):
                    part = get_str(message, "text")
                    if part:
                        partial_parts.append(part)
                    partial_words.extend(parse_words(message, offset=utterance.start))
                elif kind == "error":
                    # The vendor's own words. Not raised: an utterance that
                    # produced no transcript is a None the router already
                    # handles, and failing the whole session over one rejected
                    # utterance is the harsher answer.
                    log.warning(
                        "stt.xai.error_frame",
                        provider=self.config.name,
                        provider_id=self.config.id,
                        detail=get_str(message, "message") or "no detail",
                    )
                    break
        if text:
            return Transcription(text=text, words=words)
        return Transcription(text=" ".join(partial_parts), words=partial_words)


@register(ProviderKind.XAI, realtime=True)
def _factory(  # pyright: ignore[reportUnusedFunction]
    config: ProviderConfig,
    secrets: SecretStore,
    model: str | None,
    caps: TranscribeCapabilities | None,
) -> XaiBackend:
    return XaiBackend(config, model=model, caps=caps, api_key=secret_for(config, secrets))
