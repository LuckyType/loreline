"""xAI batch STT connector (``POST /v1/stt``, multipart).

The streaming shape turned inside out. Same host, same path, same parameter
names (shared in ``_xai.py``), same ``words[]`` payload - but the audio goes in
the request body as a whole file rather than as socket frames, and the whole
transcript comes back in one response instead of a sequence of events. Each
voiced utterance is wrapped in a WAV container and posted on its own, exactly as
the Deepgram, OpenAI-compatible and Gemini batch connectors do, so the container
carries the encoding and sample rate that the streaming URL has to spell out.

Two details of this endpoint that the code has to honour and a reader would
otherwise trip over:

* ``file`` must be the LAST multipart field. httpx writes ``data`` fields before
  ``files``, so passing the parameters as ``data`` and the audio as ``files`` is
  what satisfies that, and a test pins the resulting field order rather than
  trusting the note.
* there is no ``model`` field, on this endpoint or the socket. Nothing is sent;
  see ``_xai.py`` and the grok-stt-1.0 entry in capabilities.yaml.

This connector serves re-processing: a stored recording arrives with
``prefer_batch``, which routes it here rather than through the socket that a
live capture uses.

UNVERIFIED: written from xAI's documentation and exercised only against
``mocks/xai_stt.py`` and a mocked transport, never against api.x.ai, because
this environment has no xAI key.

Docs: https://docs.x.ai/developers/model-capabilities/audio/speech-to-text
      https://docs.x.ai/developers/rest-api-reference/inference/voice
"""

from __future__ import annotations

import httpx

from loreline.audio.chunker import Utterance
from loreline.audio.wav import pcm_to_wav
from loreline.capabilities import surface_for
from loreline.capability_config import TranscribeCapabilities
from loreline.logging import get_logger
from loreline.models import Glossary, Interaction, ProviderConfig, ProviderKind
from loreline.secrets import SecretStore
from loreline.stt.backends._ws import as_obj_dict, get_str
from loreline.stt.backends._xai import parse_words, stt_params
from loreline.stt.base import HttpConnector, Transcription, glossary_terms, secret_for
from loreline.stt.registry import register

log = get_logger(__name__)

# The batch surface in capabilities.yaml is the API base, because chat and video
# live on it too; this is the transcription path on it.
_STT_PATH = "/stt"
# An utterance is at most VadChunker.max_utterance_s of audio (30 s by default),
# so a minute is already generous for a service whose documented ceiling is a
# 500 MB file.
_TIMEOUT_S = 60.0

# Multipart fields, in the order they go on the wire: every parameter, then the
# audio. A mapping rather than pairs because that is what httpx takes, with a
# list where the field repeats.
_Form = dict[str, str | list[str]]


class XaiBatchBackend(HttpConnector[_Form]):
    """Pre-recorded transcription with inline diarization via xAI.

    The prepared value is the multipart form minus the audio, which is added per
    utterance.
    """

    def __init__(
        self,
        config: ProviderConfig,
        *,
        model: str | None = None,
        caps: TranscribeCapabilities | None = None,
        client: httpx.AsyncClient | None = None,
        api_key: str | None = None,
        language: str | None = None,
    ) -> None:
        endpoint = surface_for(config, Interaction.TRANSCRIBE, "batch")
        super().__init__(
            config,
            client=client,
            base_url=endpoint.url,
            headers=endpoint.request_headers(api_key),
            timeout=_TIMEOUT_S,
        )
        self._language = language or config.language
        # Kept for the log line only: this endpoint takes no model parameter.
        self._model = model
        self._caps = caps

    def prepare(self, glossary: Glossary | None) -> _Form:
        return _as_form(
            stt_params(
                caps=self._caps,
                language=self._language,
                terms=glossary_terms(glossary),
                realtime=False,
            )
        )

    async def transcribe_one(self, utterance: Utterance, prepared: _Form) -> Transcription | None:
        wav = pcm_to_wav(utterance.pcm, sample_rate=self.config.sample_rate)
        response = await self._client.post(
            _STT_PATH,
            data=prepared,
            # Last field, which this endpoint requires: httpx writes every
            # `data` field before any `files` one. WAV is one of the containers
            # xAI auto-detects, so no audio_format/sample_rate pair is needed -
            # those are for raw pcm/mulaw/alaw bodies.
            files={"file": ("utterance.wav", wav, "audio/wav")},
        )
        # The body names the offending parameter, which is what the raise keeps.
        self._raise_for_status(response)
        return self._parse(response.json(), utterance)

    def _parse(self, payload: object, utterance: Utterance) -> Transcription | None:
        """``{"text": …, "words": [...]}`` into one transcription.

        ``channels[]`` is not read: it only appears under ``multichannel``,
        which this connector never asks for, because the capture pipeline is
        mono and the WAV header says so.
        """
        body = as_obj_dict(payload)
        if not body:
            log.warning(
                "stt.xai_batch.unexpected_payload",
                provider=self.config.name,
                provider_id=self.config.id,
            )
            return None
        return Transcription(
            text=get_str(body, "text"),
            words=parse_words(body, offset=utterance.start),
        )


def _as_form(params: list[tuple[str, str]]) -> _Form:
    """Name/value pairs as httpx's multipart form, repeats collected into lists.

    The pairs are the shape the socket's query string wants; this is the same
    values in the shape httpx's ``data`` wants, and ``keyterm`` is the one name
    that appears more than once.
    """
    form: _Form = {}
    for name, value in params:
        existing = form.get(name)
        if existing is None:
            form[name] = value
        elif isinstance(existing, list):
            existing.append(value)
        else:
            form[name] = [existing, value]
    return form


@register(ProviderKind.XAI)
def _factory(  # pyright: ignore[reportUnusedFunction]
    config: ProviderConfig,
    secrets: SecretStore,
    model: str | None,
    caps: TranscribeCapabilities | None,
) -> XaiBatchBackend:
    """xAI's transcription over the multipart endpoint.

    Reached when the caller asked for batch, which for this vendor means a
    re-processing job replaying a stored file: the one curated model prefers the
    socket, so a live capture goes to ``xai.py`` instead.
    """
    return XaiBatchBackend(config, model=model, caps=caps, api_key=secret_for(config, secrets))
