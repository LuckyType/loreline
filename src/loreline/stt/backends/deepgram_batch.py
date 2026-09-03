"""Deepgram batch STT connector (pre-recorded ``POST /v1/listen``).

UNVERIFIED: written from Deepgram's documentation and exercised only against a
mocked transport, never against the real API, because this environment has no
Deepgram key. Every model it unlocks is therefore ``hidden: true`` in
capabilities.yaml until someone runs it with a key; see the note above the
``whisper-large`` entry there for what flipping that gate requires.

Why it exists: Deepgram's hosted Whisper models are pre-recorded only ("Live
streaming is not available with Deepgram Whisper Cloud"), so with just the
WebSocket connector in ``deepgram.py`` there was no way to reach them at all and
capabilities.yaml could not honestly offer them. Nova and Flux are unaffected:
they stream, and the streaming connector serves re-processing too.

The wire shape is the streaming one turned inside out. Same host, same path,
same query parameters (shared in ``_deepgram.py``), same ``alternatives[]``
payload - but the audio goes in the request body as a whole file rather than as
socket frames, and the whole transcript comes back in one response instead of a
sequence of ``Results`` frames. Each voiced utterance is wrapped in a WAV
container and posted on its own, exactly as the OpenAI-compatible and Gemini
batch connectors do, so a WAV header carries the encoding and sample rate that
the streaming URL has to spell out.

Docs: https://developers.deepgram.com/reference/speech-to-text/listen-pre-recorded.md
      https://developers.deepgram.com/docs/pre-recorded-audio
      https://developers.deepgram.com/docs/deepgram-whisper-cloud
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from http import HTTPStatus

import httpx

from loreline.audio.chunker import Utterance
from loreline.audio.wav import pcm_to_wav
from loreline.logging import get_logger
from loreline.models import Glossary, ProviderConfig, ProviderKind, TranscriptEvent, Word
from loreline.secrets import SecretStore
from loreline.stt.backends._deepgram import auth_headers, listen_params, parse_alternative
from loreline.stt.backends._ws import as_list, as_obj_dict
from loreline.stt.base import error_detail, glossary_terms, http_base_url
from loreline.stt.registry import register

log = get_logger(__name__)

# Host only: the pre-recorded and streaming endpoints share the /v1/listen path
# and differ in scheme, which is why capabilities.yaml records the bare host as
# this provider's base_url.
_DEFAULT_BASE_URL = "https://api.deepgram.com"
_LISTEN_PATH = "/v1/listen"
# An utterance is at most VadChunker.max_utterance_s of audio (30 s by default),
# so a minute is already generous. Whisper Cloud is the slow case: Deepgram
# warns it is "less scalable than all other Deepgram models".
_TIMEOUT_S = 60.0


class DeepgramBatchBackend:
    """Pre-recorded transcription with inline diarization via Deepgram."""

    def __init__(
        self,
        config: ProviderConfig,
        *,
        model: str | None = None,
        client: httpx.AsyncClient | None = None,
        api_key: str | None = None,
        language: str | None = None,
    ) -> None:
        self.config = config
        self._language = language or config.language
        # No default model: which one to fall back to is a capability-config
        # question, not a connector constant, and omitting the parameter lets
        # Deepgram apply its own default rather than pinning one here.
        self._model = model
        base_url = http_base_url(config.base_url) or _DEFAULT_BASE_URL
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=base_url, headers=auth_headers(api_key), timeout=_TIMEOUT_S
        )

    async def transcribe(
        self,
        audio: AsyncIterator[Utterance],
        *,
        session_id: str,
        glossary: Glossary | None = None,
    ) -> AsyncIterator[TranscriptEvent]:
        # A tuple, not a list: httpx types repeated query parameters as an
        # immutable sequence, and this one is built once for the whole stream.
        params = tuple(
            listen_params(
                model=self._model,
                language=self._language,
                terms=glossary_terms(glossary),
                realtime=False,
            )
        )
        async for utterance in audio:
            event = await self._transcribe_one(params, utterance, session_id=session_id)
            if event is not None:
                yield event

    async def _transcribe_one(
        self, params: tuple[tuple[str, str], ...], utterance: Utterance, *, session_id: str
    ) -> TranscriptEvent | None:
        wav = pcm_to_wav(utterance.pcm, sample_rate=self.config.sample_rate)
        # Raw bytes in the body with the container's own media type, not
        # multipart and not the {"url": ...} JSON form, which is for audio
        # Deepgram must fetch itself.
        response = await self._client.post(
            _LISTEN_PATH,
            params=params,
            content=wav,
            headers={"Content-Type": "audio/wav"},
        )
        if response.status_code >= HTTPStatus.BAD_REQUEST:
            # Keep the body: Deepgram names the offending parameter there
            # ("keyterm is not supported for this model"), which the status line
            # never does, and that is usually the whole fix.
            raise httpx.HTTPStatusError(
                f"{response.status_code} from {response.request.url}: {error_detail(response)}",
                request=response.request,
                response=response,
            )
        return self._to_event(response.json(), utterance, session_id)

    def _to_event(
        self, payload: object, utterance: Utterance, session_id: str
    ) -> TranscriptEvent | None:
        """``results.channels[0].alternatives[0]`` into one final event.

        Only the first channel is read because only one is ever sent: the
        capture pipeline is mono and the WAV says so.
        """
        results = as_obj_dict(as_obj_dict(payload).get("results"))
        channels = as_list(results.get("channels"))
        alternatives = as_list(as_obj_dict(channels[0]).get("alternatives")) if channels else []
        if not alternatives:
            log.warning(
                "stt.deepgram_batch.no_alternatives",
                provider=self.config.name,
                provider_id=self.config.id,
            )
            return None
        text, words = parse_alternative(as_obj_dict(alternatives[0]), offset=utterance.start)
        if not text:
            return None
        return TranscriptEvent(
            session_id=session_id,
            source=self.config.id,
            text=text,
            words=words,
            speaker=_speaker(words),
            start_ts=utterance.start,
            end_ts=utterance.end,
            is_final=True,
        )

    async def health(self) -> bool:
        """Ask for the model list, which exercises the credential.

        https://developers.deepgram.com/reference/manage/models/list
        """
        try:
            response = await self._client.get("/v1/models")
        except httpx.HTTPError:
            return False
        return response.status_code < HTTPStatus.BAD_REQUEST

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()


def _speaker(words: list[Word]) -> str | None:
    """The utterance's dominant speaker, from its first labelled word - the
    convention every other connector here uses."""
    return next((w.speaker for w in words if w.speaker), None)


@register(ProviderKind.DEEPGRAM)
def _factory(  # pyright: ignore[reportUnusedFunction]
    config: ProviderConfig, secrets: SecretStore, model: str | None
) -> DeepgramBatchBackend:
    """Deepgram's pre-recorded models, chosen by the registry on the model.

    A config whose model streams (Nova, Flux) still goes to the WebSocket
    connector; only a batch-only model, or one curated batch-only, lands here.
    """
    api_key = secrets.get(config.auth_ref) if config.auth_ref else None
    return DeepgramBatchBackend(config, model=model, api_key=api_key)
