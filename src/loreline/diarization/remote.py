"""Remote diarization provider (sherpa-onnx HTTP service)."""

from __future__ import annotations

from http import HTTPStatus
from typing import cast

import httpx

from loreline.logging import get_logger
from loreline.models import SpeakerSegment

log = get_logger(__name__)


class RemoteDiarizer:
    """Call a self-hosted diarization service that returns speaker segments.

    The service contract (see ``services/diarization`` and ``mocks/diarization``):
    ``POST {endpoint}/diarize`` multipart ``file`` (WAV) ->
    ``{"segments": [{"start": float, "end": float, "speaker": str}, ...]}``.
    """

    def __init__(
        self,
        endpoint: str,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._endpoint = endpoint
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(base_url=endpoint, timeout=120.0)

    async def diarize(
        self,
        wav: bytes,
        *,
        sample_rate: int = 16000,
        min_speakers: int | None = None,
        max_speakers: int | None = None,
    ) -> list[SpeakerSegment]:
        data: dict[str, str] = {"sample_rate": str(sample_rate)}
        if min_speakers is not None:
            data["min_speakers"] = str(min_speakers)
        if max_speakers is not None:
            data["max_speakers"] = str(max_speakers)
        files = {"file": ("audio.wav", wav, "audio/wav")}
        response = await self._client.post("/diarize", data=data, files=files)
        response.raise_for_status()
        return _parse_segments(response.json())

    async def health(self) -> bool:
        try:
            response = await self._client.get("/healthz")
        except httpx.HTTPError:
            return False
        return response.status_code < HTTPStatus.INTERNAL_SERVER_ERROR

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()


def _parse_segments(payload: object) -> list[SpeakerSegment]:
    if not isinstance(payload, dict):
        return []
    raw_segments = cast("dict[str, object]", payload).get("segments")
    if not isinstance(raw_segments, list):
        return []
    segments: list[SpeakerSegment] = []
    for raw in cast("list[object]", raw_segments):
        if not isinstance(raw, dict):
            continue
        item = cast("dict[str, object]", raw)
        start, end, speaker = item.get("start"), item.get("end"), item.get("speaker")
        if isinstance(start, (int, float)) and isinstance(end, (int, float)):
            segments.append(
                SpeakerSegment(start=float(start), end=float(end), speaker=str(speaker))
            )
    return segments
