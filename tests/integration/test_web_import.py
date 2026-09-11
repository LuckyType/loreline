"""Integration tests for importing a recording as a session.

The point of every one of these is that an import is not special: the row it
writes, the WAV and index beside it, the transcription it takes and the delete
that removes it are the same ones a capture gets (see ``docs/adr/0008``).
"""

from __future__ import annotations

import io
import json
import struct
import time
import wave
from collections.abc import AsyncIterator
from pathlib import Path
from typing import NamedTuple

import pytest
import pytest_asyncio
from asgi_lifespan import LifespanManager
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response
from test_web_session import (  # type: ignore[import-not-found]
    FakeBackend,
    fake_diarizers,
)

from loreline.audio.chunker import SpeechDetector
from loreline.session.importing import UploadTooLargeError, receive_upload
from loreline.settings import Settings
from loreline.web.app import AppState, create_app

_MODEL = "fake-model"
_SAMPLE_RATE = 16000


def _any_byte_detector(_sample_rate: int) -> SpeechDetector:
    """Stand in for silero: a frame with any nonzero byte counts as speech.

    The real VAD is an optional native dependency and its judgement is not what
    these tests are about - that the recording is cut into utterances at all,
    and that the spans start where the audio does, is.
    """
    return any


def wav_bytes(*, seconds: float = 1.0, rate: int = _SAMPLE_RATE, channels: int = 1) -> bytes:
    """A PCM16 WAV of the given shape, loud enough for the detector above."""
    frames = int(seconds * rate)
    pcm = b"".join(
        struct.pack("<" + "h" * channels, *([(i % 4000) - 2000] * channels)) for i in range(frames)
    )
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(pcm)
    return buffer.getvalue()


def build_app(settings: Settings) -> FastAPI:
    return create_app(
        settings,
        backend_factory=FakeBackend,  # type: ignore[arg-type]
        diarizer_factory=fake_diarizers,
    )


class Harness(NamedTuple):
    """A running app, as the two handles a test needs on it."""

    client: AsyncClient
    ctx: AppState


@pytest_asyncio.fixture
async def harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Harness]:
    monkeypatch.setattr("loreline.session.importing.default_detector", _any_byte_detector)
    settings = Settings(data_dir=tmp_path / "data", auth_password="", jwt_secret="t")
    app = build_app(settings)
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            ctx: AppState = app.state.ctx  # pyright: ignore[reportAny]
            yield Harness(client, ctx)


async def create_provider(client: AsyncClient) -> str:
    resp = await client.post("/api/providers", json={"name": "Fake", "kind": "openai_compat"})
    return resp.json()["id"]


async def post_import(
    client: AsyncClient,
    *,
    content: bytes | None = None,
    filename: str = "session 12.wav",
    data: dict[str, str] | None = None,
) -> Response:
    body = wav_bytes() if content is None else content
    return await client.post(
        "/api/session/import",
        files={"file": (filename, body, "audio/wav")},
        data=data or {},
    )


async def test_an_import_becomes_a_completed_session_with_audio_and_an_index(
    harness: Harness,
) -> None:
    """The whole contract: a row like a capture's, and the two files beside it."""
    resp = await post_import(
        harness.client, content=wav_bytes(seconds=2.0), data={"started_at": "1000.0"}
    )

    assert resp.status_code == 201
    body = resp.json()
    session = body["session"]
    assert body["job_id"] is None  # no transcribe block was sent
    assert session["status"] == "completed"
    assert session["origin"] == "import"
    assert session["import_name"] == "session 12.wav"
    assert session["started_at"] == 1000.0
    assert session["started_mono"] == 0.0
    # The recording's own length is what the session ran for.
    assert session["ended_at"] is not None
    assert abs(session["ended_at"] - 1002.0) < 0.1
    assert session["primary_provider"] is None

    sid = session["id"]
    assert harness.ctx.audio_store.exists(sid)  # WAV *and* index sidecar
    assert len(harness.ctx.audio_store.read_utterances(sid)) >= 1
    # And nothing left in the scratch space.
    assert list(harness.ctx.settings.imports_dir.glob("*")) == []

    detail = await harness.client.get(f"/api/session/{sid}")
    assert detail.status_code == 200
    assert abs(detail.json()["audio_duration_s"] - 2.0) < 0.1
    assert detail.json()["transcript"] == []  # an import has no live capture


async def test_an_import_with_no_start_time_lands_at_now(harness: Harness) -> None:
    """A recording carries no clock of its own, so "now" is the honest default."""
    before = time.time()

    resp = await post_import(harness.client)

    assert resp.status_code == 201
    assert resp.json()["session"]["started_at"] >= before


async def test_a_transcribe_block_starts_the_run_in_the_same_request(harness: Harness) -> None:
    """Transcribing an import is an ordinary re-processing job, and the rows it
    writes start where the recording does - which is what ``started_mono = 0``
    on the row buys, and the one thing an import's timestamps could get wrong."""
    pid = await create_provider(harness.client)

    resp = await post_import(
        harness.client,
        content=wav_bytes(seconds=2.0),
        data={"transcribe": json.dumps({"provider_id": pid, "model": _MODEL})},
    )

    assert resp.status_code == 201
    job_id = resp.json()["job_id"]
    assert job_id
    sid = resp.json()["session"]["id"]
    await harness.ctx.reprocess.wait(job_id)

    job = (await harness.client.get(f"/api/reprocess/{job_id}")).json()
    assert job["status"] == "done"
    assert job["segments_added"] >= 1
    assert job["model"] == _MODEL

    version = await harness.client.get(f"/api/session/{sid}/transcript", params={"version": job_id})
    rows = version.json()
    assert rows
    assert 0.0 <= rows[0]["start_ts"] < 1.0  # the session clock is the WAV's clock
    assert rows[0]["text"] == "hello world"

    export = await harness.client.get(
        f"/api/session/{sid}/export", params={"fmt": "txt", "version": job_id}
    )
    assert "hello world" in export.text


async def test_an_unknown_provider_is_refused_before_the_upload_is_read(harness: Harness) -> None:
    """A typo must not cost a ten minute upload to discover."""
    resp = await post_import(
        harness.client, data={"transcribe": json.dumps({"provider_id": "nope", "model": _MODEL})}
    )

    assert resp.status_code == 404
    assert "nope" in resp.json()["detail"]
    assert (await harness.client.get("/api/session")).json() == []  # nothing was stored


async def test_an_import_joins_the_campaign_it_names(harness: Harness) -> None:
    """And an id no campaign answers to is refused, not stored.

    The import dialog offers the campaigns by name, so a bad id only arrives
    from a client of somebody's own - but storing it would produce exactly the
    unresolvable ``campaign_id`` campaigns exist to end, and it would also
    decide which glossary a transcribe block in the same request runs with.
    """
    campaign_id = (await harness.client.post("/api/campaigns", json={"name": "Barovia"})).json()[
        "id"
    ]

    ok = await post_import(harness.client, data={"campaign_id": campaign_id})
    assert ok.status_code == 201
    assert ok.json()["session"]["campaign_id"] == campaign_id

    refused = await post_import(harness.client, data={"campaign_id": "ghost"})
    assert refused.status_code == 404
    assert len((await harness.client.get("/api/session")).json()) == 1  # only the good one


async def test_a_transcribe_block_that_names_no_model_is_refused(harness: Harness) -> None:
    """The provider row carries no model, so there is nothing to fall back to."""
    pid = await create_provider(harness.client)

    resp = await post_import(harness.client, data={"transcribe": json.dumps({"provider_id": pid})})

    assert resp.status_code == 422
    assert "model" in resp.json()["detail"]
    assert (await harness.client.get("/api/session")).json() == []


async def test_a_file_that_is_not_audio_leaves_no_session_behind(harness: Harness) -> None:
    """A failed decode is a 422 and nothing else: no row, no WAV, no scratch file."""
    resp = await post_import(
        harness.client, content=b"the party went to the vault" * 500, filename="notes.txt"
    )

    assert resp.status_code == 422
    assert (await harness.client.get("/api/session")).json() == []
    assert list(harness.ctx.settings.imports_dir.glob("*")) == []
    assert list(harness.ctx.audio_store.root.glob("*")) == []


async def test_a_recording_past_the_size_limit_is_refused_with_413(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ceiling exists so one request cannot fill the disk."""
    monkeypatch.setattr("loreline.session.importing.default_detector", _any_byte_detector)
    settings = Settings(
        data_dir=tmp_path / "data", auth_password="", jwt_secret="t", import_max_mb=1
    )
    app = build_app(settings)
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # 60 s at 16 kHz mono is a little under 2 MB.
            resp = await post_import(client, content=wav_bytes(seconds=60.0))

            assert resp.status_code == 413
            assert "1 MB" in resp.json()["detail"]
            assert (await client.get("/api/session")).json() == []


async def test_a_recording_past_the_hour_limit_is_refused_with_422(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other ceiling: length, measured after decoding."""
    monkeypatch.setattr("loreline.session.importing.default_detector", _any_byte_detector)
    settings = Settings(
        data_dir=tmp_path / "data",
        auth_password="",
        jwt_secret="t",
        import_max_hours=1.0 / 3600,  # one second
    )
    app = build_app(settings)
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await post_import(client, content=wav_bytes(seconds=3.0))

            assert resp.status_code == 422
            assert "limit" in resp.json()["detail"]
            assert (await client.get("/api/session")).json() == []


async def test_deleting_an_import_takes_its_recording_with_it(harness: Harness) -> None:
    """Delete already removes WAV, index, transcripts, logs and videos; an
    imported session goes the same way, because it is the same kind of row."""
    resp = await post_import(harness.client)
    sid = resp.json()["session"]["id"]
    assert harness.ctx.audio_store.exists(sid)

    deleted = await harness.client.post("/api/session/delete", json={"ids": [sid]})

    assert deleted.status_code == 200
    assert (await harness.client.get("/api/session")).json() == []
    assert not harness.ctx.audio_store.wav_path(sid).exists()
    assert not harness.ctx.audio_store.index_path(sid).exists()


async def test_an_import_is_listed_with_its_origin_and_file_name(harness: Harness) -> None:
    """What the history page reads to draw the badge and the row's title."""
    await post_import(harness.client, filename="the vault.m4a")

    rows = (await harness.client.get("/api/session")).json()

    assert len(rows) == 1
    assert rows[0]["origin"] == "import"
    assert rows[0]["import_name"] == "the vault.m4a"


async def test_receive_upload_stops_at_the_ceiling(tmp_path: Path) -> None:
    """The streaming half of the size check, which is what actually enforces it:
    a client that under-reports its content-length still stops costing disk at
    the limit rather than at whatever it decided to send."""

    async def chunks() -> AsyncIterator[bytes]:
        for _ in range(10):
            yield b"x" * 1000

    with pytest.raises(UploadTooLargeError):
        await receive_upload(chunks(), tmp_path / "upload.bin", limit=2000)
