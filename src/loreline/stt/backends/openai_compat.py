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

from collections.abc import AsyncIterator
from http import HTTPStatus
from typing import NamedTuple, cast

import httpx

from loreline.audio.chunker import Utterance
from loreline.audio.wav import pcm_to_wav
from loreline.logging import get_logger
from loreline.models import Glossary, ProviderConfig, ProviderKind, TranscriptEvent, Word
from loreline.secrets import SecretStore
from loreline.stt.base import error_detail, glossary_terms
from loreline.stt.registry import register

log = get_logger(__name__)

_DEFAULT_BASE_URL = "https://api.openai.com/v1"
_DEFAULT_MODEL = "whisper-1"


class _Transcription(NamedTuple):
    """One utterance's result: the text plus whatever structure came with it."""

    text: str
    words: list[Word]

    @property
    def speaker(self) -> str | None:
        """The utterance's dominant speaker, taken from its first labelled word
        - the same convention the Deepgram and AssemblyAI connectors use."""
        return next((w.speaker for w in self.words if w.speaker), None)


class OpenAICompatBackend:
    """Batch transcription against an OpenAI-compatible HTTP endpoint."""

    def __init__(
        self,
        config: ProviderConfig,
        *,
        client: httpx.AsyncClient | None = None,
        api_key: str | None = None,
        language: str | None = None,
        default_base_url: str = _DEFAULT_BASE_URL,
        default_model: str = _DEFAULT_MODEL,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        """``default_base_url``/``default_model``/``extra_headers`` let a kind
        that speaks this same wire format reuse the backend rather than copy it
        - see loreline/stt/backends/openrouter.py."""
        self.config = config
        self._language = language or config.language
        self._model = config.model or default_model
        base_url = config.base_url or default_base_url
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        headers.update(extra_headers or {})
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(base_url=base_url, headers=headers, timeout=60.0)
        # None until the first response tells us whether this endpoint honours
        # verbose_json; False pins it to plain json from then on.
        self._verbose_json: bool | None = None

    async def transcribe(
        self,
        audio: AsyncIterator[Utterance],
        *,
        session_id: str,
        glossary: Glossary | None = None,
    ) -> AsyncIterator[TranscriptEvent]:
        prompt = ", ".join(glossary_terms(glossary)) or None
        async for utterance in audio:
            result = await self._transcribe_one(utterance, prompt=prompt)
            if not result.text:
                continue
            yield TranscriptEvent(
                session_id=session_id,
                source=self.config.id,
                text=result.text,
                words=result.words,
                speaker=result.speaker,
                start_ts=utterance.start,
                end_ts=utterance.end,
                is_final=True,
            )

    async def _transcribe_one(self, utterance: Utterance, *, prompt: str | None) -> _Transcription:
        wav = pcm_to_wav(utterance.pcm, sample_rate=self.config.sample_rate)
        response = await self._post(wav, prompt=prompt, verbose=self._verbose_json is not False)
        if response.status_code == HTTPStatus.BAD_REQUEST and self._verbose_json is None:
            # This endpoint does not do verbose_json. Remember it and fall back
            # once, rather than rejecting the utterance over a response format
            # the GM never asked for.
            log.info("stt.openai_compat.verbose_json_unsupported", provider_id=self.config.id)
            self._verbose_json = False
            response = await self._post(wav, prompt=prompt, verbose=False)
        if response.status_code >= HTTPStatus.BAD_REQUEST:
            # raise_for_status() alone reports only "404 Not Found", throwing
            # away the body - where these servers put the actual reason (e.g.
            # "Model 'X' is not installed locally"). Keep it: it's usually the
            # difference between a mystery and an obvious fix.
            raise httpx.HTTPStatusError(
                f"{response.status_code} from {response.request.url}: {error_detail(response)}",
                request=response.request,
                response=response,
            )
        payload: object = response.json()
        if not isinstance(payload, dict):
            log.warning(
                "stt.openai_compat.unexpected_payload",
                provider=self.config.name,
                provider_id=self.config.id,
            )
            return _Transcription("", [])
        mapping = cast("dict[str, object]", payload)
        text = mapping.get("text")
        if self._verbose_json is None:
            # A body carrying words/segments proves the format took effect.
            self._verbose_json = "words" in mapping or "segments" in mapping
        return _Transcription(
            text=text.strip() if isinstance(text, str) else "",
            words=_parse_words(mapping, offset=utterance.start),
        )

    async def _post(self, wav: bytes, *, prompt: str | None, verbose: bool) -> httpx.Response:
        data: dict[str, object] = {
            "model": self._model,
            "language": self._language,
            "response_format": "verbose_json" if verbose else "json",
        }
        if verbose:
            # Repeated form field, per the OpenAI multipart convention. Without
            # it a verbose_json body carries segments but no `words` array.
            data["timestamp_granularities[]"] = ["word", "segment"]
        if prompt:
            data["prompt"] = prompt
        files = {"file": ("utterance.wav", wav, "audio/wav")}
        return await self._client.post("/audio/transcriptions", data=data, files=files)

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
