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

import httpx

from loreline.audio.chunker import Utterance
from loreline.audio.wav import pcm_to_wav
from loreline.health import HealthReport, probe_endpoint
from loreline.logging import get_logger
from loreline.models import Glossary, ProviderConfig, ProviderKind
from loreline.secrets import SecretStore
from loreline.stt.backends._deepgram import auth_headers, listen_params, parse_alternative
from loreline.stt.backends._ws import as_list, as_obj_dict
from loreline.stt.base import (
    HttpConnector,
    Transcription,
    glossary_terms,
    http_base_url,
    secret_for,
)
from loreline.stt.registry import register

log = get_logger(__name__)

# Host only: the pre-recorded and streaming endpoints share the /v1/listen path
# and differ in scheme, which is why capabilities.yaml records the bare host as
# this provider's base_url.
_DEFAULT_BASE_URL = "https://api.deepgram.com"
_LISTEN_PATH = "/v1/listen"
# The health probe. See ``health`` for why it is not the model list.
_AUTH_PROBE_PATH = "/v1/auth/token"
# An utterance is at most VadChunker.max_utterance_s of audio (30 s by default),
# so a minute is already generous. Whisper Cloud is the slow case: Deepgram
# warns it is "less scalable than all other Deepgram models".
_TIMEOUT_S = 60.0


# A tuple, not a list: httpx types repeated query parameters as an immutable
# sequence, and this one is built once for the whole stream.
_Params = tuple[tuple[str, str], ...]


class DeepgramBatchBackend(HttpConnector[_Params]):
    """Pre-recorded transcription with inline diarization via Deepgram.

    The prepared value is the ``/v1/listen`` query string as httpx params.
    """

    def __init__(
        self,
        config: ProviderConfig,
        *,
        model: str | None = None,
        client: httpx.AsyncClient | None = None,
        api_key: str | None = None,
        language: str | None = None,
    ) -> None:
        super().__init__(
            config,
            client=client,
            base_url=http_base_url(config.base_url) or _DEFAULT_BASE_URL,
            headers=auth_headers(api_key),
            timeout=_TIMEOUT_S,
        )
        self._language = language or config.language
        # No default model: which one to fall back to is a capability-config
        # question, not a connector constant, and omitting the parameter lets
        # Deepgram apply its own default rather than pinning one here.
        self._model = model

    def prepare(self, glossary: Glossary | None) -> _Params:
        return tuple(
            listen_params(
                model=self._model,
                language=self._language,
                terms=glossary_terms(glossary),
                realtime=False,
            )
        )

    async def transcribe_one(self, utterance: Utterance, prepared: _Params) -> Transcription | None:
        wav = pcm_to_wav(utterance.pcm, sample_rate=self.config.sample_rate)
        # Raw bytes in the body with the container's own media type, not
        # multipart and not the {"url": ...} JSON form, which is for audio
        # Deepgram must fetch itself.
        response = await self._client.post(
            _LISTEN_PATH,
            params=prepared,
            content=wav,
            headers={"Content-Type": "audio/wav"},
        )
        # Deepgram names the offending parameter in the body ("keyterm is not
        # supported for this model"), which is what the raise keeps.
        self._raise_for_status(response)
        return self._parse(response.json(), utterance)

    def _parse(self, payload: object, utterance: Utterance) -> Transcription | None:
        """``results.channels[0].alternatives[0]`` into one transcription.

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
        return Transcription(text=text, words=words)

    async def health(self) -> HealthReport:
        """Ask what the calling key is, which is the only thing that tests it.

        Not the model list, which is what this connector shipped with: verified
        live, ``GET /v1/models`` answers **200 with the full catalogue and no
        Authorization header at all**, so grading it proved that api.deepgram.com
        was up and nothing whatsoever about the credential. Every key, valid or
        garbage, came back healthy - the same defect the LLM probe had.

        ``/v1/auth/token`` describes the key that called it and answers **401
        "Invalid credentials."** (as plain text, which the grading falls back to
        reading) for a bad or absent one. Verified live for the failure half;
        the 200 half is UNVERIFIED, in keeping with this connector's banner,
        since this environment has no Deepgram key. If that endpoint turns out
        to need a scope a plain key lacks, the answer grades as UNAUTHORIZED or
        UNKNOWN rather than as a false "down", and swapping the path is a
        one-line fix.
        https://developers.deepgram.com/reference/token-based-auth-api/grant-token
        """
        return await probe_endpoint(self._client, _AUTH_PROBE_PATH)


@register(ProviderKind.DEEPGRAM)
def _factory(  # pyright: ignore[reportUnusedFunction]
    config: ProviderConfig, secrets: SecretStore, model: str | None
) -> DeepgramBatchBackend:
    """Deepgram's pre-recorded models, chosen by the registry on the model.

    A config whose model streams (Nova, Flux) still goes to the WebSocket
    connector; only a batch-only model, or one curated batch-only, lands here.
    """
    return DeepgramBatchBackend(config, model=model, api_key=secret_for(config, secrets))
