"""Gemini transcription connector (``gemini-3.5-transcribe``).

Google's Cloud Speech-to-Text v2 API (``speech.googleapis.com``, see
``loreline.stt.backends.google``) rejects API keys outright - it requires an
OAuth2 principal, so a bare key fails at ``StreamingRecognize`` with
``CREDENTIALS_MISSING``. The Gemini API (``generativelanguage.googleapis.com``)
is a different service that *does* authenticate with a plain API key, via the
``x-goog-api-key`` header, and ``gemini-3.5-transcribe`` covers what this app
needs from a provider in a single call: speaker diarization, word-level
timestamps, custom vocabulary, and explicit language codes.

Unlike the gRPC streaming v2 connector, this is a batch endpoint: each voiced
utterance is wrapped in a WAV container, base64'd inline, and POSTed on its
own - the same shape as the OpenAI-compatible backend. Inline audio is capped
at 20 MB per request, which an utterance of a few seconds never approaches.

Docs: https://ai.google.dev/gemini-api/docs/transcribe
"""

from __future__ import annotations

import base64
from collections.abc import AsyncIterator
from http import HTTPStatus
from typing import cast

import httpx

from loreline.audio.chunker import Utterance
from loreline.audio.wav import pcm_to_wav
from loreline.logging import get_logger
from loreline.models import Glossary, ProviderConfig, ProviderKind, TranscriptEvent, Word
from loreline.secrets import SecretStore
from loreline.stt.base import error_detail, glossary_terms
from loreline.stt.registry import register

log = get_logger(__name__)

_DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
# The API caps custom_vocabulary at 1000 terms; a longer glossary is truncated
# rather than rejected outright mid-session.
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


class GeminiSTTBackend:
    """Batch transcription with inline diarization via the Gemini API."""

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
        self.config = config
        self._language = language or config.language
        self._model = model
        self._diarize = diarize
        base_url = config.base_url or _DEFAULT_BASE_URL
        # Gemini authenticates with this header rather than a bearer token.
        headers = {"x-goog-api-key": api_key} if api_key else {}
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(base_url=base_url, headers=headers, timeout=60.0)

    async def transcribe(
        self,
        audio: AsyncIterator[Utterance],
        *,
        session_id: str,
        glossary: Glossary | None = None,
    ) -> AsyncIterator[TranscriptEvent]:
        vocabulary = glossary_terms(glossary)[:_MAX_VOCABULARY]
        async for utterance in audio:
            event = await self._transcribe_one(
                utterance, session_id=session_id, vocabulary=vocabulary
            )
            if event is not None:
                yield event

    def _request_body(self, wav: bytes, vocabulary: list[str]) -> dict[str, object]:
        mode: dict[str, object] = {"type": "verbatim", "timestamp_granularities": ["word"]}
        if self._diarize:
            mode["diarization_mode"] = "speaker"
        transcription: dict[str, object] = {"mode": mode}
        # An empty language_codes list means "auto-detect", which is what a
        # provider configured with no language should get.
        if self._language:
            transcription["language_codes"] = [self._language]
        if vocabulary:
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

    async def _transcribe_one(
        self, utterance: Utterance, *, session_id: str, vocabulary: list[str]
    ) -> TranscriptEvent | None:
        wav = pcm_to_wav(utterance.pcm, sample_rate=self.config.sample_rate)
        response = await self._client.post(
            "/interactions", json=self._request_body(wav, vocabulary)
        )
        if response.status_code >= HTTPStatus.BAD_REQUEST:
            # Keep the body: Gemini puts the actionable reason there (bad key,
            # unknown model, audio too long), not in the status line.
            raise httpx.HTTPStatusError(
                f"{response.status_code} from {response.request.url}: {error_detail(response)}",
                request=response.request,
                response=response,
            )
        return self._to_event(response.json(), utterance, session_id)

    def _to_event(
        self, payload: object, utterance: Utterance, session_id: str
    ) -> TranscriptEvent | None:
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
        if not text:
            return None
        return TranscriptEvent(
            session_id=session_id,
            source=self.config.id,
            text=text,
            words=words,
            speaker=words[0].speaker if words else None,
            start_ts=utterance.start,
            end_ts=utterance.end,
            is_final=True,
        )

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

    async def health(self) -> bool:
        # Unlike the Cloud STT connector, this actually exercises the
        # credential: a bad key fails here rather than at the first utterance.
        try:
            response = await self._client.get("/models")
        except httpx.HTTPError:
            return False
        return response.status_code < HTTPStatus.BAD_REQUEST

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()


@register(ProviderKind.GEMINI)
def _factory(  # pyright: ignore[reportUnusedFunction]
    config: ProviderConfig, secrets: SecretStore, model: str | None
) -> GeminiSTTBackend:
    api_key = secrets.get(config.auth_ref) if config.auth_ref else None
    return GeminiSTTBackend(config, model=model, api_key=api_key)
