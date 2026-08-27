"""Post-session re-processing jobs.

Re-runs a stored session's audio through a (typically higher-quality or local)
STT backend, producing a new transcript version tagged
``REPROCESS_SOURCE_PREFIX + provider`` for future comparison against the live
transcript (kept out of the canonical view - see
``loreline.export.canonical_transcript`` - until a diff UI lands). Jobs run as
in-process ``asyncio`` tasks; state is tracked in the ``reprocess_jobs`` table.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from collections.abc import AsyncIterator, Callable
from typing import TYPE_CHECKING

from loreline.bus import EventBus
from loreline.diarization.merge import assign_speakers
from loreline.diarization.provider import create_diarizer
from loreline.logging import get_logger
from loreline.models import (
    DIARIZE_SOURCE,
    REPROCESS_SOURCE_PREFIX,
    DiarizationMode,
    JobStatus,
    ProviderKind,
    ReprocessJob,
    TranscriptEvent,
    rebase_transcript,
)
from loreline.stt.registry import create_backend
from loreline.stt.router import RouterConfig, SttRouter

if TYPE_CHECKING:
    from loreline.audio.chunker import Utterance
    from loreline.diarization.base import DiarizationProvider
    from loreline.models import DiarizationConfig, ProviderConfig
    from loreline.persistence import (
        AudioStore,
        GlossaryRepository,
        ProviderRepository,
        ReprocessRepository,
        SessionRepository,
        TranscriptRepository,
    )
    from loreline.secrets import SecretStore
    from loreline.stt.base import STTBackend
    from loreline.web.schemas import ReprocessRequest

log = get_logger(__name__)

BackendFactory = Callable[["ProviderConfig", "SecretStore"], "STTBackend"]
DiarizerFactory = Callable[["DiarizationConfig"], "DiarizationProvider"]


class SessionNotFoundError(ValueError):
    """Raised when re-processing a session id that does not exist."""


class AudioMissingError(ValueError):
    """Raised when a session has no stored audio to re-process."""


class ProviderNotFoundError(ValueError):
    """Raised when the chosen re-process provider id does not exist."""


class ReprocessManager:
    """Enqueue and run post-session re-processing jobs."""

    def __init__(
        self,
        *,
        providers: ProviderRepository,
        glossaries: GlossaryRepository,
        sessions: SessionRepository,
        transcripts: TranscriptRepository,
        reprocess: ReprocessRepository,
        secrets: SecretStore,
        audio_store: AudioStore,
        backend_factory: BackendFactory | None = None,
        diarizer_factory: DiarizerFactory | None = None,
    ) -> None:
        self._providers = providers
        self._glossaries = glossaries
        self._sessions = sessions
        self._transcripts = transcripts
        self._reprocess = reprocess
        self._secrets = secrets
        self._audio_store = audio_store
        self._backend_factory = backend_factory or create_backend
        self._diarizer_factory = diarizer_factory or create_diarizer
        self._tasks: dict[str, asyncio.Task[None]] = {}

    async def enqueue(self, req: ReprocessRequest) -> ReprocessJob:
        """Validate inputs, create a job row, and spawn the runner task."""
        session = await self._sessions.get(req.session_id)
        if session is None:
            msg = f"unknown session {req.session_id!r}"
            raise SessionNotFoundError(msg)
        if not self._audio_store.exists(req.session_id):
            msg = f"session {req.session_id!r} has no stored audio"
            raise AudioMissingError(msg)
        provider: ProviderConfig | None = None
        if req.operation == "transcribe":
            provider = await self._providers.get(req.provider_id)
            if provider is None:
                msg = f"unknown provider {req.provider_id!r}"
                raise ProviderNotFoundError(msg)
            if req.model:
                # Model is chosen on demand at enqueue time, overriding the stored default.
                provider = provider.model_copy(update={"model": req.model})

        job = ReprocessJob(
            id=uuid.uuid4().hex,
            session_id=req.session_id,
            provider_id=req.provider_id if req.operation == "transcribe" else "",
            operation=req.operation,
            diarization=req.diarization,
            status=JobStatus.QUEUED,
            created_at=time.time(),
        )
        await self._reprocess.create(job)
        campaign_id = session.campaign_id
        task = asyncio.create_task(self._run(job, provider, campaign_id, session.started_mono))
        self._tasks[job.id] = task
        task.add_done_callback(lambda _t: self._tasks.pop(job.id, None))
        log.info("reprocess.enqueue", job_id=job.id, session_id=job.session_id)
        return job

    async def wait(self, job_id: str) -> None:
        """Await completion of a running job's task (no-op if unknown)."""
        task = self._tasks.get(job_id)
        if task is not None:
            await task

    async def reconcile(self) -> None:
        """Fail jobs left running/queued by a previous process (startup sweep)."""
        await self._reprocess.mark_interrupted()

    async def aclose(self) -> None:
        """Cancel and await any in-flight job tasks (on shutdown)."""
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    async def _run(
        self,
        job: ReprocessJob,
        provider: ProviderConfig | None,
        campaign_id: str | None,
        started_mono: float,
    ) -> None:
        job.status = JobStatus.RUNNING
        job.started_at = time.time()
        await self._reprocess.update(job)
        try:
            if job.operation == "diarize":
                job.segments_added = await self._diarize_session(job)
            else:
                job.segments_added = await self._transcribe_session(
                    job, provider, campaign_id, started_mono
                )
            job.status = JobStatus.DONE
        except Exception as exc:  # any failure marks the job errored
            job.status = JobStatus.ERROR
            job.error = str(exc)
            log.exception("reprocess.failed", job_id=job.id, operation=job.operation)
        finally:
            job.finished_at = time.time()
            await self._reprocess.update(job)

    async def _transcribe_session(
        self,
        job: ReprocessJob,
        provider: ProviderConfig | None,
        campaign_id: str | None,
        started_mono: float,
    ) -> int:
        """Re-run STT over the stored utterances, replacing this provider's rows."""
        if provider is None:
            msg = "transcribe requires a provider"
            raise ProviderNotFoundError(msg)
        backend = self._backend_factory(provider, self._secrets)
        diarizer = await self._build_diarizer(job.diarization)
        glossary = await self._glossaries.get_effective(campaign_id)
        bus: EventBus[TranscriptEvent] = EventBus()
        router = SttRouter(
            backend,
            bus,
            RouterConfig(
                session_id=job.session_id,
                glossary=glossary,
                diarization=job.diarization,
            ),
            diarizer=diarizer,
        )
        await self._transcripts.delete_source(
            job.session_id, f"{REPROCESS_SOURCE_PREFIX}{job.provider_id}"
        )
        try:
            # Blocking file I/O (a whole session's utterances) off the event loop.
            utterances = await asyncio.to_thread(self._audio_store.read_utterances, job.session_id)
            return await self._drive(router, bus, utterances, job.provider_id, started_mono)
        finally:
            await _aclose(backend)
            await _aclose(diarizer)

    async def _build_diarizer(self, config: DiarizationConfig) -> DiarizationProvider:
        """Construct the diarizer for a reprocess op.

        OpenAI batch diarization needs an OpenAI API key. Rather than only reading
        the ``OPENAI_API_KEY`` env var, reuse the key the user already stored on a
        configured OpenAI provider (secret store); the env var stays the fallback.
        """
        if config.mode == DiarizationMode.OPENAI:
            from loreline.diarization.openai_diarizer import OpenAIDiarizer  # noqa: PLC0415

            key = _resolve_openai_key(await self._providers.list(), self._secrets)
            return OpenAIDiarizer(api_key=key)
        return self._diarizer_factory(config)

    async def _diarize_session(self, job: ReprocessJob) -> int:
        """Diarize the whole continuous session audio once and relabel the live
        transcript globally, giving stable speaker identity across the session."""
        session = await self._sessions.get(job.session_id)
        if session is None:
            msg = f"unknown session {job.session_id!r}"
            raise SessionNotFoundError(msg)
        # Blocking file I/O (the whole continuous session WAV) off the event loop.
        wav, sample_rate = await asyncio.to_thread(self._audio_store.read_wav, job.session_id)
        diarizer = await self._build_diarizer(job.diarization)
        try:
            segments = await diarizer.diarize(
                wav,
                sample_rate=sample_rate,
                min_speakers=job.diarization.min_speakers,
                max_speakers=job.diarization.max_speakers,
            )
        finally:
            await _aclose(diarizer)
        if not segments:
            return 0
        events = await self._transcripts.for_session(job.session_id)
        live = [event for event in events if event.source == session.primary_provider]
        relabeled = [
            assign_speakers(event, segments).model_copy(update={"source": DIARIZE_SOURCE})
            for event in live
        ]
        await self._transcripts.delete_source(job.session_id, DIARIZE_SOURCE)
        for event in relabeled:
            await self._transcripts.add(event)
        return len(relabeled)

    async def _drive(
        self,
        router: SttRouter,
        bus: EventBus[TranscriptEvent],
        utterances: list[Utterance],
        provider_id: str,
        started_mono: float,
    ) -> int:
        """Subscribe first, then run the router, persisting every emitted event.

        Subscribing before the router runs guarantees no published event (or the
        close sentinel) is missed.
        """
        source = f"{REPROCESS_SOURCE_PREFIX}{provider_id}"
        count = 0
        async with bus.subscribe(reliable=True) as stream:
            run_task = asyncio.create_task(_run_then_close(router, utterances, bus))
            try:
                async for event in stream:
                    rebased = rebase_transcript(event, started_mono)
                    tagged = rebased.model_copy(update={"source": source})
                    await self._transcripts.add(tagged)
                    count += 1
            finally:
                await run_task
        return count


async def _run_then_close(
    router: SttRouter, utterances: list[Utterance], bus: EventBus[TranscriptEvent]
) -> None:
    try:
        await router.run(_aiter(utterances))
    finally:
        await bus.aclose()


async def _aiter(items: list[Utterance]) -> AsyncIterator[Utterance]:
    for item in items:
        yield item


async def _aclose(obj: object) -> None:
    closer = getattr(obj, "aclose", None)
    if closer is not None:
        try:
            await closer()
        except Exception:  # cleanup must never mask the job result
            log.warning("reprocess.aclose.failed")


def _resolve_openai_key(providers: list[ProviderConfig], secrets: SecretStore) -> str | None:
    """Return a configured OpenAI provider's stored API key, if any.

    Lets OpenAI batch diarization reuse the key the user already saved for their
    OpenAI provider instead of requiring a separate ``OPENAI_API_KEY`` env var.
    """
    for provider in providers:
        if provider.kind == ProviderKind.OPENAI and provider.auth_ref:
            key = secrets.get(provider.auth_ref)
            if key:
                return key
    return None
