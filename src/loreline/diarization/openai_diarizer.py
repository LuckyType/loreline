"""OpenAI batch speaker diarization (``gpt-4o-transcribe-diarize``).

Runs the whole continuous session audio through OpenAI's diarization model in one
batch call (REST ``/v1/audio/transcriptions``, ``response_format=diarized_json``)
and returns a global speaker timeline - used by the post-session "diarize"
reprocess operation as a cloud alternative to the self-hosted sherpa-onnx service.

The model only exists as a batch endpoint (not in the Realtime API), which is why
this is a post-session pass. The upload is capped at 25 MB, so longer sessions are
transcoded to Opus with ffmpeg first (≈25 MB ~ 13 min as 16 kHz WAV, hours as
Opus). Auth uses the ``OPENAI_API_KEY`` environment variable.

Docs: https://developers.openai.com/api/docs/guides/speech-to-text#speaker-diarization
"""

from __future__ import annotations

import asyncio
import os
from typing import cast

import httpx

from loreline.logging import get_logger
from loreline.models import SpeakerSegment

log = get_logger(__name__)

_DEFAULT_BASE_URL = "https://api.openai.com/v1"
_DEFAULT_MODEL = "gpt-4o-transcribe-diarize"
_MAX_UPLOAD_BYTES = 25 * 1024 * 1024  # OpenAI /v1/audio/transcriptions limit


class OpenAIDiarizer:
    """Diarize a full session WAV via OpenAI's batch diarization model."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._api_key = api_key if api_key is not None else os.environ.get("OPENAI_API_KEY")
        self._model = model or _DEFAULT_MODEL
        headers = {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=base_url or _DEFAULT_BASE_URL, headers=headers, timeout=600.0
        )

    async def diarize(
        self,
        wav: bytes,
        *,
        sample_rate: int = 16000,
        min_speakers: int | None = None,
        max_speakers: int | None = None,
    ) -> list[SpeakerSegment]:
        _ = (sample_rate, min_speakers, max_speakers)  # not configurable on this model
        audio, filename, content_type = await self._prepare(wav)
        files = {"file": (filename, audio, content_type)}
        data = {
            "model": self._model,
            "response_format": "diarized_json",
            "chunking_strategy": "auto",  # required for audio > 30 s
        }
        response = await self._client.post("/audio/transcriptions", data=data, files=files)
        response.raise_for_status()
        return _parse_segments(response.json())

    async def _prepare(self, wav: bytes) -> tuple[bytes, str, str]:
        if len(wav) <= _MAX_UPLOAD_BYTES:
            return wav, "audio.wav", "audio/wav"
        compressed = await _compress_opus(wav)
        if len(compressed) > _MAX_UPLOAD_BYTES:
            msg = f"session audio is {len(compressed)} bytes after Opus compression (> 25 MB)"
            raise ValueError(msg)
        log.info("diarize.openai.compressed", wav_bytes=len(wav), opus_bytes=len(compressed))
        return compressed, "audio.ogg", "audio/ogg"

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()


async def _compress_opus(wav: bytes) -> bytes:
    """Transcode WAV bytes to Opus (~16 kbps) via ffmpeg over stdin/stdout."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-i", "pipe:0", "-c:a", "libopus", "-b:a", "16k", "-f", "ogg", "pipe:1",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )  # fmt: skip
    except OSError as exc:
        msg = "session audio exceeds 25 MB and ffmpeg is unavailable to compress it"
        raise RuntimeError(msg) from exc
    out, err = await proc.communicate(wav)
    if proc.returncode != 0:
        msg = f"ffmpeg compression failed: {err.decode('utf-8', errors='replace')[:200]}"
        raise RuntimeError(msg)
    return out


def _parse_segments(payload: object) -> list[SpeakerSegment]:
    """Map a ``diarized_json`` payload (``{"segments": [...]}`` or a bare list)."""
    raw: object = payload
    if isinstance(payload, dict):
        raw = cast("dict[str, object]", payload).get("segments")
    items = cast("list[object]", raw) if isinstance(raw, list) else []
    segments: list[SpeakerSegment] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        seg = cast("dict[str, object]", item)
        speaker = seg.get("speaker")
        start = seg.get("start")
        end = seg.get("end")
        if (
            speaker is None
            or not isinstance(start, int | float)
            or not isinstance(end, int | float)
        ):
            continue
        label = str(speaker)
        name = label if label.lower().startswith("speaker") else f"Speaker {label}"
        segments.append(SpeakerSegment(start=float(start), end=float(end), speaker=name))
    return segments
