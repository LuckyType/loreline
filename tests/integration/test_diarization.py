"""Integration test: RemoteDiarizer against the mock diarization service."""

from __future__ import annotations

import httpx

from loreline.audio import pcm_to_wav
from loreline.diarization import assign_speakers
from loreline.diarization.provider import create_diarizer
from loreline.diarization.remote import RemoteDiarizer, probe_diarizer
from loreline.health import HealthStatus
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


async def test_remote_diarizer_sends_the_session_id_and_drops_it_on_close() -> None:
    """The session id is what makes labels comparable between two calls.

    Sending it is half the contract; the other half is that the service is told
    when the session is over, which happens on close because that is where a
    capture and a re-processing job both already end.
    """
    app = create_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://diar") as client:
        diarizer = RemoteDiarizer("http://diar", client=client)
        wav = pcm_to_wav(b"\x01\x00" * 16000, sample_rate=16000)
        await diarizer.diarize(wav, session_id="s-42")
        await diarizer.diarize(wav, session_id="s-42")
        assert app.state.deleted_sessions == []
        await diarizer.aclose()

    assert app.state.seen_sessions == ["s-42", "s-42"]
    assert app.state.deleted_sessions == ["s-42"]


async def test_remote_diarizer_without_a_session_id_sends_none() -> None:
    """A caller with nothing to remember across calls stays stateless."""
    app = create_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://diar") as client:
        diarizer = RemoteDiarizer("http://diar", client=client)
        await diarizer.diarize(pcm_to_wav(b"\x01\x00" * 16000, sample_rate=16000))
        await diarizer.aclose()

    assert app.state.seen_sessions == []
    assert app.state.deleted_sessions == []


async def test_remote_diarizer_close_survives_a_service_that_cannot_forget() -> None:
    """A service too old for the route, or simply gone, must not fail a stop."""

    def refuse(request: httpx.Request) -> httpx.Response:
        if request.method == "DELETE":
            return httpx.Response(405, text="method not allowed")
        return httpx.Response(200, json={"segments": []})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(refuse), base_url="http://diar"
    ) as client:
        diarizer = RemoteDiarizer("http://diar", client=client)
        await diarizer.diarize(b"", session_id="s-42")
        await diarizer.aclose()


async def test_probe_grades_the_service_like_a_provider() -> None:
    """The same five states the settings page renders, not a bool: a 404 on a
    live host is a wrong URL and reads as unreachable, where ``< 500`` called
    it fine."""
    transport = httpx.ASGITransport(app=create_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://diar") as client:
        report = await probe_diarizer("http://diar", client=client)
    assert report.status is HealthStatus.HEALTHY

    def gone(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(gone), base_url="http://diar"
    ) as client:
        report = await probe_diarizer("http://diar", client=client)
    assert report.status is HealthStatus.UNREACHABLE


def test_create_diarizer_remote_requires_endpoint() -> None:
    config = DiarizationConfig(mode=DiarizationMode.REMOTE)
    try:
        create_diarizer(config)
    except ValueError as exc:
        assert "endpoint" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected ValueError")
