"""Gemini transcription connector (``gemini-3.5-transcribe``).

Google's Cloud Speech-to-Text v2 API (``speech.googleapis.com``, see
``loreline.stt.backends.google``) rejects API keys outright - it requires an
OAuth2 principal, so a bare key fails at ``StreamingRecognize`` with
``CREDENTIALS_MISSING``. The Gemini API (``generativelanguage.googleapis.com``)
is a different service that *does* authenticate with a plain API key, via the
``x-goog-api-key`` header, and ``gemini-3.5-transcribe`` covers what this app
needs from a provider in a single call: speaker diarization, word-level
timestamps, custom vocabulary, and explicit language codes. Individually, that
is: the service rejects a custom vocabulary sent alongside either of the other
two ("custom_vocabulary is incompatible with timestamps"), which is why
capabilities.yaml records the pairs as conflicts and ``_request_body`` resolves
them before building the request.

Unlike the gRPC streaming v2 connector, this is a batch endpoint: each voiced
utterance is wrapped in a WAV container, base64'd inline, and POSTed on its
own - the same shape as the OpenAI-compatible backend. Inline audio is capped
at 20 MB per request, which an utterance of a few seconds never approaches.

Docs: https://ai.google.dev/gemini-api/docs/transcribe
"""

from __future__ import annotations

import base64
from typing import cast

import httpx

from loreline.audio.chunker import Utterance
from loreline.audio.wav import pcm_to_wav
from loreline.capabilities import surface_for
from loreline.logging import get_logger
from loreline.models import Glossary, Interaction, ProviderConfig, ProviderKind, Word
from loreline.secrets import SecretStore
from loreline.stt.base import (
    FeatureConflictGuard,
    HttpConnector,
    Transcription,
    glossary_terms,
    glossary_terms_for,
    secret_for,
)
from loreline.stt.registry import register

log = get_logger(__name__)

# The API caps custom_vocabulary at 1000 terms; a longer glossary is truncated
# rather than rejected outright mid-session. The curated models record that
# ceiling in capabilities.yaml, which is what the request actually reads; this
# is the fallback for a Gemini id nobody has annotated, where there is no
# per-model number to read.
_MAX_VOCABULARY = 1000


def _as_dict(value: object) -> dict[str, object]:
    return cast("dict[str, object]", value) if isinstance(value, dict) else {}


def _as_list(value: object) -> list[object]:
    return cast("list[object]", value) if isinstance(value, list) else []


def _as_str(value: object) -> str:
    return value if isinstance(value, str) else ""


def _seconds(value: object) -> float:
    """Parse a protobuf duration string (``"1.250s"``) into float seconds."""
    try:
        return float(_as_str(value).removesuffix("s"))
    except ValueError:
        return 0.0


class GeminiSTTBackend(HttpConnector[list[str]]):
    """Batch transcription with inline diarization via the Gemini API.

    The prepared value is the ``custom_vocabulary`` list, capped for the model.
    """

    def __init__(
        self,
        config: ProviderConfig,
        *,
        model: str | None = None,
        client: httpx.AsyncClient | None = None,
        api_key: str | None = None,
        language: str | None = None,
        diarize: bool = True,
    ) -> None:
        # The native surface: x-goog-api-key rather than a bearer token, and a
        # base its OpenAI-compatible sibling (the summarize surface) does not
        # serve. Both facts are the yaml's, not this connector's.
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
        self._diarize = diarize
        # capabilities.yaml declares which of the three features below this
        # model refuses to combine; the guard is what keeps them off the wire.
        self._conflicts = FeatureConflictGuard(config, model)

    def prepare(self, glossary: Glossary | None) -> list[str]:
        return glossary_terms_for(
            ProviderKind.GEMINI,
            self._model,
            glossary_terms(glossary),
            realtime=False,
            fallback_max_terms=_MAX_VOCABULARY,
        )

    def _request_body(self, wav: bytes, vocabulary: list[str]) -> dict[str, object]:
        # Everything this request would turn on, before the model gets a say.
        # Word timestamps are not a GM-facing toggle: this connector has always
        # asked for them, because they are what a diarizer aligns speaker spans
        # onto (loreline.diarization.merge).
        requested = {"word_timestamps"}
        if self._diarize:
            requested.add("inline_diarization")
        if vocabulary:
            requested.add("glossary")
        # Google refuses either of the other two paired with the glossary:
        # "custom_vocabulary is incompatible with timestamps." Sending such a
        # pair fails the whole utterance with a 400, so the glossary wins and
        # the other feature is left off (see CONFLICT_PRECEDENCE).
        allowed = self._conflicts.allowed(requested)
        mode: dict[str, object] = {"type": "verbatim"}
        # Omitted rather than sent empty: an unasked-for key is a shape the API
        # documents, an empty timestamp_granularities is not.
        if "word_timestamps" in allowed:
            mode["timestamp_granularities"] = ["word"]
        if "inline_diarization" in allowed:
            mode["diarization_mode"] = "speaker"
        transcription: dict[str, object] = {"mode": mode}
        # An empty language_codes list means "auto-detect", which is what a
        # provider configured with no language should get.
        if self._language:
            transcription["language_codes"] = [self._language]
        if "glossary" in allowed:
            transcription["custom_vocabulary"] = vocabulary
        body: dict[str, object] = {
            "input": [
                {
                    "type": "audio",
                    "data": base64.b64encode(wav).decode("ascii"),
                    "mime_type": "audio/wav",
                }
            ],
            "generation_config": {"transcription_config": transcription},
        }
        # The Interactions API requires a model, and this kind always has one:
        # capabilities.yaml marks a Gemini transcription default, so the only
        # way to get here without one is that marker going missing - in which
        # case Google's own 400 names the field, which beats this connector
        # substituting a model id it invented.
        if self._model:
            body["model"] = self._model
        return body

    async def transcribe_one(
        self, utterance: Utterance, prepared: list[str]
    ) -> Transcription | None:
        wav = pcm_to_wav(utterance.pcm, sample_rate=self.config.sample_rate)
        response = await self._client.post("/interactions", json=self._request_body(wav, prepared))
        # Gemini puts the actionable reason in the body (bad key, unknown
        # model, audio too long), which is what the raise keeps.
        self._raise_for_status(response)
        return self._parse(response.json(), utterance)

    def _parse(self, payload: object, utterance: Utterance) -> Transcription | None:
        body = _as_dict(payload)
        status = _as_str(body.get("status"))
        if status and status != "completed":
            log.warning(
                "stt.gemini.incomplete",
                provider=self.config.name,
                provider_id=self.config.id,
                status=status,
            )
            return None
        chunks: list[str] = []
        words: list[Word] = []
        for step in _as_list(body.get("steps")):
            for content in _as_list(_as_dict(step).get("content")):
                item = _as_dict(content)
                if item.get("type") != "text":
                    continue
                chunks.append(_as_str(item.get("text")))
                words.extend(self._words(item.get("annotations"), utterance.start))
        text = " ".join(chunk for chunk in chunks if chunk).strip()
        return Transcription(text=text, words=words)

    def _words(self, annotations: object, offset: float) -> list[Word]:
        """Map ``word_info`` annotations to words, rebased onto session time."""
        words: list[Word] = []
        for annotation in _as_list(annotations):
            info = _as_dict(annotation)
            if info.get("type") != "word_info":
                continue
            # Offsets are relative to this utterance's audio, but transcript
            # timings are session-relative.
            speaker = _as_str(info.get("speaker"))
            words.append(
                Word(
                    text=_as_str(info.get("text")),
                    start=_seconds(info.get("start_offset")) + offset,
                    end=_seconds(info.get("end_offset")) + offset,
                    speaker=f"Speaker {speaker}" if speaker else None,
                )
            )
        return words


@register(ProviderKind.GEMINI)
def _factory(  # pyright: ignore[reportUnusedFunction]
    config: ProviderConfig, secrets: SecretStore, model: str | None
) -> GeminiSTTBackend:
    return GeminiSTTBackend(config, model=model, api_key=secret_for(config, secrets))
