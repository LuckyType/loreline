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

A provider row that names no model is answered by asking the server which ones
it has, once per connector (see ``_request_model``). This connector used to
leave the field out instead, on the theory that a server with a single model
loaded transcribes with it regardless. Speaches, the self-hosted server this
repo ships a compose service for, refuses that: ``422 {"type": "missing",
"loc": ["body", "model"], "msg": "Field required"}`` even with exactly one
model loaded.
"""

from __future__ import annotations

from http import HTTPStatus
from typing import cast

import httpx

from loreline.audio.chunker import Utterance
from loreline.audio.wav import pcm_to_wav
from loreline.capabilities import catalog_for, surface_for
from loreline.capability_config import TranscribeCapabilities
from loreline.catalog import ClientFactory
from loreline.logging import get_logger
from loreline.models import Glossary, Interaction, ProviderConfig, ProviderKind, Word
from loreline.secrets import SecretStore
from loreline.stt.base import HttpConnector, Transcription, glossary_terms, secret_for
from loreline.stt.catalog import list_models
from loreline.stt.registry import register

log = get_logger(__name__)


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
        catalog_client_factory: ClientFactory | None = None,
    ) -> None:
        """Three kinds share this wire format (OpenAI cloud, OpenRouter, the
        self-hosted kind), and everything that differs between them - the
        base, the attribution headers - is their batch transcription surface
        in capabilities.yaml, so nothing here is per kind.

        ``model`` may be None, and this is the one connector where that is a
        normal state rather than a missing default: it also serves the
        self-hosted kind, whose catalogue is whatever the operator installed,
        so capabilities.yaml curates no models for it and can vouch for none.
        The server is then asked which models it has, once, and the answer is
        used for every utterance after (see :meth:`_request_model`).

        ``catalog_client_factory`` is how that second surface is reached; it
        exists for the same reason ``client`` does, so a test can serve the
        catalogue without a running server. It is a factory rather than a
        client because the catalogue reader owns and closes what it opens, and
        closing this connector's own client would end the run."""
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
        # Already spelled into the client's headers above. Kept because
        # resolving an unset model asks a second surface, the catalogue, whose
        # own spelling of the credential is that surface's to decide.
        self._api_key = api_key
        self._catalog_client_factory = catalog_client_factory
        # What the server answered when this connector had no model to send.
        # Resolved on the first utterance and kept for the connector's life,
        # the way FeatureConflictGuard keeps its conflict groups: a fact about
        # the run, not a lookup per utterance.
        self._server_model: str | None = None
        # None until the first response tells us whether this endpoint honours
        # verbose_json; False pins it to plain json from then on.
        self._verbose_json: bool | None = None

    def prepare(self, glossary: Glossary | None) -> str | None:
        return ", ".join(glossary_terms(glossary)) or None

    async def transcribe_one(self, utterance: Utterance, prepared: str | None) -> Transcription:
        prompt = prepared
        model = await self._request_model()
        wav = pcm_to_wav(utterance.pcm, sample_rate=self.config.sample_rate)
        response = await self._post(
            wav, model=model, prompt=prompt, verbose=self._verbose_json is not False
        )
        if response.status_code == HTTPStatus.BAD_REQUEST and self._verbose_json is None:
            # This endpoint does not do verbose_json. Remember it and fall back
            # once, rather than rejecting the utterance over a response format
            # the GM never asked for.
            log.info("stt.openai_compat.verbose_json_unsupported", provider_id=self.config.id)
            self._verbose_json = False
            response = await self._post(wav, model=model, prompt=prompt, verbose=False)
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

    async def _request_model(self) -> str:
        """The model this connector sends, asking the server once if it must.

        The configured one wherever there is one, which is every kind whose
        request schemas require the caller to choose. Only the self-hosted kind
        arrives without one, and the field cannot simply be left out: Speaches
        answers a request with no ``model`` with ``422 Field required`` even
        when it has exactly one loaded, which is a confusing way to learn that
        the row needs a model. So the server is asked what it has, through the
        same reader the picker offers that row's models from, so the connector
        cannot run a model the GM was never shown.
        """
        if self._model:
            return self._model
        if self._server_model is None:
            self._server_model = await self._resolve_model()
        return self._server_model

    async def _resolve_model(self) -> str:
        """The server's own answer to "which models do you have", read once.

        Several is a real "we do not know which one you meant": the row names
        none and the operator loaded more than one, so the first is taken and
        the whole list is logged, loudly, rather than the choice being made in
        silence. Nothing here raises on the way: the reader is fail soft and
        returns an empty list for a catalogue that was unreachable, empty or
        unreadable, and its own log line carries which of those it was.
        """
        models = await list_models(
            kind=self.config.kind,
            base_url=self.config.base_url,
            api_key=self._api_key,
            interaction=Interaction.TRANSCRIBE,
            client_factory=self._catalog_client_factory,
        )
        if not models:
            catalogue = catalog_for(
                self.config.kind, Interaction.TRANSCRIBE, base_url=self.config.base_url
            )
            where = f" at {catalogue.url}" if catalogue else ""
            msg = (
                f"{self.config.name} has no model configured and its server "
                f"listed none{where}; name a model on the provider row"
            )
            raise ValueError(msg)
        chosen = models[0].id
        if len(models) > 1:
            log.warning(
                "stt.openai_compat.model_ambiguous",
                provider=self.config.name,
                provider_id=self.config.id,
                chosen=chosen,
                listed=[m.id for m in models],
            )
        else:
            log.info(
                "stt.openai_compat.model_resolved",
                provider=self.config.name,
                provider_id=self.config.id,
                chosen=chosen,
            )
        return chosen

    async def _post(
        self, wav: bytes, *, model: str, prompt: str | None, verbose: bool
    ) -> httpx.Response:
        data: dict[str, object] = {
            "language": self._language,
            "response_format": "verbose_json" if verbose else "json",
            "model": model,
        }
        if verbose:
            # Repeated form field, per the OpenAI multipart convention. Without
            # it a verbose_json body carries segments but no `words` array.
            data["timestamp_granularities[]"] = ["word", "segment"]
        if prompt:
            data["prompt"] = prompt
        files = {"file": ("utterance.wav", wav, "audio/wav")}
        return await self._client.post("/audio/transcriptions", data=data, files=files)


@register(ProviderKind.OPENAI_COMPAT)
def _factory(  # pyright: ignore[reportUnusedFunction]
    config: ProviderConfig,
    secrets: SecretStore,
    model: str | None,
    # Unused: this connector's prompt has no per-model ceiling in the yaml to
    # read, so the resolved capabilities say nothing it acts on.
    _caps: TranscribeCapabilities | None,
) -> OpenAICompatBackend:
    return OpenAICompatBackend(config, model=model, api_key=secret_for(config, secrets))


@register(ProviderKind.OPENAI)
def _openai_batch_factory(  # pyright: ignore[reportUnusedFunction]
    config: ProviderConfig,
    secrets: SecretStore,
    model: str | None,
    # Unused: this connector's prompt has no per-model ceiling in the yaml to
    # read, so the resolved capabilities say nothing it acts on.
    _caps: TranscribeCapabilities | None,
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
