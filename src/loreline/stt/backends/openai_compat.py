"""OpenAI-compatible STT backend.

Covers OpenAI's ``/v1/audio/transcriptions`` and any compatible self-hosted
endpoint (Speaches, whisper.cpp server), plus OpenRouter's transcription
gateway. Each voiced utterance is wrapped in a WAV container and POSTed as
multipart. Glossary terms are passed via the ``prompt`` field.

``verbose_json`` is requested rather than ``json`` so the response carries
per-word timings and - where the model produces them - speaker labels, which
plain ``json`` throws away by returning only ``text``. Not every compatible
server implements it, so a rejection downgrades that *one backend instance* to
plain ``json`` for the rest of its life rather than failing the utterance or
paying a retry on every one after (see ``_verbose_json``).
"""

from __future__ import annotations

from http import HTTPStatus
from typing import cast

import httpx

from loreline.audio.chunker import Utterance
from loreline.audio.wav import pcm_to_wav
from loreline.capabilities import surface_for
from loreline.health import HealthReport, probe_endpoint
from loreline.logging import get_logger
from loreline.models import Glossary, Interaction, ProviderConfig, ProviderKind, Word
from loreline.secrets import SecretStore
from loreline.stt.base import HttpConnector, Transcription, glossary_terms, secret_for
from loreline.stt.registry import register

log = get_logger(__name__)

# The health probe. Free, implemented by every OpenAI-compatible server, and on
# OpenAI cloud it 401s a bad key. A kind whose /models is public declares a
# different path on its surface (OpenRouter's /key), because grading a public
# list says nothing about a key.
_DEFAULT_HEALTH_PATH = "/models"


class OpenAICompatBackend(HttpConnector[str | None]):
    """Batch transcription against an OpenAI-compatible HTTP endpoint.

    The prepared value is the glossary prompt, or None without a glossary.
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
        """Three kinds share this wire format (OpenAI cloud, OpenRouter, the
        self-hosted kind), and everything that differs between them - the
        base, the attribution headers, the probe path - is their batch
        transcription surface in capabilities.yaml, so nothing here is per
        kind.

        ``model`` may be None, and this is the one connector where that is a
        normal state rather than a missing default: it also serves the
        self-hosted kind, whose catalogue is whatever the operator installed,
        so capabilities.yaml curates no models for it and can vouch for none.
        The field is then left out and the server transcribes with its own."""
        endpoint = surface_for(config, Interaction.TRANSCRIBE, "batch")
        super().__init__(
            config,
            client=client,
            base_url=endpoint.url,
            headers=endpoint.request_headers(api_key),
            timeout=60.0,
        )
        self._language = language or config.language
        self._model = model
        self._health_path = endpoint.health or _DEFAULT_HEALTH_PATH
        # None until the first response tells us whether this endpoint honours
        # verbose_json; False pins it to plain json from then on.
        self._verbose_json: bool | None = None

    def prepare(self, glossary: Glossary | None) -> str | None:
        return ", ".join(glossary_terms(glossary)) or None

    async def transcribe_one(self, utterance: Utterance, prepared: str | None) -> Transcription:
        prompt = prepared
        wav = pcm_to_wav(utterance.pcm, sample_rate=self.config.sample_rate)
        response = await self._post(wav, prompt=prompt, verbose=self._verbose_json is not False)
        if response.status_code == HTTPStatus.BAD_REQUEST and self._verbose_json is None:
            # This endpoint does not do verbose_json. Remember it and fall back
            # once, rather than rejecting the utterance over a response format
            # the GM never asked for.
            log.info("stt.openai_compat.verbose_json_unsupported", provider_id=self.config.id)
            self._verbose_json = False
            response = await self._post(wav, prompt=prompt, verbose=False)
        self._raise_for_status(response)
        payload: object = response.json()
        if not isinstance(payload, dict):
            log.warning(
                "stt.openai_compat.unexpected_payload",
                provider=self.config.name,
                provider_id=self.config.id,
            )
            return Transcription("", [])
        mapping = cast("dict[str, object]", payload)
        text = mapping.get("text")
        if self._verbose_json is None:
            # A body carrying words/segments proves the format took effect.
            self._verbose_json = "words" in mapping or "segments" in mapping
        return Transcription(
            text=text.strip() if isinstance(text, str) else "",
            words=_parse_words(mapping, offset=utterance.start),
        )

    async def _post(self, wav: bytes, *, prompt: str | None, verbose: bool) -> httpx.Response:
        data: dict[str, object] = {
            "language": self._language,
            "response_format": "verbose_json" if verbose else "json",
        }
        # Omitted when none was resolved, which happens only for the
        # self-hosted kind: a server with a single model loaded transcribes
        # with it regardless, and naming a model it does not have would fail a
        # request that otherwise works.
        if self._model:
            data["model"] = self._model
        if verbose:
            # Repeated form field, per the OpenAI multipart convention. Without
            # it a verbose_json body carries segments but no `words` array.
            data["timestamp_granularities[]"] = ["word", "segment"]
        if prompt:
            data["prompt"] = prompt
        files = {"file": ("utterance.wav", wav, "audio/wav")}
        return await self._client.post("/audio/transcriptions", data=data, files=files)

    async def health(self) -> HealthReport:
        """Probe whichever cheap read this kind's endpoint answers honestly.

        ``/models`` by default, which is free and implemented everywhere. This
        class serves three kinds at once, so the grading also has to cope with
        a self-hosted server that checks no key and answers 200 - which is the
        honest verdict there, since ``auth: optional`` in capabilities.yaml
        means there may be no credential to get wrong in the first place.
        """
        return await probe_endpoint(self._client, self._health_path)


@register(ProviderKind.OPENAI_COMPAT)
def _factory(  # pyright: ignore[reportUnusedFunction]
    config: ProviderConfig, secrets: SecretStore, model: str | None
) -> OpenAICompatBackend:
    return OpenAICompatBackend(config, model=model, api_key=secret_for(config, secrets))


@register(ProviderKind.OPENAI)
def _openai_batch_factory(  # pyright: ignore[reportUnusedFunction]
    config: ProviderConfig, secrets: SecretStore, model: str | None
) -> OpenAICompatBackend:
    """OpenAI cloud's batch transcription models (whisper-1, gpt-transcribe).

    The registry routes an OPENAI config here when its model is not one of the
    Realtime ones, so one stored provider covers both transports. The config's
    base_url never reaches this connector: for this kind it has always meant
    the Realtime WebSocket endpoint, which the batch API cannot live at, so
    the batch surface is declared non-overridable and an operator who wants a
    custom batch endpoint has the OPENAI_COMPAT kind for exactly that.
    """
    return OpenAICompatBackend(config, model=model, api_key=secret_for(config, secrets))


def _speaker_label(raw: object) -> str | None:
    """Speaker indices come back as integers (0, 1, …); render them the way the
    other connectors do so labels are comparable across providers."""
    if isinstance(raw, bool) or raw is None:
        return None
    if isinstance(raw, int):
        return f"Speaker {raw}"
    if isinstance(raw, str) and raw.strip():
        return raw if raw.startswith("Speaker") else f"Speaker {raw}"
    return None


def _parse_words(payload: dict[str, object], *, offset: float) -> list[Word]:
    """Per-word timings and speakers out of a verbose_json body.

    Word timings are clip-relative, so they are shifted by the utterance's own
    start to land on the session clock - the same correction every other
    connector applies.

    Falls back to segment-level rows when the response carries segments but no
    words: coarser, but it still preserves speaker changes, which is the part
    that matters for diarization. Returns [] for a plain ``json`` body, leaving
    the event exactly as it was before this existed.
    """
    raw_words = payload.get("words")
    if isinstance(raw_words, list) and raw_words:
        return _rows(cast("list[object]", raw_words), offset=offset, text_key="word")
    raw_segments = payload.get("segments")
    if isinstance(raw_segments, list):
        return _rows(cast("list[object]", raw_segments), offset=offset, text_key="text")
    return []


def _rows(rows: list[object], *, offset: float, text_key: str) -> list[Word]:
    words: list[Word] = []
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        row = cast("dict[str, object]", raw)
        text = row.get(text_key)
        start = row.get("start")
        end = row.get("end")
        if not isinstance(text, str) or not isinstance(start, int | float):
            continue
        words.append(
            Word(
                text=text.strip(),
                start=float(start) + offset,
                end=(float(end) if isinstance(end, int | float) else float(start)) + offset,
                speaker=_speaker_label(row.get("speaker")),
            )
        )
    return words
