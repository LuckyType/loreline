"""Tests for video generation: the OpenRouter client and the job manager.

Everything here runs offline against mocked ``httpx`` transports, and the
manager's polling loop is driven by an injected no-op sleep - the same
approach the rest of the suite uses, and the only way to test a
minutes-long asynchronous flow in milliseconds.
"""

from __future__ import annotations

import asyncio
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
from test_web_session import FakeBackend, capture_factory, fake_diarizers

import loreline.web.routes.video as video_route
from loreline import capabilities
from loreline.capability_config import CapabilityConfig
from loreline.llm import DEFAULT_SCENE_PROMPT, LLMError
from loreline.models import (
    Interaction,
    JobStatus,
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
    GenerationState,
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
from loreline.video.vendors import vendor_for
from loreline.web.app import AppState, create_app
from loreline.web.schemas import VideoGenerateRequest


def _openrouter() -> ProviderConfig:
    return ProviderConfig(id="v1", name="OpenRouter", kind=ProviderKind.OPENROUTER)


def _xai() -> ProviderConfig:
    return ProviderConfig(id="x1", name="xAI", kind=ProviderKind.XAI)


def _client(transport: httpx.MockTransport) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=transport, base_url="https://openrouter.ai/api/v1")


def _xai_client(transport: httpx.MockTransport) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=transport, base_url="https://api.x.ai/v1")


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
        ProviderConfig(id="chat", name="Ollama", kind=ProviderKind.OPENAI_COMPAT)
    )
    await repos.sessions.create(Session(id="s1", started_at=0.0))
    yield repos
    await db.close()


class TestCapability:
    def test_only_the_kinds_with_a_video_api_can_generate(self) -> None:
        """A plain OpenAI-compatible chat endpoint has no video API; offering
        it would produce a request that can only ever fail."""
        assert supports_video(ProviderKind.OPENROUTER) is True
        assert supports_video(ProviderKind.XAI) is True
        assert supports_video(ProviderKind.OPENAI_COMPAT) is False
        assert supports_video(ProviderKind.DEEPGRAM) is False

    def test_every_video_kind_has_an_adapter_to_speak_with(self) -> None:
        """The two halves of the same fact, checked against each other: the
        yaml decides which kinds are offered, vendors.py decides which can be
        spoken to, and a kind in one and not the other fails at submit time
        with the job already created and paid for."""
        for kind in ProviderKind:
            if supports_video(kind):
                assert vendor_for(kind) is not None


class TestPayload:
    def test_unset_parameters_are_omitted_not_nulled(self) -> None:
        """Video models differ in which parameters they accept at all, and one
        handed a parameter it does not support rejects the whole request - so
        an unset knob must be absent from the body, not present as null."""
        payload = build_payload(kind=ProviderKind.OPENROUTER, model="m", prompt="a wizard")
        assert payload == {"model": "m", "prompt": "a wizard"}

    def test_set_parameters_ride_along(self) -> None:
        payload = build_payload(
            kind=ProviderKind.OPENROUTER,
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

    def test_generate_audio_false_is_omitted_on_the_gateway(self) -> None:
        """False is OpenRouter's default; sending it explicitly would trip
        models that do not take the parameter at all."""
        payload = build_payload(
            kind=ProviderKind.OPENROUTER, model="m", prompt="p", generate_audio=False
        )
        assert "generate_audio" not in payload


class TestXaiPayload:
    """The same dialog, a different body. Worth its own class because one of
    these differences is silent: the audio default is inverted."""

    def test_generate_audio_is_always_explicit(self) -> None:
        """xAI defaults generate_audio to TRUE, so omitting it - which is right
        for OpenRouter - would hand back a clip with sound to a GM who turned it
        off, and nothing anywhere would report an error."""
        off = build_payload(kind=ProviderKind.XAI, model="m", prompt="p", generate_audio=False)
        on = build_payload(kind=ProviderKind.XAI, model="m", prompt="p", generate_audio=True)
        assert off["generate_audio"] is False
        assert on["generate_audio"] is True

    def test_seed_is_not_sent(self) -> None:
        """xAI documents no seed parameter, and an unknown field is a rejected
        request rather than an ignored one."""
        payload = build_payload(kind=ProviderKind.XAI, model="m", prompt="p", seed=42)
        assert "seed" not in payload

    def test_the_shared_knobs_ride_along(self) -> None:
        payload = build_payload(
            kind=ProviderKind.XAI,
            model="grok-imagine-video-1.5",
            prompt="a wizard",
            duration=8,
            resolution="720p",
            aspect_ratio="16:9",
            generate_audio=True,
        )
        assert payload == {
            "model": "grok-imagine-video-1.5",
            "prompt": "a wizard",
            "duration": 8,
            "resolution": "720p",
            "aspect_ratio": "16:9",
            "generate_audio": True,
        }

    def test_an_unset_knob_is_absent_rather_than_null(self) -> None:
        payload = build_payload(kind=ProviderKind.XAI, model="m", prompt="p")
        assert payload == {"model": "m", "prompt": "p", "generate_audio": False}


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

    async def test_a_live_catalogue_is_not_merged_with_the_curated_one(self) -> None:
        """Live wins outright where a vendor publishes a list and this file
        curates one too, which on OpenRouter is every time.

        The gateway's list is the newer of the two by construction, so a model
        it no longer serves must not survive in the picker because the yaml
        still names it - a job against a retired model is paid for and then
        fails. Read on the same body as the test above: veo-3.1 is curated for
        OpenRouter and absent from that body, and wan-3.0 is in both with
        different durations."""

        def handle(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=VIDEO_BODY)

        models = await list_video_models(
            config=_openrouter(),
            api_key="k",
            client_factory=lambda: _client(httpx.MockTransport(handle)),
        )
        curated = capabilities.curated_models(ProviderKind.OPENROUTER, Interaction.VIDEO)
        assert "google/veo-3.1" in curated
        assert [m.id for m in models] == ["alibaba/wan-3.0", "openai/sora-2-pro"]
        wan = next(m for m in models if m.id == "alibaba/wan-3.0")
        # The vendor's durations, not the file's much longer list for the same id.
        assert wan.supported_durations == [4, 8]

    async def test_unreachable_provider_falls_back_to_the_curated_list(self) -> None:
        """Best effort, like the chat model list, and by the same rule: a
        vendor that cannot be reached yields the curated models rather than an
        empty dialog. Generation itself does not need the catalogue, so a GM
        whose gateway is having a bad minute can still start a clip."""
        transport = httpx.MockTransport(lambda _r: httpx.Response(500))
        models = await list_video_models(
            config=_openrouter(), api_key="k", client_factory=lambda: _client(transport)
        )
        expected = capabilities.curated_models(ProviderKind.OPENROUTER, Interaction.VIDEO)
        assert expected, "openrouter curates no video models; this test needs rewriting"
        # In the file's order, which is written deliberately, and not sorted.
        assert [m.id for m in models] == expected


class TestCuratedVideoModels:
    """The other half of the same list: what the dialog gets for a vendor that
    publishes no video catalogue at all.

    xAI is that vendor - its ``GET /v1/models`` is the chat roster, and the
    yaml says so where it curates grok-imagine-video-1.5 by hand. This used to
    return nothing, so a working xAI row showed "No video models available"
    while ``POST /api/video`` generated a clip from that same model happily.
    """

    @staticmethod
    def _with_hidden(cfg: CapabilityConfig, kind: ProviderKind, model_id: str) -> CapabilityConfig:
        """The same config with one model hidden, as a release gate would be.

        ``default`` goes with it because the loader refuses a hidden default,
        and a kind whose every video model is hidden needs none: nothing is
        offered for that interaction any more.
        """
        spec = cfg.providers[kind]
        models = [
            m.model_copy(update={"hidden": True, "default": False}) if m.id == model_id else m
            for m in spec.models
        ]
        providers = dict(cfg.providers)
        providers[kind] = spec.model_copy(update={"models": models})
        return cfg.model_copy(update={"providers": providers})

    @staticmethod
    def _unreachable() -> httpx.AsyncClient:
        """A transport that answers nothing, so a test here cannot start
        depending on a network the fallback exists to do without."""
        return _xai_client(httpx.MockTransport(lambda _r: httpx.Response(500)))

    async def test_a_vendor_with_no_catalogue_offers_its_curated_models(self) -> None:
        """The parameters come off the file's ``video`` block, field by field,
        because they are the only description of this model anyone has."""
        seen: list[str] = []

        def handle(request: httpx.Request) -> httpx.Response:
            seen.append(str(request.url))
            return httpx.Response(200, json={"data": []})

        models = await list_video_models(
            config=_xai(),
            api_key="k",
            client_factory=lambda: _xai_client(httpx.MockTransport(handle)),
        )
        # Nothing to ask: the kind declares no video catalog surface, so the
        # fallback is reached without a round trip rather than after one.
        assert seen == []
        assert [m.id for m in models] == ["grok-imagine-video-1.5"]
        grok = models[0]
        assert grok.name == "Grok Imagine Video 1.5"  # the file's label, not the id
        assert grok.supported_durations == list(range(1, 16))
        assert grok.supported_resolutions == ["480p", "720p", "1080p"]
        assert grok.supported_aspect_ratios == ["16:9", "9:16", "1:1", "4:3", "3:4", "3:2", "2:3"]
        assert grok.generate_audio is True  # `audio: true` in the file
        # Neither is written down for a curated model, so neither is promised:
        # the file records no seed knob and no explicit WxH sizes.
        assert grok.seed is False
        assert grok.supported_sizes is None

    async def test_the_curated_row_carries_what_a_real_generation_needs(self) -> None:
        """The verified case, end to end through the picker: the values a real
        xAI job was submitted with are all offered by the row the dialog gets."""
        models = await list_video_models(
            config=_xai(), api_key="k", client_factory=self._unreachable
        )
        grok = next(m for m in models if m.id == "grok-imagine-video-1.5")
        assert grok.supported_durations is not None
        assert 2 in grok.supported_durations
        assert "480p" in (grok.supported_resolutions or [])
        assert "16:9" in (grok.supported_aspect_ratios or [])

    async def test_a_hidden_curated_model_is_never_offered(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``hidden`` is the release gate for a connector nobody has verified
        against the real API, and the fallback must not be a way around it.

        Read only: the loaded config is copied, the flag flipped in memory, and
        the process-wide cache restored by monkeypatch."""
        shipped = capabilities.config()
        curated = "grok-imagine-video-1.5"
        entry = next(m for m in shipped.providers[ProviderKind.XAI].models if m.id == curated)
        assert not entry.hidden, f"{curated} is hidden now; this guard needs rewriting"
        gated = self._with_hidden(shipped, ProviderKind.XAI, curated)
        monkeypatch.setattr(capabilities, "config", lambda: gated)

        models = await list_video_models(
            config=_xai(), api_key="k", client_factory=self._unreachable
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
            payload=build_payload(kind=ProviderKind.OPENROUTER, model="m", prompt="p"),
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


class TestXaiClientCalls:
    """The same three calls against xAI, which spells every one of them
    differently: a different submit path, a different id field, a different word
    for success, and the result at a URL instead of a path."""

    async def test_start_posts_to_the_generations_path_and_reads_request_id(self) -> None:
        seen: dict[str, object] = {}

        def handle(request: httpx.Request) -> httpx.Response:
            seen["path"] = request.url.path
            return httpx.Response(200, json={"request_id": "d97415a1-5796"})

        remote_id = await start_generation(
            config=_xai(),
            api_key="k",
            payload=build_payload(kind=ProviderKind.XAI, model="m", prompt="p"),
            client_factory=lambda: _xai_client(httpx.MockTransport(handle)),
        )

        assert remote_id == "d97415a1-5796"
        assert str(seen["path"]).endswith("/videos/generations")

    async def test_an_openrouter_shaped_answer_is_no_job_id(self) -> None:
        """The failure the vendor split exists to prevent, in one assertion: an
        "id" field is not what this vendor returns, and reading one would leave
        a job polling a handle the service never issued."""

        def handle(_r: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"id": "gen_1", "status": "pending"})

        with pytest.raises(VideoError, match="no job id"):
            await start_generation(
                config=_xai(),
                api_key="k",
                payload={"model": "m", "prompt": "p"},
                client_factory=lambda: _xai_client(httpx.MockTransport(handle)),
            )

    @pytest.mark.parametrize(
        ("status", "done", "failed"),
        [
            ("pending", False, False),
            # "done", not "completed": waiting for OpenRouter's word here would
            # poll until the hour deadline and then fail a finished generation.
            ("done", True, False),
            ("failed", False, True),
            ("expired", False, True),
        ],
    )
    async def test_poll_classifies_every_upstream_state(
        self, status: str, done: bool, failed: bool
    ) -> None:
        def handle(_r: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"status": status, "model": "m"})

        state = await poll_generation(
            config=_xai(),
            api_key="k",
            remote_id="req_1",
            client_factory=lambda: _xai_client(httpx.MockTransport(handle)),
        )
        assert (state.done, state.failed) == (done, failed)

    async def test_the_poll_carries_the_result_url_and_the_error_message(self) -> None:
        """Two things the gateway puts elsewhere: the address of the video, and
        a failure reason that is an object rather than a string."""

        def finished(_r: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={"status": "done", "video": {"url": "https://vidgen.x.ai/a/video.mp4"}},
            )

        def refused(_r: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, json={"status": "failed", "error": {"code": "x", "message": "prompt refused"}}
            )

        ok = await poll_generation(
            config=_xai(),
            api_key="k",
            remote_id="req_1",
            client_factory=lambda: _xai_client(httpx.MockTransport(finished)),
        )
        bad = await poll_generation(
            config=_xai(),
            api_key="k",
            remote_id="req_1",
            client_factory=lambda: _xai_client(httpx.MockTransport(refused)),
        )

        assert ok.video_url == "https://vidgen.x.ai/a/video.mp4"
        assert bad.error == "prompt refused"

    def test_the_result_url_is_fetched_without_the_row_credential(self) -> None:
        """The result sits on a storage host with the credential already in the
        URL, and such hosts reject a request that also carries an Authorization
        header. The gateway is the opposite case: it serves the bytes from its
        own API, behind the same key as everything else."""
        finished = GenerationState(
            status="done", done=True, video_url="https://vidgen.x.ai/a/video.mp4"
        )
        target = vendor_for(ProviderKind.XAI).content("req_1", finished)
        assert target.url == "https://vidgen.x.ai/a/video.mp4"
        assert target.authenticated is False

        gateway = vendor_for(ProviderKind.OPENROUTER).content("gen_1", None)
        assert gateway.url == "/videos/gen_1/content"
        assert gateway.authenticated is True

    async def test_the_download_asks_for_the_url_the_poll_returned(self) -> None:
        seen: list[httpx.Request] = []

        def handle(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, content=b"mp4-bytes")

        data = await download_video(
            config=_xai(),
            api_key="k",
            remote_id="req_1",
            state=GenerationState(status="done", done=True, video_url="https://vidgen.x.ai/v.mp4"),
            client_factory=lambda: _xai_client(httpx.MockTransport(handle)),
        )

        assert data == b"mp4-bytes"
        assert str(seen[0].url) == "https://vidgen.x.ai/v.mp4"

    async def test_a_finished_generation_with_no_url_fails_the_job(self) -> None:
        """There is no second place to look, so saying so names the vendor's
        broken promise instead of writing a zero-byte file."""
        with pytest.raises(VideoError, match="no video URL"):
            await download_video(
                config=_xai(),
                api_key="k",
                remote_id="req_1",
                state=GenerationState(status="done", done=True),
                client_factory=lambda: _xai_client(
                    httpx.MockTransport(lambda _r: httpx.Response(200, content=b"x"))
                ),
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

    async def test_deleting_a_running_job_stops_its_generation(
        self, video_repos: Repos, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A row deleted mid-generation takes the generation with it. Left to
        run, the poll loop would finish by writing a file that nothing names."""
        self._instant_polling(monkeypatch)

        def handle(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path.endswith("/videos"):
                return httpx.Response(200, json={"id": "gen_3", "status": "pending"})
            if path.endswith("/content"):
                return httpx.Response(200, content=b"\x00\x00\x00 ftypmp42")
            return httpx.Response(200, json={"id": "gen_3", "status": "in_progress"})

        manager = self._manager(tmp_path, httpx.MockTransport(handle), repos=video_repos)
        job = await manager.enqueue(
            VideoGenerateRequest(session_id="s1", provider_id="v1", model="m", prompt="p")
        )
        await asyncio.sleep(0)  # let the runner submit and start polling

        await manager.delete(job.id)

        assert await video_repos.videos.get(job.id) is None
        assert not VideoStore(tmp_path / "video").exists(job.id)
        await manager.wait(job.id)  # nothing is left running under that id

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
                json={"name": "OpenRouter", "kind": "openrouter"},
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
                json={"name": "Ollama", "kind": "openai_compat"},
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

    async def test_a_converted_prompt_records_the_model_and_what_it_read(
        self, client: AsyncClient
    ) -> None:
        """The scene is saved with the video: which model wrote it, and the
        recap it was condensed from. A hand-written prompt carries neither, and
        that absence is the signal - see VideoGenerateRequest."""
        session_id, provider_id = await self._setup(client)
        job = (
            await client.post(
                "/api/video",
                json={
                    "session_id": session_id,
                    "provider_id": provider_id,
                    "model": "m",
                    "prompt": "A lone rider on a burning bridge.",
                    "scene_model": "gpt-5.6-luna",
                    "scene_source": "The party did a lot, at length.",
                    "duration": 4,
                },
            )
        ).json()
        assert job["scene_model"] == "gpt-5.6-luna"
        assert job["scene_source"] == "The party did a lot, at length."

        await _ctx(client).video.wait(job["id"])
        # Read back out of SQLite, not out of the enqueue response.
        listed = (await client.get("/api/video", params={"session_id": session_id})).json()
        assert listed[0]["scene_model"] == "gpt-5.6-luna"
        assert listed[0]["scene_source"] == "The party did a lot, at length."

    async def test_a_hand_written_prompt_records_no_scene(self, client: AsyncClient) -> None:
        """Blank and absent are the same answer, so a client that always sends
        the fields cannot invent a conversion that never happened."""
        session_id, provider_id = await self._setup(client)
        job = (
            await client.post(
                "/api/video",
                json={
                    "session_id": session_id,
                    "provider_id": provider_id,
                    "model": "m",
                    "prompt": "a wizard",
                    "scene_model": "  ",
                    "scene_source": "",
                },
            )
        ).json()
        assert job["scene_model"] is None
        assert job["scene_source"] is None

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

    async def test_deleting_the_session_removes_its_videos(self, client: AsyncClient) -> None:
        """A session's generated videos go with it.

        The job rows already did (the table cascades on the session row) and
        the files did not: every deleted session left its ``.mp4`` files on
        disk with nothing that named them, which is the one kind of storage
        nothing will ever prune.
        """
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

        deleted = await client.post("/api/session/delete", json={"ids": [session_id]})
        assert deleted.status_code == 200
        assert not state.video_store.exists(job["id"])
        assert await state.video_jobs.get(job["id"]) is None


class TestSceneRoute:
    """``POST /api/video/scene``: the recap in the prompt box, condensed into one
    shot a video model can actually render.

    The LLM call itself is replaced wholesale - what matters here is which
    instructions it is handed, not what a model would answer.
    """

    @pytest_asyncio.fixture
    async def client(self, tmp_path: Path) -> AsyncIterator[AsyncClient]:
        settings = Settings(data_dir=tmp_path / "data", auth_password="", jwt_secret="x")
        app = create_app(settings)
        async with LifespanManager(app):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as ac:
                yield ac

    @staticmethod
    async def _llm(client: AsyncClient) -> str:
        return (
            await client.post(
                "/api/providers",
                json={"name": "LLM", "kind": "openai_compat", "base_url": "http://llm:1234/v1"},
            )
        ).json()["id"]

    @staticmethod
    def _fake_conversion(
        monkeypatch: pytest.MonkeyPatch, seen: dict[str, object], answer: str = "A lone rider."
    ) -> None:
        async def fake(**kwargs: object) -> str:
            seen.clear()
            seen.update(kwargs)
            return answer

        monkeypatch.setattr(video_route, "summarize_transcript", fake)

    async def test_converts_a_recap_into_a_scene(
        self, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: dict[str, object] = {}
        self._fake_conversion(monkeypatch, seen, answer="  A lone rider on a burning bridge.  ")
        llm = await self._llm(client)

        resp = await client.post(
            "/api/video/scene",
            json={"provider_id": llm, "model": "gpt-5.6-luna", "text": "The party did a lot."},
        )
        assert resp.status_code == 200
        # Trimmed, because it lands straight in a character-counted box.
        assert resp.json() == {
            "scene": "A lone rider on a burning bridge.",
            "model": "gpt-5.6-luna",
        }
        assert seen["transcript"] == "The party did a lot."
        assert seen["model"] == "gpt-5.6-luna"

    async def test_blank_text_is_refused(self, client: AsyncClient) -> None:
        """Nothing to convert is a 400, not a paid round trip that answers about
        an empty recap."""
        llm = await self._llm(client)
        resp = await client.post(
            "/api/video/scene", json={"provider_id": llm, "model": "m", "text": "   "}
        )
        assert resp.status_code == 400

    async def test_stored_prompt_wins_over_the_built_in_one(
        self, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: dict[str, object] = {}
        self._fake_conversion(monkeypatch, seen)
        llm = await self._llm(client)
        body = {"provider_id": llm, "model": "m", "text": "a recap"}

        resp = await client.post("/api/video/scene", json=body)
        assert resp.status_code == 200
        assert seen["system_prompt"] == DEFAULT_SCENE_PROMPT  # nothing stored

        await client.put("/api/system/defaults", json={"scene_prompt": "Ein Bild, sonst nichts."})
        assert (await client.post("/api/video/scene", json=body)).status_code == 200
        assert seen["system_prompt"] == "Ein Bild, sonst nichts."

    async def test_the_style_shapes_the_instructions_and_only_the_request_supplies_it(
        self, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The look reaches the model as part of the scene instructions, so the
        text that lands in the box already reads that way - and it comes from
        the request, never from the stored default behind the GM's back, which
        is what lets them clear it for one video."""
        seen: dict[str, object] = {}
        self._fake_conversion(monkeypatch, seen)
        llm = await self._llm(client)
        await client.put("/api/system/defaults", json={"video_style": "gritty photoreal"})
        body: dict[str, object] = {"provider_id": llm, "model": "m", "text": "a recap"}

        assert (await client.post("/api/video/scene", json=body)).status_code == 200
        assert "gritty photoreal" not in str(seen["system_prompt"])

        styled = {**body, "style": "90s anime cel animation"}
        assert (await client.post("/api/video/scene", json=styled)).status_code == 200
        instructions = str(seen["system_prompt"])
        assert instructions.startswith(DEFAULT_SCENE_PROMPT)
        assert "90s anime cel animation" in instructions

    async def test_an_upstream_failure_is_a_502_carrying_the_reason(
        self, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The dialog shows this message inline and leaves the box alone, so it
        has to say what went wrong rather than read as a bug in Loreline."""

        async def fail(**_kwargs: object) -> str:
            raise LLMError("model not found")

        monkeypatch.setattr(video_route, "summarize_transcript", fail)
        llm = await self._llm(client)
        resp = await client.post(
            "/api/video/scene", json={"provider_id": llm, "model": "nope", "text": "a recap"}
        )
        assert resp.status_code == 502
        assert resp.json()["detail"] == "model not found"

    async def test_a_non_llm_provider_is_refused(
        self, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: dict[str, object] = {}
        self._fake_conversion(monkeypatch, seen)
        stt = (
            await client.post("/api/providers", json={"name": "Deepgram", "kind": "deepgram"})
        ).json()["id"]
        resp = await client.post(
            "/api/video/scene", json={"provider_id": stt, "model": "m", "text": "a recap"}
        )
        assert resp.status_code == 400
        assert seen == {}


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
            diarizer_factory=fake_diarizers,
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
                json={"name": "Deepgram", "kind": "deepgram"},
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
