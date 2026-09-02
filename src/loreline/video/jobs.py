"""Video-generation jobs.

Turns a session summary into a video via OpenRouter's ``/videos`` API. Runs as
in-process ``asyncio`` tasks with state in the ``video_jobs`` table - the same
shape as :mod:`loreline.reprocess.jobs`, for the same reason: the work outlives
the HTTP request that started it.

What differs from re-processing is *where* the waiting happens. A re-transcribe
is slow because this process is doing the work; a generation is slow because
somebody else's GPU is, and all this process does is poll. Hence the backoff
loop below and the deadline that stops it - a generation that never finishes
must fail the job rather than poll until the process dies.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from loreline.logging import get_logger
from loreline.models import JobStatus, ProviderConfig, VideoJob, VideoModelInfo
from loreline.video.client import (
    ClientFactory,
    VideoError,
    build_payload,
    download_video,
    list_video_models,
    poll_generation,
    start_generation,
    supports_video,
)

if TYPE_CHECKING:
    from loreline.persistence import ProviderRepository, SessionRepository, VideoRepository
    from loreline.secrets import SecretStore
    from loreline.video.store import VideoStore
    from loreline.web.schemas import VideoGenerateRequest

log = get_logger(__name__)

# Injected so tests can drive the polling loop without real delays; production
# always passes asyncio.sleep.
SleepFn = Callable[[float], Awaitable[None]]

# Polling cadence. Generations take minutes, so the first checks are spaced
# generously rather than hammering the API for a result that cannot be ready;
# the interval grows to a ceiling so a slow model does not accumulate hundreds
# of calls.
_POLL_INITIAL_S = 5.0
_POLL_MAX_S = 30.0
_POLL_BACKOFF = 1.5
# Hard ceiling on one generation. Well past any current model's runtime, but
# finite: without it a job wedged upstream would poll forever.
_DEADLINE_S = 3600.0


class SessionNotFoundError(ValueError):
    """Raised when generating for a session id that does not exist."""


class ProviderNotFoundError(ValueError):
    """Raised when the chosen provider id does not exist."""


class ProviderNotVideoCapableError(ValueError):
    """Raised when the chosen provider cannot generate video (non-OpenRouter)."""


class EmptyPromptError(ValueError):
    """Raised when the request carries no prompt to generate from."""


class VideoManager:
    """Enqueue and run video-generation jobs."""

    def __init__(
        self,
        *,
        providers: ProviderRepository,
        sessions: SessionRepository,
        videos: VideoRepository,
        video_store: VideoStore,
        secrets: SecretStore,
        client_factory: ClientFactory | None = None,
        sleep: SleepFn = asyncio.sleep,
    ) -> None:
        self._providers = providers
        self._sessions = sessions
        self._videos = videos
        self._store = video_store
        self._secrets = secrets
        self._client_factory = client_factory
        self._sleep = sleep
        self._tasks: dict[str, asyncio.Task[None]] = {}

    async def list_models(self, provider: ProviderConfig) -> list[VideoModelInfo]:
        """Video models a provider offers, with each one's parameter support.

        On the manager rather than called straight from the route so every
        outbound video call goes through the same (injectable) HTTP client -
        otherwise the catalog would quietly bypass it and hit the network.
        """
        api_key = self._secrets.get(provider.auth_ref) if provider.auth_ref else None
        return await list_video_models(
            config=provider, api_key=api_key, client_factory=self._client_factory
        )

    async def enqueue(self, req: VideoGenerateRequest) -> VideoJob:
        """Validate inputs, create a job row, and spawn the runner task."""
        session = await self._sessions.get(req.session_id)
        if session is None:
            msg = f"unknown session {req.session_id!r}"
            raise SessionNotFoundError(msg)

        provider = await self._providers.get(req.provider_id)
        if provider is None:
            msg = f"unknown provider {req.provider_id!r}"
            raise ProviderNotFoundError(msg)
        if not supports_video(provider.kind):
            msg = f"provider {provider.name!r} cannot generate video"
            raise ProviderNotVideoCapableError(msg)

        prompt = req.prompt.strip()
        if not prompt:
            msg = "prompt is empty"
            raise EmptyPromptError(msg)

        job = VideoJob(
            id=uuid.uuid4().hex,
            session_id=req.session_id,
            provider_id=req.provider_id,
            model=req.model,
            prompt=prompt,
            duration=req.duration,
            resolution=req.resolution,
            aspect_ratio=req.aspect_ratio,
            generate_audio=req.generate_audio,
            seed=req.seed,
            status=JobStatus.QUEUED,
            created_at=time.time(),
        )
        await self._videos.create(job)
        task = asyncio.create_task(self._run(job, provider))
        self._tasks[job.id] = task
        task.add_done_callback(lambda _t: self._tasks.pop(job.id, None))
        log.info("video.enqueue", job_id=job.id, session_id=job.session_id, model=job.model)
        return job

    async def wait(self, job_id: str) -> None:
        """Await completion of a running job's task (no-op if unknown)."""
        task = self._tasks.get(job_id)
        if task is not None:
            await task

    async def reconcile(self) -> None:
        """Fail jobs left running/queued by a previous process (startup sweep)."""
        await self._videos.mark_interrupted()

    async def aclose(self) -> None:
        """Cancel and await any in-flight job tasks (on shutdown)."""
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    async def delete(self, job_id: str) -> None:
        """Remove a job row and its stored video."""
        self._store.delete(job_id)
        await self._videos.delete(job_id)

    async def _run(self, job: VideoJob, provider: ProviderConfig) -> None:
        job.status = JobStatus.RUNNING
        job.started_at = time.time()
        await self._videos.update(job)
        api_key = self._secrets.get(provider.auth_ref) if provider.auth_ref else None
        try:
            await self._generate(job, provider, api_key)
        except asyncio.CancelledError:
            # Shutdown, not failure - leave the row RUNNING so the next start's
            # reconcile() sweep marks it interrupted with everything else.
            raise
        except VideoError as exc:
            await self._fail(job, str(exc))
        except Exception as exc:  # pragma: no cover - unexpected, still must not wedge the row
            log.exception("video.job.unexpected_error", job_id=job.id)
            await self._fail(job, str(exc))

    async def _generate(self, job: VideoJob, provider: ProviderConfig, api_key: str | None) -> None:
        payload = build_payload(
            model=job.model,
            prompt=job.prompt,
            duration=job.duration,
            resolution=job.resolution,
            aspect_ratio=job.aspect_ratio,
            generate_audio=job.generate_audio,
            seed=job.seed,
        )
        job.remote_id = await start_generation(
            config=provider,
            api_key=api_key,
            payload=payload,
            client_factory=self._client_factory,
        )
        # Persisted before the first poll: if the process dies here the row
        # still records which upstream generation was paid for.
        await self._videos.update(job)
        log.info("video.job.submitted", job_id=job.id, remote_id=job.remote_id)

        await self._await_completion(job, provider, api_key)

        data = await download_video(
            config=provider,
            api_key=api_key,
            remote_id=job.remote_id,
            client_factory=self._client_factory,
        )
        path = self._store.write(job.id, data)
        job.video_path = str(path)
        job.status = JobStatus.DONE
        job.finished_at = time.time()
        await self._videos.update(job)
        log.info("video.job.done", job_id=job.id, bytes=len(data))

    async def _await_completion(
        self, job: VideoJob, provider: ProviderConfig, api_key: str | None
    ) -> None:
        """Poll until the generation finishes, fails, or outruns the deadline."""
        remote_id = job.remote_id
        if remote_id is None:  # pragma: no cover - _generate always sets it first
            msg = "no upstream job id to poll"
            raise VideoError(msg)
        deadline = time.monotonic() + _DEADLINE_S
        interval = _POLL_INITIAL_S
        while True:
            await self._sleep(interval)
            state = await poll_generation(
                config=provider,
                api_key=api_key,
                remote_id=remote_id,
                client_factory=self._client_factory,
            )
            if state.done:
                return
            if state.failed:
                # The provider's own message where there is one - "failed" on
                # its own tells the GM nothing about what to change.
                raise VideoError(state.error or f"generation {state.status}")
            if time.monotonic() > deadline:
                msg = f"generation still {state.status} after {int(_DEADLINE_S // 60)} minutes"
                raise VideoError(msg)
            interval = min(interval * _POLL_BACKOFF, _POLL_MAX_S)

    async def _fail(self, job: VideoJob, error: str) -> None:
        job.status = JobStatus.ERROR
        job.error = error
        job.finished_at = time.time()
        await self._videos.update(job)
        log.warning("video.job.failed", job_id=job.id, error=error)
