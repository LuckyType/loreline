"""Post-session re-processing jobs.

Re-runs a stored session's audio through a (typically higher-quality or local)
STT backend, producing a new transcript *version* tagged
``REPROCESS_SOURCE_PREFIX + job_id`` - every run is kept, so the original and
any number of re-transcriptions stay comparable side by side (see
``loreline.export.variant_view``). A diarize job relabels ONE version
(``job.target``) into a ``DIARIZE_SOURCE_PREFIX + version`` copy, replacing
that version's previous diarization. Jobs run as in-process ``asyncio``
tasks; state is tracked in the ``reprocess_jobs`` table.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

from loreline.bus import EventBus
from loreline.diarization.merge import assign_speakers
from loreline.export import variant_rows
from loreline.logging import bind_log_context, get_logger
from loreline.models import (
    DIARIZE_SOURCE_PREFIX,
    ORIGINAL_VERSION,
    REPROCESS_SOURCE_PREFIX,
    JobStatus,
    ReprocessJob,
    TranscriptEvent,
    rebase_transcript,
)
from loreline.stt.registry import BackendFactory, create_backend
from loreline.stt.router import RouterConfig, SttRouter

if TYPE_CHECKING:
    from loreline.audio.chunker import Utterance
    from loreline.diarization.provider import BuildDiarizer
    from loreline.models import ProviderConfig
    from loreline.persistence import (
        AudioStore,
        GlossaryRepository,
        ProviderRepository,
        ReprocessRepository,
        SessionRepository,
        TranscriptRepository,
    )
    from loreline.secrets import SecretStore
    from loreline.web.schemas import ReprocessRequest

log = get_logger(__name__)


class SessionNotFoundError(ValueError):
    """Raised when re-processing a session id that does not exist."""


class AudioMissingError(ValueError):
    """Raised when a session has no stored audio to re-process."""


class ProviderNotFoundError(ValueError):
    """Raised when the chosen re-process provider id does not exist."""


class TargetNotFoundError(ValueError):
    """Raised when a diarize job targets a transcript version with no rows."""


class OriginalVersionError(ValueError):
    """Raised when asked to delete the original (live capture) transcript version."""


class VersionNotFoundError(ValueError):
    """Raised when deleting a transcript version that does not exist."""


class VersionBusyError(ValueError):
    """Raised when deleting a transcript version a job is still writing."""


# How often a running job's segment count is written back to its row. The
# session page polls the job list every 1.5s, so a tighter cadence would only
# add SQLite commits nobody can see.
_COUNT_INTERVAL_S = 1.0


class _LiveSegmentCount:
    """Publish a running job's ``segments_added`` while the job still runs.

    The count used to reach the row only when the job finished, so a
    re-transcription of an hours-long recording showed "running" and a flat 0
    for as long as it took. It is deliberately not turned into a percentage:
    two models segment the same audio differently, so there is no honest
    denominator to divide by - the number is only a rough feel for progress,
    read next to the other versions' counts.

    Ticks inside the interval are dropped; the caller's completion write (see
    ``ReprocessManager._run``) persists the final count either way, so no run
    can end on a stale number.
    """

    def __init__(self, job: ReprocessJob, repo: ReprocessRepository) -> None:
        self._job = job
        self._repo = repo
        self._last_write = 0.0

    async def set(self, count: int) -> None:
        self._job.segments_added = count
        now = time.monotonic()
        if now - self._last_write < _COUNT_INTERVAL_S:
            return
        self._last_write = now
        await self._repo.update(self._job)


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
        transcript_bus: EventBus[TranscriptEvent],
        diarizer_factory: BuildDiarizer,
        backend_factory: BackendFactory | None = None,
    ) -> None:
        self._providers = providers
        self._glossaries = glossaries
        self._sessions = sessions
        self._transcripts = transcripts
        self._reprocess = reprocess
        self._secrets = secrets
        self._audio_store = audio_store
        # The app-wide bus the WebSockets read, not the job-local one below: a
        # run writes into an existing session's history, so its events have to
        # reach whoever is watching that session (see _drive).
        self._bus = transcript_bus
        self._backend_factory = backend_factory or create_backend
        self._diarizer_factory = diarizer_factory
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
        elif req.target != ORIGINAL_VERSION:
            events = await self._transcripts.for_session(req.session_id)
            if not variant_rows(events, req.target):
                msg = f"unknown transcript version {req.target!r}"
                raise TargetNotFoundError(msg)

        job = ReprocessJob(
            id=uuid.uuid4().hex,
            session_id=req.session_id,
            provider_id=req.provider_id if req.operation == "transcribe" else "",
            operation=req.operation,
            # What the run will actually use, because it is the only model
            # there is: the request must name one for a transcribe job and the
            # provider row carries none. This used to record the row's model,
            # which was routinely null while a constant inside the connector
            # decided what really ran, so the row misreported the version's
            # provenance.
            model=req.model if req.operation == "transcribe" else None,
            target=req.target if req.operation == "diarize" else ORIGINAL_VERSION,
            # Same reason as `model`: the row says whether this version was
            # produced with the glossary, not merely what was asked for.
            use_glossary=req.use_glossary if req.operation == "transcribe" else True,
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

    async def delete_version(self, session_id: str, version: str) -> None:
        """Delete one transcript version: its segments, its diarization, its jobs.

        ``ORIGINAL_VERSION`` is refused. Every other version can be produced
        again from the stored audio; the original is the live capture, and the
        microphone is not coming back. The page hides the button, but the rule
        lives here - hiding a control is not a guarantee.

        A *diarize* job's id is deliberately not a version this accepts. A
        diarize job creates no transcript of its own: it rewrites ONE target
        version's rows into a ``DIARIZE_SOURCE_PREFIX`` copy that supersedes
        them on read (see ``loreline.export.variant_view``), so "deleting a
        diarize version" would mean dropping a relabeling while keeping the
        transcript, which is a different operation than the one this endpoint
        offers. What deleting a version does do is take its relabeling with it:
        ``diarize:<version>`` rows whose base version is gone are unreachable
        by every reader, and the diarize jobs that wrote them describe work on
        a transcript that no longer exists, so both go too.
        """
        if version == ORIGINAL_VERSION:
            msg = "the original transcript cannot be deleted"
            raise OriginalVersionError(msg)
        jobs = await self._reprocess.for_session(session_id)
        owner = next(
            (j for j in jobs if j.id == version and j.operation == "transcribe"),
            None,
        )
        if owner is None:
            msg = f"unknown transcript version {version!r}"
            raise VersionNotFoundError(msg)
        # A job still writing this version would keep inserting rows after the
        # delete, leaving segments no job row explains.
        pending = {JobStatus.QUEUED, JobStatus.RUNNING}
        busy = [
            j
            for j in jobs
            if j.status in pending
            and (j.id == version or (j.operation == "diarize" and j.target == version))
        ]
        if busy:
            msg = f"transcript version {version!r} is still being written"
            raise VersionBusyError(msg)
        await self._transcripts.delete_source(session_id, f"{REPROCESS_SOURCE_PREFIX}{version}")
        await self._transcripts.delete_source(session_id, f"{DIARIZE_SOURCE_PREFIX}{version}")
        await self._reprocess.delete_version(session_id, version)
        log.info("reprocess.version.deleted", session_id=session_id, version=version)

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
        # Attribute every line this task emits, including the router's and the
        # backend's - which know nothing about jobs - to the session and the
        # version being written. That routes them into this version's log file
        # and keeps them off the dashboard, which shows the live capture only
        # (see loreline.logging.bind_log_context).
        bind_log_context(session_id=job.session_id, job_id=job.id)
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
        """Re-run STT over the stored utterances as a new transcript version."""
        if provider is None:
            msg = "transcribe requires a provider"
            raise ProviderNotFoundError(msg)
        backend = self._backend_factory(provider, self._secrets, job.model)
        diarizer = await self._diarizer_factory(job.diarization)
        # Not loaded at all when the job opted out, so no glossary reaches the
        # backend as keyterms or as a prompt.
        glossary = await self._glossaries.get_effective(campaign_id) if job.use_glossary else None
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
        try:
            # Blocking file I/O (a whole session's utterances) off the event loop.
            utterances = await asyncio.to_thread(self._audio_store.read_utterances, job.session_id)
            return await self._drive(router, bus, utterances, job, started_mono)
        finally:
            await _aclose(backend)
            await _aclose(diarizer)

    async def _diarize_session(self, job: ReprocessJob) -> int:
        """Diarize the whole continuous session audio once and relabel ONE
        transcript version (``job.target``) globally, giving stable speaker
        identity across the session. Replaces that version's previous
        diarization; other versions are untouched."""
        session = await self._sessions.get(job.session_id)
        if session is None:
            msg = f"unknown session {job.session_id!r}"
            raise SessionNotFoundError(msg)
        # Blocking file I/O (the whole continuous session WAV) off the event loop.
        wav, sample_rate = await asyncio.to_thread(self._audio_store.read_wav, job.session_id)
        diarizer = await self._diarizer_factory(job.diarization)
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
        base = variant_rows(events, job.target)
        source = f"{DIARIZE_SOURCE_PREFIX}{job.target}"
        relabeled = [
            assign_speakers(event, segments).model_copy(update={"source": source}) for event in base
        ]
        await self._transcripts.delete_source(job.session_id, source)
        live = _LiveSegmentCount(job, self._reprocess)
        for written, event in enumerate(relabeled, start=1):
            await self._transcripts.add(event)
            await live.set(written)
        return len(relabeled)

    async def _drive(
        self,
        router: SttRouter,
        bus: EventBus[TranscriptEvent],
        utterances: list[Utterance],
        job: ReprocessJob,
        started_mono: float,
    ) -> int:
        """Subscribe first, then run the router, persisting every emitted event.

        Subscribing before the router runs guarantees no published event (or the
        close sentinel) is missed. The running count is published to the job row
        as it grows, so the page shows the version filling up rather than a
        motionless "running".

        Each persisted event is also republished on the app-wide bus, tagged
        with this version's source, so a session-filtered ``/ws/transcript``
        subscriber sees the text arrive as it is produced rather than only
        after the job ends. The tag is what keeps it out of the original: a
        subscriber files an event under the version its ``source`` names.
        """
        source = f"{REPROCESS_SOURCE_PREFIX}{job.id}"
        live = _LiveSegmentCount(job, self._reprocess)
        count = 0
        async with bus.subscribe(reliable=True) as stream:
            run_task = asyncio.create_task(_run_then_close(router, utterances, bus))
            try:
                async for event in stream:
                    rebased = rebase_transcript(event, started_mono)
                    tagged = rebased.model_copy(update={"source": source})
                    await self._transcripts.add(tagged)
                    await self._bus.publish(tagged)
                    count += 1
                    await live.set(count)
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
