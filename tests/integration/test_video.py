"""Tests for video generation: the OpenRouter client and the job manager.

Everything here runs offline against mocked ``httpx`` transports, and the
manager's polling loop is driven by an injected no-op sleep - the same
approach the rest of the suite uses, and the only way to test a
minutes-long asynchronous flow in milliseconds.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import NamedTuple

import httpx
import pytest
import pytest_asyncio
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient
from test_catalog_reader import VIDEO_BODY
from test_web_session import FakeBackend, FakeDiarizer, capture_factory

from loreline.models import (
    Interaction,
    JobStatus,
    Protocol,
    ProviderConfig,
    ProviderKind,
    Session,
    VideoJob,
)
from loreline.persistence import (
    Database,
    ProviderRepository,
    SessionRepository,
    VideoRepository,
)
from loreline.secrets import SecretStore
from loreline.settings import Settings
from loreline.stt.catalog import list_models
from loreline.video.client import (
    VideoError,
    build_payload,
    download_video,
    list_video_models,
    poll_generation,
    start_generation,
    supports_video,
)
from loreline.video.jobs import (
    EmptyPromptError,
    ProviderNotFoundError,
    ProviderNotVideoCapableError,
    SessionNotFoundError,
    VideoManager,
)
from loreline.video.store import VideoStore
from loreline.web.app import AppState, create_app
from loreline.web.schemas import VideoGenerateRequest


def _openrouter() -> ProviderConfig:
    return ProviderConfig(
        id="v1", name="OpenRouter", kind=ProviderKind.OPENROUTER, protocol=Protocol.HTTP_BATCH
    )


def _client(transport: httpx.MockTransport) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=transport, base_url="https://openrouter.ai/api/v1")


class Repos(NamedTuple):
    """The collaborators VideoManager needs, wired to a real SQLite file."""

    providers: ProviderRepository
    sessions: SessionRepository
    videos: VideoRepository
    secrets: SecretStore


@pytest_asyncio.fixture
async def video_repos(tmp_path: Path) -> AsyncIterator[Repos]:
    """A live SQLite DB with one session and two providers (an OpenRouter one
    and a chat-only one), plus the repositories the manager needs."""
    db = Database(tmp_path / "test.db")
    await db.connect()
    repos = Repos(
        providers=ProviderRepository(db),
        sessions=SessionRepository(db),
        videos=VideoRepository(db),
        secrets=SecretStore(tmp_path / "secrets.json"),
    )
    await repos.providers.upsert(_openrouter())
    await repos.providers.upsert(
        ProviderConfig(
            id="chat", name="Ollama", kind=ProviderKind.OPENAI_COMPAT, protocol=Protocol.HTTP_BATCH
        )
    )
    await repos.sessions.create(Session(id="s1", started_at=0.0))
    yield repos
    await db.close()


class TestCapability:
    def test_only_openrouter_can_generate_video(self) -> None:
        """A plain OpenAI-compatible chat endpoint has no video API; offering
        it would produce a request that can only ever fail."""
        assert supports_video(ProviderKind.OPENROUTER) is True
        assert supports_video(ProviderKind.OPENAI_COMPAT) is False
        assert supports_video(ProviderKind.DEEPGRAM) is False


class TestPayload:
    def test_unset_parameters_are_omitted_not_nulled(self) -> None:
        """Video models differ in which parameters they accept at all, and one
        handed a parameter it does not support rejects the whole request - so
        an unset knob must be absent from the body, not present as null."""
        payload = build_payload(model="m", prompt="a wizard")
        assert payload == {"model": "m", "prompt": "a wizard"}

    def test_set_parameters_ride_along(self) -> None:
        payload = build_payload(
            model="m",
            prompt="a wizard",
            duration=8,
            resolution="720p",
            aspect_ratio="16:9",
            generate_audio=True,
            seed=42,
        )
        assert payload == {
            "model": "m",
            "prompt": "a wizard",
            "duration": 8,
            "resolution": "720p",
            "aspect_ratio": "16:9",
            "generate_audio": True,
            "seed": 42,
        }

    def test_generate_audio_false_is_omitted(self) -> None:
        """False is the default everywhere; sending it explicitly would trip
        models that do not take the parameter at all."""
        assert "generate_audio" not in build_payload(model="m", prompt="p", generate_audio=False)


class TestModelCatalog:
    """The generate dialog's list is a projection of the one catalogue reader:
    what the vendor body becomes is pinned in test_catalog_reader.py, and what
    is pinned here is how a row lands in ``VideoModelInfo``."""

    async def test_rows_carry_per_model_parameter_support(self) -> None:
        """None means "this model takes no duration at all", which the form
        must be able to tell apart from an empty list of choices; a knob the
        vendor did not vouch for (``generate_audio: null``) is not offered."""

        def handle(request: httpx.Request) -> httpx.Response:
            assert request.url.path.endswith("/videos/models")
            assert request.headers.get("Authorization") == "Bearer k"
            return httpx.Response(200, json=VIDEO_BODY)

        models = await list_video_models(
            config=_openrouter(),
            api_key="k",
            client_factory=lambda: _client(httpx.MockTransport(handle)),
        )
        assert [m.id for m in models] == ["alibaba/wan-3.0", "openai/sora-2-pro"]  # sorted
        wan, sora = models
        assert (wan.name, wan.supported_durations, wan.supported_resolutions) == (
            "Wan 3.0",
            [4, 8],
            ["480p", "720p"],
        )
        assert (wan.generate_audio, wan.seed) == (True, True)
        assert sora.name == "openai/sora-2-pro"  # no name published: the id stands in
        assert sora.supported_durations == [4, 8, 12]
        assert sora.supported_sizes is None
        assert (sora.generate_audio, sora.seed) == (False, False)

    async def test_unreachable_provider_yields_an_empty_catalog(self) -> None:
        """Best effort, like the chat model list: the dialog should open and
        say there are no models, not fail the page."""
        transport = httpx.MockTransport(lambda _r: httpx.Response(500))
        models = await list_video_models(
            config=_openrouter(), api_key="k", client_factory=lambda: _client(transport)
        )
        assert models == []


class TestClientCalls:
    async def test_start_returns_the_upstream_job_id(self) -> None:
        seen: dict[str, object] = {}

        def handle(request: httpx.Request) -> httpx.Response:
            seen["path"] = request.url.path
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json={"id": "gen_1", "status": "pending"})

        remote_id = await start_generation(
            config=_openrouter(),
            api_key="k",
            payload=build_payload(model="m", prompt="p"),
            client_factory=lambda: _client(httpx.MockTransport(handle)),
        )
        assert remote_id == "gen_1"
        assert str(seen["path"]).endswith("/videos")

    async def test_start_surfaces_the_providers_own_error(self) -> None:
        """ "400 Bad Request" tells the GM nothing about what to change; the
        provider's message names the unsupported parameter."""

        def handle(_r: httpx.Request) -> httpx.Response:
            return httpx.Response(400, json={"error": {"message": "duration not supported"}})

        with pytest.raises(VideoError, match="duration not supported"):
            await start_generation(
                config=_openrouter(),
                api_key="k",
                payload={"model": "m", "prompt": "p"},
                client_factory=lambda: _client(httpx.MockTransport(handle)),
            )

    async def test_start_rejects_a_response_with_no_job_id(self) -> None:
        """Accepting this silently would leave a job with nothing to poll."""

        def handle(_r: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"status": "pending"})

        with pytest.raises(VideoError, match="no job id"):
            await start_generation(
                config=_openrouter(),
                api_key="k",
                payload={"model": "m", "prompt": "p"},
                client_factory=lambda: _client(httpx.MockTransport(handle)),
            )

    @pytest.mark.parametrize(
        ("status", "done", "failed"),
        [
            ("pending", False, False),
            ("in_progress", False, False),
            ("completed", True, False),
            ("failed", False, True),
            ("cancelled", False, True),
            # Expired means the result was collected too late - still no video.
            ("expired", False, True),
        ],
    )
    async def test_poll_classifies_every_upstream_state(
        self, status: str, done: bool, failed: bool
    ) -> None:
        def handle(_r: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"id": "gen_1", "status": status})

        state = await poll_generation(
            config=_openrouter(),
            api_key="k",
            remote_id="gen_1",
            client_factory=lambda: _client(httpx.MockTransport(handle)),
        )
        assert (state.done, state.failed) == (done, failed)

    async def test_download_rejects_an_empty_body(self) -> None:
        """A "ready" generation that returns nothing must fail the job rather
        than write a zero-byte file the UI would offer as a playable video."""

        def handle(_r: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"")

        with pytest.raises(VideoError, match="no content"):
            await download_video(
                config=_openrouter(),
                api_key="k",
                remote_id="gen_1",
                client_factory=lambda: _client(httpx.MockTransport(handle)),
            )


class TestJobManager:
    """The full enqueue → submit → poll → download → store flow.

    The polling intervals are monkeypatched to zero so a flow designed to take
    minutes runs in milliseconds; everything else is the real manager.
    """

    @staticmethod
    def _instant_polling(monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("loreline.video.jobs._POLL_INITIAL_S", 0.0)
        monkeypatch.setattr("loreline.video.jobs._POLL_MAX_S", 0.0)

    @staticmethod
    def _manager(tmp_path: Path, transport: httpx.MockTransport, repos: Repos) -> VideoManager:
        return VideoManager(
            providers=repos.providers,
            sessions=repos.sessions,
            videos=repos.videos,
            video_store=VideoStore(tmp_path / "video"),
            secrets=repos.secrets,
            client_factory=lambda: _client(transport),
        )

    async def test_happy_path_stores_a_playable_file(
        self, video_repos: Repos, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._instant_polling(monkeypatch)
        calls: list[str] = []

        def handle(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            calls.append(path)
            if path.endswith("/videos"):
                return httpx.Response(200, json={"id": "gen_1", "status": "pending"})
            if path.endswith("/content"):
                return httpx.Response(200, content=b"\x00\x00\x00 ftypmp42")
            # First poll still running, second one done - proves the loop loops.
            polls = [c for c in calls if c.endswith("/gen_1")]
            status = "completed" if len(polls) > 1 else "in_progress"
            return httpx.Response(200, json={"id": "gen_1", "status": status})

        manager = self._manager(tmp_path, httpx.MockTransport(handle), repos=video_repos)
        job = await manager.enqueue(
            VideoGenerateRequest(
                session_id="s1", provider_id="v1", model="m", prompt="a wizard", duration=8
            )
        )
        await manager.wait(job.id)

        stored = await video_repos.videos.get(job.id)
        assert stored is not None
        assert stored.status is JobStatus.DONE
        assert stored.remote_id == "gen_1"
        assert stored.video_path is not None
        assert stored.video_path.endswith(f"{job.id}.mp4")

    async def test_upstream_failure_lands_on_the_job_with_its_message(
        self, video_repos: Repos, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A generation that fails upstream must fail the row, carrying the
        provider's reason - not hang in "running" forever."""
        self._instant_polling(monkeypatch)

        def handle(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/videos"):
                return httpx.Response(200, json={"id": "gen_2", "status": "pending"})
            return httpx.Response(
                200, json={"id": "gen_2", "status": "failed", "error": "content policy"}
            )

        manager = self._manager(tmp_path, httpx.MockTransport(handle), repos=video_repos)
        job = await manager.enqueue(
            VideoGenerateRequest(session_id="s1", provider_id="v1", model="m", prompt="p")
        )
        await manager.wait(job.id)

        stored = await video_repos.videos.get(job.id)
        assert stored is not None
        assert stored.status is JobStatus.ERROR
        assert stored.error == "content policy"
        assert stored.video_path is None

    async def test_remote_id_is_persisted_before_polling_begins(
        self, video_repos: Repos, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If the process dies mid-generation the row must still record which
        upstream job was paid for."""
        self._instant_polling(monkeypatch)
        job_id = ""
        remote_at_first_poll: str | None = None

        async def capture(*_a: object, **_kw: object) -> None:
            nonlocal remote_at_first_poll
            stored = await video_repos.videos.get(job_id)
            remote_at_first_poll = stored.remote_id if stored else None
            msg = "stop here"
            raise VideoError(msg)

        def handle(_r: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"id": "gen_3", "status": "pending"})

        manager = self._manager(tmp_path, httpx.MockTransport(handle), repos=video_repos)
        monkeypatch.setattr("loreline.video.jobs.poll_generation", capture)
        job = await manager.enqueue(
            VideoGenerateRequest(session_id="s1", provider_id="v1", model="m", prompt="p")
        )
        job_id = job.id
        await manager.wait(job.id)

        assert remote_at_first_poll == "gen_3"

    async def test_rejects_a_provider_that_cannot_generate_video(
        self, video_repos: Repos, tmp_path: Path
    ) -> None:
        manager = self._manager(
            tmp_path, httpx.MockTransport(lambda _r: httpx.Response(200)), repos=video_repos
        )
        with pytest.raises(ProviderNotVideoCapableError):
            await manager.enqueue(
                VideoGenerateRequest(session_id="s1", provider_id="chat", model="m", prompt="p")
            )

    async def test_rejects_unknown_session_provider_and_blank_prompt(
        self, video_repos: Repos, tmp_path: Path
    ) -> None:
        manager = self._manager(
            tmp_path, httpx.MockTransport(lambda _r: httpx.Response(200)), repos=video_repos
        )
        with pytest.raises(SessionNotFoundError):
            await manager.enqueue(
                VideoGenerateRequest(session_id="nope", provider_id="v1", model="m", prompt="p")
            )
        with pytest.raises(ProviderNotFoundError):
            await manager.enqueue(
                VideoGenerateRequest(session_id="s1", provider_id="nope", model="m", prompt="p")
            )
        with pytest.raises(EmptyPromptError):
            await manager.enqueue(
                VideoGenerateRequest(session_id="s1", provider_id="v1", model="m", prompt="   ")
            )

    async def test_reconcile_fails_jobs_a_dead_process_left_behind(
        self, video_repos: Repos
    ) -> None:
        """Nothing else revisits a queued/running row after a restart - without
        this sweep it shows as "generating" forever."""
        await video_repos.videos.create(
            VideoJob(
                id="orphan",
                session_id="s1",
                provider_id="v1",
                model="m",
                prompt="p",
                status=JobStatus.RUNNING,
                created_at=0.0,
            )
        )
        await video_repos.videos.mark_interrupted()
        stored = await video_repos.videos.get("orphan")
        assert stored is not None
        assert stored.status is JobStatus.ERROR
        assert stored.error == "interrupted by a restart"


def _ctx(client: AsyncClient) -> AppState:
    """The app's shared state, for asserting on what a route actually stored."""
    return client._transport.app.state.ctx  # type: ignore[attr-defined,union-attr]


class TestVideoRoutes:
    """HTTP contract: enqueue, poll, play back, delete.

    Uses the real app with a mocked video transport injected through
    ``create_app(video_client_factory=…)`` - the same escape hatch the capture
    and STT factories use.
    """

    @pytest_asyncio.fixture
    async def client(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> AsyncIterator[AsyncClient]:
        monkeypatch.setattr("loreline.video.jobs._POLL_INITIAL_S", 0.0)
        monkeypatch.setattr("loreline.video.jobs._POLL_MAX_S", 0.0)

        def handle(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path.endswith("/videos/models"):
                return httpx.Response(
                    200,
                    json={
                        "data": [
                            {
                                "id": "alibaba/wan-3.0",
                                "name": "Wan 3.0",
                                "supported_durations": [4, 8],
                                "supported_resolutions": ["720p"],
                                "generate_audio": True,
                            }
                        ]
                    },
                )
            if path.endswith("/videos"):
                return httpx.Response(200, json={"id": "gen_1", "status": "pending"})
            if path.endswith("/content"):
                return httpx.Response(200, content=b"\x00\x00\x00 ftypmp42")
            return httpx.Response(200, json={"id": "gen_1", "status": "completed"})

        settings = Settings(data_dir=tmp_path / "data", auth_password="", jwt_secret="x")
        app = create_app(
            settings,
            video_client_factory=lambda: _client(httpx.MockTransport(handle)),
        )
        async with LifespanManager(app):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as ac:
                yield ac

    @staticmethod
    async def _setup(client: AsyncClient) -> tuple[str, str]:
        """A session row plus an OpenRouter provider; returns (session_id, provider_id).

        The session is inserted directly rather than captured: these tests are
        about video generation, and driving the real capture pipeline would
        drag in the audio/STT fakes for a row that only needs to exist.
        """
        provider = (
            await client.post(
                "/api/providers",
                json={"name": "OpenRouter", "kind": "openrouter", "protocol": "http_batch"},
            )
        ).json()
        state = _ctx(client)
        await state.sessions.create(Session(id="s1", started_at=0.0))
        return "s1", provider["id"]

    async def test_model_catalog_reports_per_model_parameters(self, client: AsyncClient) -> None:
        _, provider_id = await self._setup(client)
        resp = await client.get("/api/video/models", params={"provider_id": provider_id})
        assert resp.status_code == 200
        assert resp.json()[0]["supported_durations"] == [4, 8]

    async def test_model_catalog_rejects_a_non_video_provider(self, client: AsyncClient) -> None:
        chat = (
            await client.post(
                "/api/providers",
                json={"name": "Ollama", "kind": "openai_compat", "protocol": "http_batch"},
            )
        ).json()
        resp = await client.get("/api/video/models", params={"provider_id": chat["id"]})
        assert resp.status_code == 400

    async def test_generate_returns_202_then_completes_and_plays_back(
        self, client: AsyncClient
    ) -> None:
        session_id, provider_id = await self._setup(client)

        resp = await client.post(
            "/api/video",
            json={
                "session_id": session_id,
                "provider_id": provider_id,
                "model": "alibaba/wan-3.0",
                "prompt": "a wizard walks into a tavern",
                "duration": 8,
                "resolution": "720p",
            },
        )
        assert resp.status_code == 202
        job = resp.json()
        assert job["status"] == "queued"

        # The generation runs as a background task; wait it out through the
        # manager rather than sleeping on a wall clock.
        await _ctx(client).video.wait(job["id"])

        listed = (await client.get("/api/video", params={"session_id": session_id})).json()
        assert [j["status"] for j in listed] == ["done"]

        content = await client.get(f"/api/video/{job['id']}/content")
        assert content.status_code == 200
        assert content.headers["content-type"] == "video/mp4"
        assert content.content.startswith(b"\x00\x00\x00 ftyp")

    async def test_content_is_409_while_the_video_is_not_ready(self, client: AsyncClient) -> None:
        """The player must not be handed a URL that 500s mid-generation."""
        session_id, provider_id = await self._setup(client)
        job = (
            await client.post(
                "/api/video",
                json={
                    "session_id": session_id,
                    "provider_id": provider_id,
                    "model": "m",
                    "prompt": "p",
                },
            )
        ).json()
        state = _ctx(client)
        # Let the background generation finish first, then rewind the row:
        # setting the status while the job is still running just races it, and
        # the manager writes DONE over the change.
        await state.video.wait(job["id"])
        stored = await state.video_jobs.get(job["id"])
        assert stored is not None
        stored.status = JobStatus.RUNNING
        await state.video_jobs.update(stored)

        resp = await client.get(f"/api/video/{job['id']}/content")
        assert resp.status_code == 409

    async def test_blank_prompt_is_rejected(self, client: AsyncClient) -> None:
        session_id, provider_id = await self._setup(client)
        resp = await client.post(
            "/api/video",
            json={
                "session_id": session_id,
                "provider_id": provider_id,
                "model": "m",
                "prompt": "   ",
            },
        )
        assert resp.status_code == 400

    async def test_delete_removes_the_job_and_its_file(self, client: AsyncClient) -> None:
        session_id, provider_id = await self._setup(client)
        job = (
            await client.post(
                "/api/video",
                json={
                    "session_id": session_id,
                    "provider_id": provider_id,
                    "model": "m",
                    "prompt": "p",
                },
            )
        ).json()
        state = _ctx(client)
        await state.video.wait(job["id"])
        assert state.video_store.exists(job["id"])

        assert (await client.delete(f"/api/video/{job['id']}")).status_code == 200
        assert not state.video_store.exists(job["id"])
        assert (await client.get(f"/api/video/{job['id']}")).status_code == 404


class TestInteractionScoping:
    """OpenRouter's chat, transcription and video catalogues are disjoint, and
    each picker must be handed the right one."""

    async def test_transcription_catalogue_is_requested_for_stt_kinds(self) -> None:
        """The unfiltered /models is OpenRouter's *chat* catalogue; the
        transcription list only comes back when explicitly asked for."""
        seen: dict[str, str] = {}

        def handle(request: httpx.Request) -> httpx.Response:
            seen["query"] = request.url.query.decode()
            return httpx.Response(200, json={"data": [{"id": "openai/whisper-large-v3-turbo"}]})

        models = await list_models(
            kind=ProviderKind.OPENROUTER,
            base_url=None,
            api_key="k",
            interaction=Interaction.TRANSCRIBE,
            client_factory=lambda: _client(httpx.MockTransport(handle)),
        )
        assert seen["query"] == "output_modalities=transcription"
        assert [m.id for m in models] == ["openai/whisper-large-v3-turbo"]

    async def test_summarize_uses_the_plain_chat_catalogue(self) -> None:
        seen: dict[str, str] = {}

        def handle(request: httpx.Request) -> httpx.Response:
            seen["query"] = request.url.query.decode()
            return httpx.Response(200, json={"data": [{"id": "anthropic/claude-sonnet-4.5"}]})

        await list_models(
            kind=ProviderKind.OPENROUTER,
            base_url=None,
            api_key="k",
            interaction=Interaction.SUMMARIZE,
            client_factory=lambda: _client(httpx.MockTransport(handle)),
        )
        assert seen["query"] == ""


class TestLiveCaptureGuard:
    """A re-process-only provider must be refused at session start rather than
    failing somewhere inside a live capture."""

    async def test_start_rejects_a_reprocess_only_provider(
        self, tmp_path: Path
    ) -> AsyncIterator[None] | None:
        settings = Settings(data_dir=tmp_path / "data", auth_password="", jwt_secret="x")
        app = create_app(settings)
        async with LifespanManager(app):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as ac:
                provider = (
                    await ac.post(
                        "/api/providers",
                        json={
                            "name": "OpenRouter STT",
                            "kind": "openrouter",
                            "protocol": "http_batch",
                        },
                    )
                ).json()
                resp = await ac.post(
                    "/api/session/start",
                    json={
                        "primary_provider": provider["id"],
                        "model": "openai/whisper-large-v3-turbo",
                    },
                )
                assert resp.status_code == 400
                assert "re-processing" in resp.json()["detail"]
        return None


class TestInlineDiarizationGuard:
    """The UI hides "Inline (from STT)" for a model that returns no speakers;
    the server refuses it too, so a stale stored default or a direct API call
    cannot start a session that would quietly produce an unlabelled transcript."""

    @pytest_asyncio.fixture
    async def client(self, tmp_path: Path) -> AsyncIterator[AsyncClient]:
        # Fake capture + STT, so a session that gets *past* the guard does not
        # try to open a real microphone - CI runners have none, and a genuine
        # start would hang the lifespan on PortAudioError.
        settings = Settings(data_dir=tmp_path / "data", auth_password="", jwt_secret="x")
        app = create_app(
            settings,
            capture_factory=capture_factory,  # type: ignore[arg-type]
            backend_factory=FakeBackend,  # type: ignore[arg-type]
            diarizer_factory=lambda _cfg: FakeDiarizer(),
        )
        async with LifespanManager(app):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as ac:
                yield ac
                await ac.post("/api/session/stop")

    @staticmethod
    async def _deepgram(client: AsyncClient) -> str:
        # The provider row carries no model: the start request names it, which
        # is also what the guard reads.
        return (
            await client.post(
                "/api/providers",
                json={"name": "Deepgram", "kind": "deepgram", "protocol": "ws"},
            )
        ).json()["id"]

    async def test_rejects_inline_for_a_model_without_speaker_labels(
        self, client: AsyncClient
    ) -> None:
        provider = await self._deepgram(client)
        resp = await client.post(
            "/api/session/start",
            json={
                "primary_provider": provider,
                "model": "flux-general-en",
                "diarization": {"mode": "inline"},
            },
        )
        assert resp.status_code == 400
        assert "speaker labels" in resp.json()["detail"]

    async def test_other_diarization_modes_are_unaffected(self, client: AsyncClient) -> None:
        """Only inline is gated - remote diarization works off the audio, not
        the STT response, so the model's speaker labels are irrelevant to it.

        Runs against the fake capture pipeline, so it exercises the guard
        without touching audio hardware.
        """
        provider = await self._deepgram(client)
        resp = await client.post(
            "/api/session/start",
            json={
                "primary_provider": provider,
                "model": "flux-general-en",
                "diarization": {"mode": "none"},
            },
        )
        assert resp.status_code != 400
