"""Integration test for the AssemblyAI batch (async) connector.

Drives the backend against an ``httpx.MockTransport`` standing in for
``api.assemblyai.com``. There is no AssemblyAI key in this environment, so these
assertions are the whole verification the connector has: they pin the three-step
flow we believe the documented API wants (upload, create, poll), the request
bodies, and the response shape, so the gap between belief and reality is one
test run once a key exists. See the hidden ``universal-2`` entry in
capabilities.yaml.

The polling half is the part worth testing hardest, because it is the part that
can fail quietly: a job that never finishes must time out rather than hang, and
giving up locally, whether by timeout or by cancellation, must delete the job
rather than leave it running with our audio.

Wire format per https://www.assemblyai.com/docs/api-reference/transcripts/submit
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from loreline.audio.chunker import Utterance
from loreline.health import HealthStatus
from loreline.models import Glossary, Protocol, ProviderConfig, ProviderKind, TranscriptEvent
from loreline.stt.backends.assemblyai_batch import AssemblyAIBatchBackend

BASE_URL = "https://api.assemblyai.com"
UPLOAD_URL = "https://cdn.assemblyai.com/upload/abc123"
JOB_ID = "job-1"


def _config() -> ProviderConfig:
    return ProviderConfig(
        id="aai-1",
        name="AssemblyAI",
        kind=ProviderKind.ASSEMBLYAI,
        protocol=Protocol.HTTP_BATCH,
        language="de",
    )


def _word(text: str, start: int, end: int, speaker: str | None = "A") -> dict[str, Any]:
    """One ``words[]`` row; start/end are milliseconds on this API."""
    row: dict[str, Any] = {"text": text, "start": start, "end": end, "confidence": 0.97}
    if speaker is not None:
        row["speaker"] = speaker
    return row


class _Api:
    """A scripted AssemblyAI: upload, create, then a canned poll sequence."""

    def __init__(self, *, polls: list[dict[str, Any]] | None = None) -> None:
        # Default: completed on the first look.
        self.polls = polls or [
            {
                "id": JOB_ID,
                "status": "completed",
                "text": "Hallo Welt",
                "words": [_word("Hallo", 100, 450), _word("Welt", 460, 900)],
            }
        ]
        self.requests: list[httpx.Request] = []
        self.poll_count = 0
        # Set to make the cleanup DELETE fail at the transport, which is what a
        # network that has gone away looks like from here.
        self.fail_delete = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if request.method == "POST" and path == "/v2/upload":
            return httpx.Response(200, json={"upload_url": UPLOAD_URL})
        if request.method == "POST" and path == "/v2/transcript":
            return httpx.Response(200, json={"id": JOB_ID, "status": "queued"})
        if request.method == "GET" and path == f"/v2/transcript/{JOB_ID}":
            index = min(self.poll_count, len(self.polls) - 1)
            self.poll_count += 1
            return httpx.Response(200, json=self.polls[index])
        if request.method == "DELETE" and path == f"/v2/transcript/{JOB_ID}":
            if self.fail_delete:
                raise httpx.ConnectError("connection refused", request=request)
            return httpx.Response(200, json={"id": JOB_ID, "status": "completed"})
        raise AssertionError(f"unexpected request: {request.method} {path}")

    @property
    def methods(self) -> list[tuple[str, str]]:
        return [(r.method, r.url.path) for r in self.requests]

    def body(self, index: int) -> dict[str, Any]:
        payload: dict[str, Any] = json.loads(self.requests[index].content)
        return payload

    def deleted(self) -> bool:
        return ("DELETE", f"/v2/transcript/{JOB_ID}") in self.methods


def _backend(
    api: _Api,
    *,
    model: str | None = "universal-2",
    poll_max_s: float = 0.01,
    job_timeout_s: float = 30.0,
) -> AssemblyAIBatchBackend:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(api.handler),
        base_url=BASE_URL,
        headers={"Authorization": "test-key"},
    )
    return AssemblyAIBatchBackend(
        _config(),
        model=model,
        client=client,
        poll_initial_s=0.001,
        poll_max_s=poll_max_s,
        job_timeout_s=job_timeout_s,
    )


async def _one(start: float = 0.0) -> AsyncIterator[Utterance]:
    yield Utterance(pcm=b"\x01\x00" * 1600, start=start, end=start + 0.1)


async def _run(
    api: _Api,
    *,
    model: str | None = "universal-2",
    glossary: Glossary | None = None,
    start: float = 0.0,
    job_timeout_s: float = 30.0,
) -> list[TranscriptEvent]:
    backend = _backend(api, model=model, job_timeout_s=job_timeout_s)
    try:
        return [
            event
            async for event in backend.transcribe(_one(start), session_id="s1", glossary=glossary)
        ]
    finally:
        await backend.aclose()


async def test_uploads_creates_and_polls_then_maps_words_onto_session_time() -> None:
    api = _Api(
        polls=[
            {"id": JOB_ID, "status": "queued"},
            {"id": JOB_ID, "status": "processing"},
            {
                "id": JOB_ID,
                "status": "completed",
                "text": "Hallo Welt",
                "words": [_word("Hallo", 100, 450), _word("Welt", 460, 900, speaker="B")],
            },
        ]
    )

    events = await _run(api, start=12.0)

    assert api.methods == [
        ("POST", "/v2/upload"),
        ("POST", "/v2/transcript"),
        ("GET", f"/v2/transcript/{JOB_ID}"),
        ("GET", f"/v2/transcript/{JOB_ID}"),
        ("GET", f"/v2/transcript/{JOB_ID}"),
    ]
    # A completed job is not deleted: the transcript is the vendor's record of
    # work already paid for, and re-processing may want to fetch it again.
    assert not api.deleted()

    upload = api.requests[0]
    assert upload.headers["content-type"] == "application/octet-stream"
    assert upload.content.startswith(b"RIFF")  # a WAV container, not raw PCM

    body = api.body(1)
    assert body["audio_url"] == UPLOAD_URL
    # Plural `speech_models` (a list) on the async API, against singular
    # `speech_model` on the streaming one.
    assert body["speech_models"] == ["universal-2"]
    assert body["language_code"] == "de"
    assert body["speaker_labels"] is True

    assert len(events) == 1
    event = events[0]
    assert event.source == "aai-1"
    assert event.is_final
    assert event.text == "Hallo Welt"
    # Milliseconds in the payload, seconds on the session clock.
    assert abs(event.words[0].start - 12.1) < 1e-6
    assert abs(event.words[0].end - 12.45) < 1e-6
    assert event.words[0].speaker == "Speaker A"
    assert event.words[1].speaker == "Speaker B"
    assert event.speaker == "Speaker A"


async def test_glossary_goes_in_keyterms_prompt_capped_for_this_model() -> None:
    """universal-2 documents 200 keyterms, a fifth of universal-3-5-pro's async
    ceiling, which is why the cap is per model rather than per vendor."""
    api = _Api()
    glossary = Glossary(campaign_id="c1", terms=[f"term{i}" for i in range(250)])

    await _run(api, glossary=glossary)

    terms: list[str] = api.body(1)["keyterms_prompt"]
    assert len(terms) == 200
    assert terms[0] == "term0"  # glossary order is priority order


async def test_no_glossary_omits_the_field() -> None:
    api = _Api()

    await _run(api)

    assert "keyterms_prompt" not in api.body(1)


async def test_unset_model_and_language_omit_their_fields() -> None:
    """Omitted, AssemblyAI applies its own model ladder and auto-detects the
    language; neither default belongs in this connector."""
    api = _Api()
    config = _config()
    config.language = ""
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(api.handler), base_url=BASE_URL, headers={}
    )
    backend = AssemblyAIBatchBackend(config, client=client, poll_initial_s=0.001)
    try:
        _ = [e async for e in backend.transcribe(_one(), session_id="s1")]
    finally:
        await backend.aclose()

    body = api.body(1)
    assert "speech_models" not in body
    assert "language_code" not in body


async def test_empty_transcript_yields_no_event() -> None:
    api = _Api(polls=[{"id": JOB_ID, "status": "completed", "text": "", "words": []}])

    assert await _run(api) == []


async def test_error_status_raises_with_the_vendor_message_and_deletes_the_job() -> None:
    """An `error` status is a normal outcome of this API, not an HTTP failure,
    so the reason has to be lifted out of the body."""
    api = _Api(polls=[{"id": JOB_ID, "status": "error", "error": "Audio file is corrupt"}])

    with pytest.raises(RuntimeError, match="Audio file is corrupt"):
        await _run(api)

    assert api.deleted()


async def test_a_job_that_never_finishes_times_out_and_is_deleted() -> None:
    """Re-processing has no deadline of its own, which is not the same as
    hanging forever: a job wedged in `queued` would otherwise stall a whole
    re-processing run with nothing to show for it."""
    api = _Api(polls=[{"id": JOB_ID, "status": "queued"}])

    with pytest.raises(TimeoutError, match="still 'queued'"):
        await _run(api, job_timeout_s=0.05)

    assert api.deleted()
    assert api.poll_count > 1  # it really polled rather than giving up at once


async def test_cancelling_mid_poll_deletes_the_job() -> None:
    """The router cancels an utterance that blew its deadline. Abandoning the
    coroutine there would leave a paid job running and our audio at the vendor,
    so the DELETE is shielded against that very cancellation."""
    api = _Api(polls=[{"id": JOB_ID, "status": "processing"}])
    backend = _backend(api)
    started = asyncio.Event()

    async def consume() -> None:
        async for _event in backend.transcribe(_one(), session_id="s1"):
            pass

    async def run() -> None:
        started.set()
        await consume()

    task = asyncio.create_task(run())
    await started.wait()
    for _ in range(1000):  # let it get properly into the polling loop
        if api.poll_count >= 2:
            break
        await asyncio.sleep(0.001)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await backend.aclose()

    assert api.deleted()


async def test_a_failing_cleanup_does_not_replace_the_original_error() -> None:
    """Cleanup runs on the way out of a failure, so anything it raises would
    otherwise land on the caller instead of the reason the utterance failed."""
    api = _Api(polls=[{"id": JOB_ID, "status": "error", "error": "Audio file is corrupt"}])
    api.fail_delete = True

    with pytest.raises(RuntimeError, match="Audio file is corrupt"):
        await _run(api)

    assert api.deleted()  # it tried


async def test_upload_failure_keeps_the_error_body() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v2/upload"
        return httpx.Response(401, json={"error": "Not authorized"})

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url=BASE_URL, headers={}
    )
    backend = AssemblyAIBatchBackend(_config(), client=client)
    try:
        with pytest.raises(httpx.HTTPStatusError, match="Not authorized"):
            _ = [e async for e in backend.transcribe(_one(), session_id="s1")]
    finally:
        await backend.aclose()


async def test_a_streaming_base_url_is_not_handed_to_the_http_client() -> None:
    """The streaming connector's default host is not even the same domain, so a
    stored wss:// base_url must not be inherited by this one."""
    config = _config()
    config.base_url = "wss://streaming.assemblyai.com/v3/ws"
    backend = AssemblyAIBatchBackend(config, api_key="k")
    try:
        assert str(backend._client.base_url) == BASE_URL  # pyright: ignore[reportPrivateUsage]
    finally:
        await backend.aclose()


async def test_health_lists_one_transcript() -> None:
    def ok(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v2/transcript"
        assert request.url.params["limit"] == "1"
        return httpx.Response(200, json={"transcripts": []})

    client = httpx.AsyncClient(transport=httpx.MockTransport(ok), base_url=BASE_URL, headers={})
    async with client:
        report = await AssemblyAIBatchBackend(_config(), client=client).health()
    assert report.status is HealthStatus.HEALTHY

    # Pinned from a live call with a bogus token.
    def unauthorized(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401, json={"error": "Authentication error, API token missing/invalid"}
        )

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(unauthorized), base_url=BASE_URL, headers={}
    )
    async with client:
        report = await AssemblyAIBatchBackend(_config(), client=client).health()
    assert report.status is HealthStatus.UNAUTHORIZED
    assert report.detail == "Authentication error, API token missing/invalid"
