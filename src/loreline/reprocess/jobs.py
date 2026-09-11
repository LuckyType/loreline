"""Post-session re-processing jobs.

Re-runs a stored session's audio through a (typically higher-quality or local)
STT backend, producing a new transcript *version* tagged
``REPROCESS_SOURCE_PREFIX + job_id`` - every run is kept, so the original and
any number of re-transcriptions stay comparable side by side (see
``loreline.export.variant_view``). A diarize job relabels ONE version
(``job.target``) into a ``DIARIZE_SOURCE_PREFIX + version`` copy, replacing
that version's previous diarization. Jobs run as in-process ``asyncio``
tasks; state is tracked in the ``reprocess_jobs`` table.

A re-transcribing job builds its connector through
:func:`stored_audio_backend`, not through the registry's default: the audio it
replays was recorded hours ago, and a model that serves both transports has a
preference written for the live capture it does not have.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import TYPE_CHECKING

import httpx

from loreline.bus import EventBus
from loreline.diarization.merge import assign_speakers
from loreline.diarization.remote import probe_diarizer
from loreline.export import final_rows, variant_rows
from loreline.health import HealthReport, HealthStatus, classify_request_error
from loreline.logging import bind_log_context, get_logger
from loreline.models import (
    DIARIZE_SOURCE_PREFIX,
    ORIGINAL_VERSION,
    REPROCESS_SOURCE_PREFIX,
    DiarizationMode,
    JobStatus,
    ReprocessJob,
    TranscriptEvent,
    rebase_transcript,
)
from loreline.stt.registry import BackendFactory, create_backend
from loreline.stt.router import RouterConfig, SttRouter

if TYPE_CHECKING:
    from loreline.audio.chunker import Utterance
    from loreline.diarization.base import DiarizationProvider
    from loreline.diarization.provider import BuildDiarizer
    from loreline.models import DiarizationConfig, ProviderConfig, SpeakerSegment
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


class SessionNotFoundError(ValueError):
    """Raised when re-processing a session id that does not exist."""


class AudioMissingError(ValueError):
    """Raised when a session has no stored audio to re-process."""


class ProviderNotFoundError(ValueError):
    """Raised when the chosen re-process provider id does not exist."""


class TargetNotFoundError(ValueError):
    """Raised when a diarize job targets a transcript version with no rows."""


class DiarizerUnreachableError(ValueError):
    """Raised when a diarize job is asked for while nothing answers at its endpoint."""


class DiarizerEndpointMissingError(ValueError):
    """Raised when a remote diarize job names no endpoint and none is stored as the default."""


class OriginalVersionError(ValueError):
    """Raised when asked to delete the original (live capture) transcript version."""


class VersionNotFoundError(ValueError):
    """Raised when deleting a transcript version that does not exist."""


class VersionBusyError(ValueError):
    """Raised when deleting a transcript version a job is still writing."""


class JobNotFoundError(ValueError):
    """Raised when cancelling a re-processing job id that does not exist."""


class JobNotCancellableError(ValueError):
    """Raised when cancelling a job that has already reached a terminal state."""


def stored_audio_backend(
    config: ProviderConfig, secrets: SecretStore, model: str | None
) -> STTBackend:
    """Build a connector for audio that has already been recorded.

    A ``BackendFactory`` like any other, so an injected test double still has
    the same three arguments; what it adds is the one fact a re-processing job
    knows and a live session does not, that nothing is waiting on this audio.

    It matters because a model that serves both transports says in
    capabilities.yaml which one it prefers, and that preference is written for
    a live capture: Deepgram's nova-3 and AssemblyAI's universal-3-5-pro, two
    models a GM is likely to have favourited, both say realtime, correctly, for
    a table that is talking now. A stored session driven the same way opens the
    streaming socket and pushes a whole recording into it as fast as the file
    reads, where a realtime endpoint expects audio at the speed it was spoken.
    A QA pass on this app flagged that delivery as a risk and found at least
    one endpoint, Gemini's, handling it badly.

    Only the preference is overridden. A model whose only transport is the
    streaming one still gets it here (see
    :func:`loreline.capabilities.is_realtime_model`), which is deliberate:
    those connectors are written to take one whole utterance at a time, and
    there is no batch endpoint to send a stored file to instead.
    """
    return create_backend(config, secrets, model, prefer_batch=True)


# What ``enqueue`` asks before it accepts a diarize job, and what a test
# substitutes: anything that grades a diarization endpoint, awaited. The
# default is the probe ``/api/system/healthz`` and ``/api/system/diarizer/probe``
# already call, so all three agree on what "not answering" means.
DiarizerProbe = Callable[[str], Awaitable[HealthReport]]

# Where a remote diarize job that names no endpoint gets one: the stored
# default, read when the job is enqueued rather than once at startup, so a
# default saved on the settings page is honoured by the next press. None when
# nothing is stored, which is the one case the job is refused for.
DefaultEndpoint = Callable[[], Awaitable[str | None]]


async def _no_default_endpoint() -> str | None:
    return None


# How often a running job's segment count is written back to its row. The
# session page polls the job list every 1.5s, so a tighter cadence would only
# add SQLite commits nobody can see.
_COUNT_INTERVAL_S = 1.0


# How long ``cancel`` waits for the run to actually stop before answering. It
# is not a deadline for stopping - it is how long the endpoint is willing to
# hold the request open so it can return the row the job settles *on* rather
# than the one it is leaving. A queued job that never started, and a diarize
# job stopped by ``task.cancel()``, both finish inside this; a re-transcription
# in the middle of a vendor call does not, so that one answers "running" and
# settles under the page's own 1.5s poll a moment later. Long enough that the
# common cases need no second read, short enough that a press never feels slow.
_CANCEL_SETTLE_S = 0.25

# The states a job can still be stopped from. Everything else is terminal and
# earns a 409 naming it: the page polls, so a run can reach the end between the
# poll that drew the Cancel button and the press that hits the endpoint.
_CANCELLABLE = frozenset({JobStatus.QUEUED, JobStatus.RUNNING})


class _CancelRequest:
    """One run's cancel flag, and whether the work really stopped for it.

    Two booleans because they answer different questions, and only the second
    one may decide the row's status.

    ``requested`` is what the GM asked for. It is read by the run itself, at
    the top of every utterance (see :func:`_aiter`), which is what makes
    cancellation cooperative: the run stops between two utterances, where
    nothing is in flight, every row it wrote is committed, and the connector,
    the router and the diarizer all wind down through the same path a run that
    reached the end of the recording takes.

    ``stopped`` is what actually happened, and it is set by the code that broke
    off. A request that arrives after the last utterance was already handed
    over changes nothing about the run, and a job whose work completed in full
    must not carry a badge saying it was cancelled - the version it produced is
    whole, and the GM will read it as a partial one.
    """

    def __init__(self) -> None:
        self.requested = False
        self.stopped = False


class _Run:
    """The live half of an enqueued job: its task, and the flag that asks it to stop.

    Kept only while the task exists (the done-callback in ``enqueue`` drops the
    entry), so "this manager has a ``_Run`` for the id" means "this process is
    the one running it" - which is what lets :meth:`ReprocessManager.cancel`
    tell a job it can actually reach from a row left behind by something else.
    """

    def __init__(self, task: asyncio.Task[None], cancel: _CancelRequest) -> None:
        self.task = task
        self.cancel = cancel


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


class _JobBankDiarizer:
    """Wrap a diarizer so every call it makes is pinned to one job-private bank id.

    A transcribe job's ``RouterConfig.session_id`` has to stay the live
    session's own id - it is also what ``Connector.transcribe`` stamps onto
    every ``TranscriptEvent`` it produces, and changing it would misfile the
    job's rows under a session no reader would find them under (see
    ``SttRouter._merge_diarization``, out of scope here). But the remote
    diarization service keys its speaker bank on that same string over the
    wire, so a second ``RemoteDiarizer`` instance sending it - the job's own,
    built fresh in ``_transcribe_session`` - lands on the SAME remote bank as
    a live capture of that session, and the job's ``aclose`` then deletes it
    out from under the live capture. Substituting a job-private id ahead of
    every call this wrapper forwards keeps the job on its own bank without
    the router or the persisted events ever seeing anything but the real
    session id.
    """

    def __init__(self, inner: DiarizationProvider, *, bank_id: str) -> None:
        self._inner = inner
        self._bank_id = bank_id

    async def diarize(
        self,
        wav: bytes,
        *,
        sample_rate: int = 16000,
        min_speakers: int | None = None,
        max_speakers: int | None = None,
        session_id: str | None = None,
    ) -> list[SpeakerSegment]:
        _ = session_id  # the caller's id is the live session's; substitute ours
        return await self._inner.diarize(
            wav,
            sample_rate=sample_rate,
            min_speakers=min_speakers,
            max_speakers=max_speakers,
            session_id=self._bank_id,
        )

    async def aclose(self) -> None:
        await self._inner.aclose()


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
        diarizer_probe: DiarizerProbe | None = None,
        default_endpoint: DefaultEndpoint | None = None,
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
        self._backend_factory = backend_factory or stored_audio_backend
        self._diarizer_factory = diarizer_factory
        self._diarizer_probe = diarizer_probe or probe_diarizer
        self._default_endpoint = default_endpoint or _no_default_endpoint
        self._runs: dict[str, _Run] = {}

    async def enqueue(self, req: ReprocessRequest) -> ReprocessJob:
        """Validate inputs, create a job row, and spawn the runner task.

        A diarize job is also checked against the service it needs, which no
        other operation is: it is the one job whose whole work is a single call
        to a machine that may not be there at all, and a job queued against one
        that is not there gives a GM nothing to act on except another version
        that ends at "-" some minutes later.
        """
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
        diarization = await self._resolve_endpoint(req.diarization)
        if req.operation == "diarize":
            await self._refuse_an_unreachable_diarizer(diarization)

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
            # The resolved config, not the request's: the row is the record of
            # what ran, and "endpoint: null" on a job that ran at the stored
            # default would send the reader to the settings page to find out.
            diarization=diarization,
            status=JobStatus.QUEUED,
            created_at=time.time(),
        )
        await self._reprocess.create(job)
        campaign_id = session.campaign_id
        # The flag is built here rather than inside the task, and handed to it:
        # `cancel` has to be able to raise it in the window between this line
        # and the loop first scheduling the task, which is exactly the window a
        # job spends QUEUED (see `_run`, which checks it before doing anything).
        cancel = _CancelRequest()
        task = asyncio.create_task(
            self._run(job, provider, campaign_id, session.started_mono, cancel)
        )
        self._runs[job.id] = _Run(task, cancel)
        task.add_done_callback(lambda _t: self._runs.pop(job.id, None))
        log.info("reprocess.enqueue", job_id=job.id, session_id=job.session_id)
        return job

    async def _resolve_endpoint(self, config: DiarizationConfig) -> DiarizationConfig:
        """Fill in a remote diarizer's endpoint from the stored default when
        the request left it blank.

        The diarize dialog promises exactly this: its endpoint field says a
        blank value falls back to the server's configured one, and it sends
        ``null`` to mean so. Nothing downstream honoured that. The factory
        refused the config outright ("remote diarization requires an
        endpoint"), so a GM who had saved a default on the settings page and
        left the field blank, as the copy invited, got a failed job. Resolving
        here rather than in the factory is what makes the probe below check
        the endpoint the job will actually use, and what puts that endpoint on
        the job row, where the version list and the failure message read it.

        A remote config with nothing to resolve to is refused, not queued, for
        either operation. A diarize job's whole work is one call to that
        endpoint. A transcribe job builds its diarizer before the router ever
        runs (see :meth:`_transcribe_session`), so the "survives a diarizer
        that is not there" allowance ``_refuse_an_unreachable_diarizer`` makes
        does not reach a diarizer that cannot be built at all: the job would
        fail on its first line, minutes after the press. A live capture asked
        for the same config is refused at the start button (see
        ``SessionConfigError``), and this is the same answer.
        """
        if config.mode != DiarizationMode.REMOTE or (config.endpoint or "").strip():
            return config
        endpoint = await self._default_endpoint()
        if endpoint:
            return config.model_copy(update={"endpoint": endpoint})
        msg = (
            "remote diarization needs an endpoint and none is configured: type the "
            "diarization service's address, or save one as the default under Settings."
        )
        raise DiarizerEndpointMissingError(msg)

    async def _refuse_an_unreachable_diarizer(self, config: DiarizationConfig) -> None:
        """Say no at the button when nothing answers at the diarizer's endpoint.

        The failure this prevents is a job whose only outcome a GM ever sees is
        another transcript version that ends at "-" some minutes later.
        Refusing costs one probe and answers immediately, with a sentence
        naming the endpoint.

        What that sentence must not do is diagnose, and it used to. It read
        "start it, or fix the endpoint, and press Diarize again", and that was
        wrong in the one case that actually came up. The diarization service
        could not answer anything at all while it was working - the sherpa-onnx
        binding holds the GIL for the length of an inference, so its own health
        endpoint was never scheduled - and a perfectly healthy service part way
        through a 36-minute recording therefore probed ``UNREACHABLE``. This
        guard then refused the next press by telling an operator to start a
        service that was running fine. Two fixes interacting badly, and the one
        that had to change is the service: it now answers while it works (see
        ``services/diarization/app.py``), so a busy diarizer is graded healthy
        here and a second press is bounded by the service's own 429 rather than
        by this. The wording changed too, because it should never have claimed
        to know: what the probe saw is that nothing answered within two
        seconds, and it cannot tell a stopped service from a mistyped address
        from a network that will not carry the request.

        The same probe ``/api/system/healthz`` polls, so the settings page's
        badge and this refusal can never disagree, and only ``UNREACHABLE`` is
        refused: a service answering 503 while its models load, one answering
        429 because it is already diarizing, or one graded degraded for not
        remembering speakers, is a service that is there, and deciding for the
        GM that it is not worth asking is not this function's call to make.

        A *transcribe* job is deliberately not checked, even though it may
        diarize every utterance it produces. Its value is the transcript, its
        diarization is an addition on top that the router already survives
        without (see ``SttRouter._merge_diarization``), and refusing a
        re-transcription because a side feature is down would be the worse
        trade of the two.
        """
        if config.mode != DiarizationMode.REMOTE or not config.endpoint:
            return
        report = await self._diarizer_probe(config.endpoint)
        if report.status is not HealthStatus.UNREACHABLE:
            return
        log.warning(
            "reprocess.diarizer_unreachable", endpoint=config.endpoint, detail=report.detail
        )
        msg = (
            f"the diarization service at {config.endpoint} did not answer its health "
            f"check ({report.detail or 'no answer'}). It may be stopped, at a different "
            "address, or unreachable from here; a service that is only busy still "
            "answers. Check it and press Diarize again."
        )
        raise DiarizerUnreachableError(msg)

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
        # delete, leaving segments no job row explains. A CANCELLED job is not
        # one of them, and that is the whole point of the state: the row only
        # reaches it once the run has actually stopped writing (see `_run`), so
        # deleting a version the GM cut short - which is what they cancelled it
        # in order to do - is exactly as safe as deleting a finished one.
        busy = [
            j
            for j in jobs
            if j.status in _CANCELLABLE
            and (j.id == version or (j.operation == "diarize" and j.target == version))
        ]
        if busy:
            msg = f"transcript version {version!r} is still being written"
            raise VersionBusyError(msg)
        await self._transcripts.delete_source(session_id, f"{REPROCESS_SOURCE_PREFIX}{version}")
        await self._transcripts.delete_source(session_id, f"{DIARIZE_SOURCE_PREFIX}{version}")
        await self._reprocess.delete_version(session_id, version)
        log.info("reprocess.version.deleted", session_id=session_id, version=version)

    async def cancel(self, job_id: str) -> ReprocessJob:
        """Stop a queued or running job, keeping everything it has written.

        The GM is watching the version fill up while it runs, which is the
        whole reason this exists: two minutes in they can already tell whether
        the model is better than the one on screen, and if it is not, there is
        no reason to pay for and wait out the rest. So nothing is rolled back -
        the rows stay, ``segments_added`` keeps its value, and the version
        stays readable and deletable exactly like a finished one. Cancelling
        destroys nothing; it only stops more from being made.

        How the run is stopped depends on what it is doing.

        A **re-transcription** walks the recording utterance by utterance, so
        it is asked to stop rather than interrupted: the flag is raised here
        and read by the iterator feeding the router (:func:`_aiter`), which
        simply stops handing over utterances. The run then finishes through its
        own normal path - the connector and the diarizer close, the last row is
        committed, the row is finalised once - and there is no half-written
        state to unpick. The cost is one utterance of latency, bounded by the
        router's own per-request timeout, because a vendor call already in
        flight is not interrupted.

        A **diarization** is one long call the flag cannot reach into, so that
        one is stopped with ``task.cancel()``. See :meth:`_diarize_session` for
        what it takes to make that safe.

        A **queued** job that the loop has not started yet is stopped before it
        does anything at all: the flag is raised below before this coroutine's
        first await, so a task created by ``enqueue`` and not yet scheduled
        cannot slip past the check at the top of :meth:`_run`. Nothing is read,
        asked for or billed.

        Raises :class:`JobNotFoundError` for an id that does not exist and
        :class:`JobNotCancellableError` for one that has already finished.
        """
        run = self._runs.get(job_id)
        # Before the first await, for the queued case above. Setting it on a
        # job that turns out to be finished below is harmless: the run that
        # would have read it is over, and the entry goes with the task.
        first_ask = run is not None and not run.cancel.requested
        if run is not None:
            run.cancel.requested = True
        job = await self._reprocess.get(job_id)
        if job is None:
            msg = f"unknown job {job_id!r}"
            raise JobNotFoundError(msg)
        if job.status is JobStatus.CANCELLED and first_ask:
            # Our own request, landed while this coroutine was reading the row:
            # the task saw the flag and finalised before the read came back.
            # Answering 409 here would report the caller's own success as a
            # race it lost.
            return job
        if job.status not in _CANCELLABLE:
            msg = f"job {job_id!r} has already finished ({job.status.value})"
            raise JobNotCancellableError(msg)
        if run is None:
            # A queued/running row this process is not running: only reachable
            # before the startup sweep has failed it (see `reconcile`). Nothing
            # is writing it, so recording the GM's decision is safe and is the
            # only thing left to do about it.
            job.status = JobStatus.CANCELLED
            job.finished_at = time.time()
            await self._reprocess.update(job)
            log.info("reprocess.cancelled", job_id=job_id, adopted=True)
            return job
        log.info("reprocess.cancel.requested", job_id=job_id, operation=job.operation)
        if job.operation == "diarize":
            run.task.cancel()
        # `wait`, not `await task`: the task may be ending in CancelledError,
        # and that must stop the job, not the request asking it to stop.
        _ = await asyncio.wait({run.task}, timeout=_CANCEL_SETTLE_S)
        return await self._reprocess.get(job_id) or job

    async def wait(self, job_id: str) -> None:
        """Await completion of a running job's task (no-op if unknown)."""
        run = self._runs.get(job_id)
        if run is not None:
            await run.task

    async def reconcile(self) -> None:
        """Fail jobs left running/queued by a previous process (startup sweep)."""
        await self._reprocess.mark_interrupted()

    async def aclose(self) -> None:
        """Cancel and await any in-flight job tasks (on shutdown).

        Deliberately not a cancellation in the sense :meth:`cancel` means: the
        flag stays down, so `_run` re-raises rather than marking the row
        CANCELLED, and the job is left QUEUED/RUNNING for the next process's
        `reconcile` to fail as interrupted. A restart is not a decision the GM
        made, and a row claiming they made it would be a lie they cannot act on.
        """
        tasks = [run.task for run in self._runs.values()]
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
        cancel: _CancelRequest,
    ) -> None:
        """Run one job, and leave a readable account of it in its own log file.

        Three lines at minimum, whatever the run does: what it set out to do,
        what it wrote and how long it took, and, when it fails, why plus the
        traceback. That is not decoration. Every version's log is offered on
        the session page as "Show logs", and the README promises each run keeps
        one; before these lines a finished re-transcription's file held the
        single ``reprocess.enqueue`` line the caller wrote and nothing about
        the run itself, so the one screen meant for answering "what happened to
        this version" could not answer it for the runs that failed silently.
        A run the GM stopped gets that line too, and it is a distinct one: the
        first question asked of a half-length version is why it is short, and
        "it was stopped on request after N segments" answers it outright.

        The binding on the first line is what puts them there, and it covers
        the whole body including the failure path: it attributes every line
        this task emits - the router's and the backend's too, which know
        nothing about jobs - to this session and this version, which routes
        them into this version's file and keeps them off the dashboard, which
        shows the live capture only (see ``loreline.logging.bind_log_context``).
        """
        bind_log_context(session_id=job.session_id, job_id=job.id)
        if cancel.requested:
            # Cancelled in the window between `enqueue` creating this task and
            # the loop getting round to it: the job never left QUEUED, so it
            # never opened a connector, never read the recording and never
            # billed anything. `started_at` stays None, which is the honest
            # record of a run that did not happen.
            cancel.stopped = True
            job.status = JobStatus.CANCELLED
            job.finished_at = time.time()
            await self._reprocess.update(job)
            log.info("reprocess.cancelled", operation=job.operation, segments_added=0, ran=False)
            return
        job.status = JobStatus.RUNNING
        job.started_at = time.time()
        await self._reprocess.update(job)
        started = time.monotonic()
        log.info("reprocess.start", **_run_description(job))
        finalised = False
        try:
            if job.operation == "diarize":
                job.segments_added = await self._diarize_session(job)
            else:
                job.segments_added = await self._transcribe_session(
                    job, provider, campaign_id, started_mono, cancel
                )
            # `stopped`, not `requested`: a flag raised after the work was
            # already over stops nothing, and the whole version is there.
            job.status = JobStatus.CANCELLED if cancel.stopped else JobStatus.DONE
            log.info(
                "reprocess.cancelled" if cancel.stopped else "reprocess.finished",
                operation=job.operation,
                segments_added=job.segments_added,
                elapsed_s=round(time.monotonic() - started, 1),
            )
        except asyncio.CancelledError:
            # Not caught by the `except Exception` below - CancelledError is a
            # BaseException - and that is exactly why it is handled here: a
            # diarize job stopped by `cancel` arrives as this and would
            # otherwise leave the row saying "running" with no run behind it.
            if not cancel.requested:
                raise  # a shutdown, not a decision: see `aclose`
            cancel.stopped = True
            job.status = JobStatus.CANCELLED
            job.finished_at = time.time()
            # Shielded, and the `finally` below skipped for it. This task is
            # already being torn down, so a plain await here can be interrupted
            # again before SQLite has seen the row - and a row left at
            # "running" by the very call that stopped it is the one outcome
            # this whole feature cannot afford.
            await asyncio.shield(self._reprocess.update(job))
            finalised = True
            log.info(
                "reprocess.cancelled",
                operation=job.operation,
                segments_added=job.segments_added,
                elapsed_s=round(time.monotonic() - started, 1),
                interrupted=True,
            )
            raise
        except Exception as exc:  # any failure marks the job errored
            job.status = JobStatus.ERROR
            job.error = _job_error_message(exc, job)
            # The row carries one sentence for a GM; this carries the same
            # sentence plus the whole traceback, into the file "Show logs" opens.
            log.exception(
                "reprocess.failed",
                job_id=job.id,
                operation=job.operation,
                error=job.error,
                elapsed_s=round(time.monotonic() - started, 1),
            )
        finally:
            if not finalised:
                job.finished_at = time.time()
                await self._reprocess.update(job)

    async def _transcribe_session(
        self,
        job: ReprocessJob,
        provider: ProviderConfig | None,
        campaign_id: str | None,
        started_mono: float,
        cancel: _CancelRequest,
    ) -> int:
        """Re-run STT over the stored utterances as a new transcript version."""
        if provider is None:
            msg = "transcribe requires a provider"
            raise ProviderNotFoundError(msg)
        backend = self._backend_factory(provider, self._secrets, job.model)
        # A job-private bank id, not job.session_id: that id is also the live
        # session's, and the remote diarizer's bank is keyed on it over the
        # wire, so this job's diarizer (its own instance, closed below) would
        # otherwise delete the live capture's speaker bank at aclose (see
        # _JobBankDiarizer). Namespaced with "job" rather than plain
        # f"{job.session_id}:{job.id}" so it cannot collide with a diarize
        # job's f"{job.session_id}:{job.target}" bank either, when a target
        # names this very job's id.
        diarizer = _JobBankDiarizer(
            await self._diarizer_factory(job.diarization),
            bank_id=f"{job.session_id}:job:{job.id}",
        )
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
            return await self._drive(router, bus, utterances, job, started_mono, cancel)
        finally:
            await _aclose(backend)
            await _aclose(diarizer)

    async def _diarize_session(self, job: ReprocessJob) -> int:
        """Diarize the whole continuous session audio once and relabel ONE
        transcript version (``job.target``) globally, giving stable speaker
        identity across the session. Replaces that version's previous
        diarization; other versions are untouched.

        Unlike a re-transcription this is one call, not a loop, so there is no
        boundary at which to ask it to stop: :meth:`cancel` interrupts it with
        ``task.cancel()`` instead, and the write below is what makes that safe
        to do."""
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
                # One call covers the whole session, so nothing here needs a
                # memory of the last one. It is the version being relabeled,
                # not the session: re-running this job against a different
                # version must not inherit the voices of another pass, and
                # ``diarizer.aclose()`` below drops the bank either way.
                session_id=f"{job.session_id}:{job.target}",
            )
        finally:
            await _aclose(diarizer)
        if not segments:
            return 0
        events = await self._transcripts.for_session(job.session_id)
        # Finals only: a diarize job writes a permanent copy of the version it
        # relabels, and an interim or a gap marker from a capture still running
        # has no business being made permanent.
        base = final_rows(variant_rows(events, job.target))
        source = f"{DIARIZE_SOURCE_PREFIX}{job.target}"
        relabeled = [
            assign_speakers(event, segments).model_copy(update={"source": source}) for event in base
        ]
        await self._transcripts.delete_source(job.session_id, source)
        live = _LiveSegmentCount(job, self._reprocess)
        try:
            for written, event in enumerate(relabeled, start=1):
                await self._transcripts.add(event)
                await live.set(written)
        except asyncio.CancelledError:
            # The one place a cancelled run does NOT keep what it wrote, and
            # for the opposite reason: ANY ``diarize:<version>`` rows supersede
            # that version's own on read (see ``loreline.export.variant_view``),
            # so a copy covering the first N rows would not show a partial
            # relabeling - it would hide every row after them, deleting the
            # rest of the transcript from every reader's point of view. A whole
            # copy or none, and the previous diarization is already gone by
            # here, so "none" means the version falls back to its own labels.
            # Shielded because this task is being torn down: an interrupted
            # cleanup is the corrupt state it exists to prevent.
            job.segments_added = 0
            await asyncio.shield(self._transcripts.delete_source(job.session_id, source))
            raise
        return len(relabeled)

    async def _drive(
        self,
        router: SttRouter,
        bus: EventBus[TranscriptEvent],
        utterances: list[Utterance],
        job: ReprocessJob,
        started_mono: float,
        cancel: _CancelRequest,
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

        A cancel stops the *feed* rather than this loop (see :func:`_aiter`),
        which is what keeps the two ends in step: the router finishes the
        utterance it holds, publishes it, closes the bus, and this loop drains
        the last event and returns a count that matches the rows on disk.
        Breaking out here instead would leave `run_task` writing into a bus
        nobody reads, and the `finally` below waiting for it anyway.
        """
        source = f"{REPROCESS_SOURCE_PREFIX}{job.id}"
        live = _LiveSegmentCount(job, self._reprocess)
        count = 0
        async with bus.subscribe(reliable=True) as stream:
            run_task = asyncio.create_task(_run_then_close(router, _aiter(utterances, cancel), bus))
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
    router: SttRouter, utterances: AsyncIterator[Utterance], bus: EventBus[TranscriptEvent]
) -> None:
    try:
        await router.run(utterances)
    finally:
        await bus.aclose()


async def _aiter(items: list[Utterance], cancel: _CancelRequest) -> AsyncIterator[Utterance]:
    """Feed the router the stored recording, one utterance at a time, until it
    runs out or the GM asks it to stop.

    This is where a cancelled re-transcription actually stops, and the boundary
    is chosen rather than forced. Between two utterances there is no request in
    flight, every row the run produced is already committed, and the router,
    the connector and the diarizer all wind down through the same path they
    take at the end of a recording - so the job finalises itself and there is
    nothing half-written for anyone to unpick. ``task.cancel()`` would stop it
    sooner, at whichever await happened to be current, and would buy those few
    seconds with all of that.

    The cost is one utterance of latency: a vendor call already in flight is
    not interrupted, so the press lands when it comes back, bounded by the
    router's own per-request timeout. ``stopped`` is set here rather than
    inferred from ``requested`` later because this is the only place that knows
    the difference between a run that was cut short and one that had already
    handed over its last utterance when the press arrived.
    """
    for item in items:
        if cancel.requested:
            cancel.stopped = True
            return
        yield item


async def _aclose(obj: object) -> None:
    closer = getattr(obj, "aclose", None)
    if closer is not None:
        try:
            await closer()
        except Exception:  # cleanup must never mask the job result
            log.warning("reprocess.aclose.failed")


def _run_description(job: ReprocessJob) -> dict[str, str]:
    """What a run's opening log line says it is about to do.

    Enough to read the rest of the file without the job row open next to it:
    which operation, which version it writes, and which machine does the work -
    the provider and model for a re-transcription, the diarizer and its
    endpoint for a relabeling. A diarize job names its target rather than a
    version of its own, because it writes none: it rewrites one version's rows
    into a copy that supersedes them (see :meth:`_diarize_session`).
    """
    if job.operation == "diarize":
        return {
            "operation": job.operation,
            "target": job.target,
            "diarizer": job.diarization.mode.value,
            "endpoint": job.diarization.endpoint or "",
        }
    return {
        "operation": job.operation,
        "version": job.id,
        "provider_id": job.provider_id,
        "model": job.model or "",
        "diarizer": job.diarization.mode.value,
    }


def _job_error_message(exc: Exception, job: ReprocessJob) -> str:
    """The message stored on a failed job's row, in words a GM can act on.

    Most exceptions here already read fine as raised: a missing session or
    provider is a ``ValueError`` with its own sentence, and an STT router that
    ran out of providers already carries every retired one's own words (see
    ``SttRouter.terminal_error``). The one case that does not is a diarizer
    that answered with an HTTP error status - both diarizers raise that as an
    ``httpx.HTTPStatusError`` - which is still a technical string built for a
    console, not a sentence written for a GM.

    ``classify_request_error`` is the same grading ``loreline.stt.router``
    already applies to a failed STT request: an ``UNREACHABLE`` verdict means
    nothing answered at all (refused, timed out, no such host), which already
    reads fine and is passed through unchanged; anything else means something
    did answer, just badly, and gets the plain-language translation instead.
    The vendor's own words are not lost - they are still in the traceback
    ``log.exception`` writes right after this is called, which lands in the
    version's own log file, exactly what "Show logs" reads. That consolation
    holds only for a run that wrote such a file, which is why :meth:`_run` now
    logs its own start, finish and failure: a job whose only stored line came
    from the caller's ``reprocess.enqueue`` left "Show logs" answering "no logs
    stored for this version", and the words were then lost after all.

    One exception carries no words at all, and it is the one this path was
    written for. ``str(httpx.ReadTimeout())`` is the empty string, so a
    diarization that ran out of time stored ``error=""`` - which the session
    page reads as a job with nothing to say and skips, leaving a failed run
    indistinguishable from a button that was never pressed. A blank message is
    therefore replaced with :func:`_wordless_failure`, never stored as it is.
    """
    failure = classify_request_error(exc)
    if failure.status is HealthStatus.UNREACHABLE:
        return str(exc).strip() or _wordless_failure(exc, job)
    return (
        "The diarization service answered but could not process the audio "
        "(is it configured correctly?)"
    )


def _wordless_failure(exc: Exception, job: ReprocessJob) -> str:
    """A sentence for an exception whose own words are the empty string.

    Built from the type and from what this job was doing, because those are the
    only two facts there are. The type name is kept inside the sentence rather
    than translated away: "ReadTimeout" is what distinguishes a service that
    accepted the audio and then went quiet from one that refused the
    connection, and it is what a maintainer reading a bug report needs, while
    the sentence around it is what a GM needs.
    """
    subject = _failing_service(job)
    if isinstance(exc, httpx.TimeoutException):
        return (
            f"{subject} did not answer in time ({type(exc).__name__}). The wait already "
            "grows with the amount of audio sent, so check that the service is running "
            "and keeping up with it."
        )
    return f"{subject} failed with {type(exc).__name__} and gave no reason."


def _failing_service(job: ReprocessJob) -> str:
    """What to call the thing that did not answer, in this job's own terms."""
    if job.operation != "diarize":
        return "The provider this run re-transcribes with"
    if job.diarization.endpoint:
        return f"The diarization service at {job.diarization.endpoint}"
    return "The diarization service"
