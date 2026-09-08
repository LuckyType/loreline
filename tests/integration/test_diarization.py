"""Integration test: RemoteDiarizer against the mock diarization service."""

from __future__ import annotations

from typing import cast

import httpx
import pytest
from structlog.testing import capture_logs

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


async def test_the_request_timeout_grows_with_the_audio_sent() -> None:
    """The flat 120 s that made every real diarization fail, replaced.

    Read off the deadline the request actually carries rather than by waiting
    for one: httpx records what it applied in the request's extensions, which
    is the only place a timeout survives the call. Two lengths, because one
    number cannot tell a floor from a multiple - and the assertion is on the
    exact values, since "more than 120" would still pass for the constant this
    exists to remove.
    """
    read_timeouts: list[float | None] = []

    def answer(request: httpx.Request) -> httpx.Response:
        timeout = cast("dict[str, float | None]", request.extensions["timeout"])
        read_timeouts.append(timeout["read"])
        return httpx.Response(200, json={"segments": []})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(answer), base_url="http://diar"
    ) as client:
        diarizer = RemoteDiarizer("http://diar", client=client)
        await diarizer.diarize(pcm_to_wav(b"\x01\x00" * 16000, sample_rate=16000))
        await diarizer.diarize(pcm_to_wav(b"\x01\x00" * 16000 * 60, sample_rate=16000))

    # 60 s floor + 4 s per second of audio: a second of speech, then a minute.
    assert read_timeouts == [64.0, 300.0]


async def test_a_payload_that_is_not_a_wav_still_gets_the_floor() -> None:
    """Sizing the deadline must never be the thing that fails a diarization."""
    read_timeouts: list[float | None] = []

    def answer(request: httpx.Request) -> httpx.Response:
        timeout = cast("dict[str, float | None]", request.extensions["timeout"])
        read_timeouts.append(timeout["read"])
        return httpx.Response(200, json={"segments": []})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(answer), base_url="http://diar"
    ) as client:
        diarizer = RemoteDiarizer("http://diar", client=client)
        await diarizer.diarize(b"")

    assert read_timeouts == [60.0]


async def test_forgetting_a_session_keeps_its_own_short_timeout() -> None:
    """The delete is not sized by audio: it happens inside a session's stop,
    and a hung service must not hold that open for as long as a diarization is
    allowed to take."""
    timeouts: list[float | None] = []

    def answer(request: httpx.Request) -> httpx.Response:
        if request.method == "DELETE":
            timeout = cast("dict[str, float | None]", request.extensions["timeout"])
            timeouts.append(timeout["read"])
        return httpx.Response(200, json={"segments": []})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(answer), base_url="http://diar"
    ) as client:
        diarizer = RemoteDiarizer("http://diar", client=client)
        await diarizer.diarize(pcm_to_wav(b"\x01\x00" * 16000, sample_rate=16000), session_id="s")
        await diarizer.aclose()

    assert timeouts == [5.0]


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


@pytest.mark.parametrize("status", [404, 405, 500])
async def test_remote_diarizer_close_survives_a_service_that_cannot_forget(status: int) -> None:
    """A service too old for the route, or simply gone, must not fail a stop.

    404 is the one a real deployment answers: the image before session memory
    has no ``/sessions`` route at all, and a capture stopping against it must
    still stop. 405 is what a route that exists for another method would say,
    and 500 stands for the service breaking while being told something it will
    forget by itself in an hour anyway.
    """

    def refuse(request: httpx.Request) -> httpx.Response:
        if request.method == "DELETE":
            return httpx.Response(status, text="no such route")
        return httpx.Response(200, json={"segments": []})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(refuse), base_url="http://diar"
    ) as client:
        diarizer = RemoteDiarizer("http://diar", client=client)
        await diarizer.diarize(b"", session_id="s-42")
        await diarizer.aclose()


async def test_a_service_restart_mid_session_is_logged_once() -> None:
    """A restart renumbers a session's speakers, and only this can show it.

    The service keeps the bank in its own process memory, so the turn after a
    restart starts at Speaker 0 again and one label ends up naming two people.
    The labels themselves cannot say so, since "Speaker 0" is exactly what a
    healthy service answers; a generation that changes with the process can.
    Once per restart, not once per utterance: the second half of the session is
    not news four hundred times.
    """
    generations = iter(["g1", "g1", "g2", "g2"])

    def answer(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"segments": [], "generation": next(generations)})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(answer), base_url="http://diar"
    ) as client:
        diarizer = RemoteDiarizer("http://diar", client=client)
        with capture_logs() as logs:
            for _ in range(4):
                await diarizer.diarize(b"", session_id="s-42")

    restarted = [line for line in logs if line["event"] == "diarization.service_restarted"]
    assert len(restarted) == 1
    assert restarted[0]["session_id"] == "s-42"
    assert restarted[0]["previous_generation"] == "g1"
    assert restarted[0]["generation"] == "g2"


async def test_a_service_that_stamps_no_generation_is_never_called_restarted() -> None:
    """An older service says nothing about its process, which is not a restart."""

    def answer(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"segments": []})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(answer), base_url="http://diar"
    ) as client:
        diarizer = RemoteDiarizer("http://diar", client=client)
        with capture_logs() as logs:
            await diarizer.diarize(b"", session_id="s-42")
            await diarizer.diarize(b"", session_id="s-42")

    assert [line for line in logs if line["event"] == "diarization.service_restarted"] == []


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


async def test_probe_says_when_a_service_does_not_remember_speakers() -> None:
    """The silent failure this exists for: an app newer than its diarizer.

    That older image accepts the extra ``session_id`` form field and ignores
    it, and answers 404 to the delete this client already swallows, so the app
    goes back to labelling every utterance "Speaker 0" with no error anywhere.
    Both services answer 200 to a health check, so a flag in the body is the
    only thing that separates them, and a green badge is the one verdict this
    must not give.
    """

    def old_service(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "ok"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(old_service), base_url="http://diar"
    ) as client:
        report = await probe_diarizer("http://diar", client=client)

    assert report.status is HealthStatus.DEGRADED
    assert "Speaker 0" in (report.detail or "")


async def test_probe_does_not_read_an_unreadable_health_body_as_a_missing_flag() -> None:
    """Tolerant in the direction loreline.health insists on.

    A body that is not JSON is one this cannot read, not proof of a service
    without session memory, and calling a working diarizer broken is the worse
    of the two errors.
    """

    def terse(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="ok")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(terse), base_url="http://diar"
    ) as client:
        report = await probe_diarizer("http://diar", client=client)

    assert report.status is HealthStatus.HEALTHY


def test_create_diarizer_remote_requires_endpoint() -> None:
    config = DiarizationConfig(mode=DiarizationMode.REMOTE)
    try:
        create_diarizer(config)
    except ValueError as exc:
        assert "endpoint" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected ValueError")
