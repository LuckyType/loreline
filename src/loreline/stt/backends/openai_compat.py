"""OpenAI-compatible STT backend.

Covers OpenAI's ``/v1/audio/transcriptions`` and any compatible self-hosted
endpoint (Speaches, whisper.cpp server). Each voiced utterance is wrapped in a
WAV container and POSTed as multipart; the JSON ``text`` becomes one final
``TranscriptEvent``. Glossary terms are passed via the ``prompt`` field.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from http import HTTPStatus
from typing import cast

import httpx

from loreline.audio.chunker import Utterance
from loreline.audio.wav import pcm_to_wav
from loreline.logging import get_logger
from loreline.models import Glossary, ProviderConfig, ProviderKind, TranscriptEvent
from loreline.secrets import SecretStore
from loreline.stt.base import glossary_terms
from loreline.stt.registry import register

log = get_logger(__name__)

_DEFAULT_BASE_URL = "https://api.openai.com/v1"
_DEFAULT_MODEL = "whisper-1"


class OpenAICompatBackend:
    """Batch transcription against an OpenAI-compatible HTTP endpoint."""

    def __init__(
        self,
        config: ProviderConfig,
        *,
        client: httpx.AsyncClient | None = None,
        api_key: str | None = None,
        language: str | None = None,
    ) -> None:
        self.config = config
        self._language = language or config.language
        self._model = config.model or _DEFAULT_MODEL
        base_url = config.base_url or _DEFAULT_BASE_URL
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(base_url=base_url, headers=headers, timeout=60.0)

    async def transcribe(
        self,
        audio: AsyncIterator[Utterance],
        *,
        session_id: str,
        glossary: Glossary | None = None,
    ) -> AsyncIterator[TranscriptEvent]:
        prompt = ", ".join(glossary_terms(glossary)) or None
        async for utterance in audio:
            text = await self._transcribe_one(utterance, prompt=prompt)
            if not text:
                continue
            yield TranscriptEvent(
                session_id=session_id,
                source=self.config.id,
                text=text,
                start_ts=utterance.start,
                end_ts=utterance.end,
                is_final=True,
            )

    async def _transcribe_one(self, utterance: Utterance, *, prompt: str | None) -> str:
        wav = pcm_to_wav(utterance.pcm, sample_rate=self.config.sample_rate)
        data: dict[str, str] = {
            "model": self._model,
            "language": self._language,
            "response_format": "json",
        }
        if prompt:
            data["prompt"] = prompt
        files = {"file": ("utterance.wav", wav, "audio/wav")}
        response = await self._client.post("/audio/transcriptions", data=data, files=files)
        if response.status_code >= HTTPStatus.BAD_REQUEST:
            # raise_for_status() alone reports only "404 Not Found", throwing
            # away the body - where these servers put the actual reason (e.g.
            # "Model 'X' is not installed locally"). Keep it: it's usually the
            # difference between a mystery and an obvious fix.
            raise httpx.HTTPStatusError(
                f"{response.status_code} from {response.request.url}: {_detail(response)}",
                request=response.request,
                response=response,
            )
        payload: object = response.json()
        if isinstance(payload, dict):
            mapping = cast("dict[str, object]", payload)
            text = mapping.get("text")
            if isinstance(text, str):
                return text.strip()
        log.warning("stt.openai_compat.unexpected_payload", provider=self.config.id)
        return ""

    async def health(self) -> bool:
        try:
            response = await self._client.get("/models")
        except httpx.HTTPError:
            return False
        return response.status_code < HTTPStatus.INTERNAL_SERVER_ERROR

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()


@register(ProviderKind.OPENAI_COMPAT)
def _factory(  # pyright: ignore[reportUnusedFunction]
    config: ProviderConfig, secrets: SecretStore
) -> OpenAICompatBackend:
    api_key = secrets.get(config.auth_ref) if config.auth_ref else None
    return OpenAICompatBackend(config, api_key=api_key)


def _detail(response: httpx.Response) -> str:
    """Best-effort human-readable reason from an error response body."""
    try:
        payload: object = response.json()
    except ValueError:
        return response.text[:300].strip() or response.reason_phrase
    if isinstance(payload, dict):
        mapping = cast("dict[str, object]", payload)
        for key in ("detail", "message", "error"):
            value = mapping.get(key)
            if isinstance(value, str) and value:
                return value
            if isinstance(value, dict):
                nested = cast("dict[str, object]", value).get("message")
                if isinstance(nested, str) and nested:
                    return nested
    return response.text[:300].strip() or response.reason_phrase
