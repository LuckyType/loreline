"""Tests for the OpenAI batch diarizer (gpt-4o-transcribe-diarize)."""

from __future__ import annotations

import httpx

from loreline.diarization.openai_diarizer import OpenAIDiarizer
from loreline.diarization.provider import create_diarizer
from loreline.models import DiarizationConfig, DiarizationMode


async def test_openai_diarizer_parses_diarized_json() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "segments": [
                    {"speaker": "A", "start": 0.0, "end": 1.5, "text": "hi"},
                    {"speaker": "B", "start": 1.5, "end": 3.0, "text": "yo"},
                ]
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://x")
    diarizer = OpenAIDiarizer(client=client)
    segments = await diarizer.diarize(b"RIFF\x00\x00fake-wav", sample_rate=16000)
    await diarizer.aclose()

    assert [s.speaker for s in segments] == ["Speaker A", "Speaker B"]
    assert segments[0].start == 0.0
    assert segments[1].end == 3.0
    assert len(requests) == 1  # one batch call over the whole session
    assert requests[0].url.path.endswith("/audio/transcriptions")


def test_create_diarizer_openai_mode() -> None:
    diarizer = create_diarizer(DiarizationConfig(mode=DiarizationMode.OPENAI))
    assert isinstance(diarizer, OpenAIDiarizer)
