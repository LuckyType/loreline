"""Integration test: RemoteDiarizer against the mock diarization service."""

from __future__ import annotations

import httpx

from loreline.audio import pcm_to_wav
from loreline.diarization import assign_speakers
from loreline.diarization.provider import create_diarizer
from loreline.diarization.remote import RemoteDiarizer
from loreline.models import DiarizationConfig, DiarizationMode, TranscriptEvent, Word
from mocks.diarization import create_app


async def test_remote_diarizer_returns_segments() -> None:
    transport = httpx.ASGITransport(app=create_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://diar") as client:
        diarizer = RemoteDiarizer("http://diar", client=client)
        wav = pcm_to_wav(b"\x01\x00" * 16000, sample_rate=16000)  # 1 second
        segments = await diarizer.diarize(wav, sample_rate=16000)

    assert len(segments) == 2
    assert segments[0].speaker == "Speaker 0"
    assert segments[1].speaker == "Speaker 1"
    assert segments[1].end > segments[0].end


async def test_remote_diarization_merges_onto_transcript() -> None:
    transport = httpx.ASGITransport(app=create_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://diar") as client:
        diarizer = RemoteDiarizer("http://diar", client=client)
        wav = pcm_to_wav(b"\x01\x00" * 16000, sample_rate=16000)
        segments = await diarizer.diarize(wav)

    event = TranscriptEvent(
        session_id="s1",
        source="p1",
        text="hallo welt",
        words=[
            Word(text="hallo", start=0.1, end=0.4),
            Word(text="welt", start=0.6, end=0.9),
        ],
        start_ts=0.0,
        end_ts=1.0,
        is_final=True,
    )
    merged = assign_speakers(event, segments)
    assert merged.words[0].speaker == "Speaker 0"
    assert merged.words[1].speaker == "Speaker 1"


async def test_health_ok() -> None:
    transport = httpx.ASGITransport(app=create_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://diar") as client:
        diarizer = RemoteDiarizer("http://diar", client=client)
        assert await diarizer.health() is True


def test_create_diarizer_remote_requires_endpoint() -> None:
    config = DiarizationConfig(mode=DiarizationMode.REMOTE)
    try:
        create_diarizer(config)
    except ValueError as exc:
        assert "endpoint" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected ValueError")
