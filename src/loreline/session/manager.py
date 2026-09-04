"""Session orchestration: capture -> router -> persistence -> transcript bus.

``SessionManager`` owns the single active capture session. It wires a frame
source + speech detector through the ``VadChunker`` into the ``SttRouter`` and
persists every emitted ``TranscriptEvent``. Hardware-facing factories (audio
source/detector, STT backends, diarizer) are injectable so the manager can run
fully offline in tests.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol, cast, runtime_checkable

from loreline.audio.chunker import SpeechDetector, Utterance, VadChunker
from loreline.bus import EventBus
from loreline.capabilities import supports_inline_diarization, supports_live_capture
from loreline.logging import bind_log_context, get_logger, log_context
from loreline.models import (
    DiarizationMode,
    Session,
    SessionStatus,
    TranscriptEvent,
    rebase_transcript,
)
from loreline.monitoring.alerts import AlertLevel
from loreline.stt.registry import BackendFactory, create_backend
from loreline.stt.router import ProvidersExhaustedError, RouterConfig, SttRouter

if TYPE_CHECKING:
    from loreline.diarization.base import DiarizationProvider
    from loreline.diarization.provider import BuildDiarizer
    from loreline.models import DiarizationConfig, ProviderConfig
    from loreline.monitoring.alerts import AlertManager
    from loreline.persistence import (
        AudioStore,
        GlossaryRepository,
        ProviderRepository,
        SessionAudioWriter,
        SessionRepository,
        TranscriptRepository,
    )
    from loreline.secrets import SecretStore
    from loreline.stt.base import STTBackend
    from loreline.web.schemas import StartSessionRequest

log = get_logger(__name__)


class CaptureSource(Protocol):
    """A stoppable source of timestamped PCM frames."""

    def frames(self) -> AsyncIterator[tuple[bytes, float]]:
        """Yield ``(pcm_bytes, monotonic_ts)`` until stopped."""
        ...

    def stop(self) -> None:
        """Signal the source to stop producing frames."""
        ...


@runtime_checkable
class PreflightCapture(Protocol):
    """A capture source that can prove its device opens before a session starts.

    Optional half of :class:`CaptureSource`: only the hardware-backed source has
    a device that can refuse, and the injectable fakes the manager runs on in
    tests have nothing to pre-flight. ``start()`` asks for it by structural
    check rather than making every fake implement a no-op.
    """

    async def preflight(self) -> None:
        """Open and immediately release the device; raise if it cannot serve."""
        ...


CaptureFactory = Callable[["StartSessionRequest", int], tuple[CaptureSource, SpeechDetector]]


class SessionActiveError(RuntimeError):
    """Raised when starting a session while one is already running."""


class ProviderNotFoundError(ValueError):
    """Raised when a referenced provider id does not exist."""


class ProviderDisabledError(ValueError):
    """Raised when a referenced provider exists but is disabled."""


class SessionConfigError(ValueError):
    """Raised when the start request's config can't be honored.

    Covers a provider kind with no registered STT backend (e.g. one still
    listed in the catalog/wizard but not yet implemented) and an invalid
    diarization config (e.g. ``remote`` mode without an endpoint) - both
    otherwise surface as a bare ``ValueError`` straight out of factory calls,
    which FastAPI turns into an unhandled 500.
    """


def _default_capture(
    req: StartSessionRequest, sample_rate: int
) -> tuple[CaptureSource, SpeechDetector]:
    from loreline.audio.capture import SoundDeviceSource  # noqa: PLC0415
    from loreline.audio.vad import SileroVad  # noqa: PLC0415

    source = SoundDeviceSource(device=req.device, sample_rate=sample_rate)
    detector = SileroVad(sample_rate=sample_rate)
    return source, detector.is_speech


@dataclass(slots=True)
class _CaptureStats:
    """How much audio the active session has actually received.

    A microphone that opens and then delivers nothing is indistinguishable from
    a healthy one everywhere else in the API - both report ``capturing`` - which
    is how a dead mic can look like a running session for a whole evening. These
    two numbers are what makes the difference visible, on the dashboard and in
    the logs, while the session is still salvageable.

    ``last_frame_mono`` starts at the moment capture was set up, so a source
    that never yields a first frame ages from the start rather than reading as
    fresh forever.
    """

    sample_rate: int = 16000
    samples: int = 0
    last_frame_mono: float = field(default_factory=time.monotonic)

    def record(self, frame: bytes) -> None:
        self.samples += len(frame) // 2  # mono s16le
        self.last_frame_mono = time.monotonic()

    @property
    def seconds(self) -> float:
        """Seconds of audio captured so far."""
        return self.samples / self.sample_rate if self.sample_rate > 0 else 0.0

    @property
    def since_last_frame(self) -> float:
        """Seconds since the last frame arrived (frames arrive during silence too)."""
        return max(0.0, time.monotonic() - self.last_frame_mono)


@dataclass(slots=True)
class _Runtime:
    session: Session
    source: CaptureSource
    session_bus: EventBus[TranscriptEvent]
    router: SttRouter
    router_task: asyncio.Task[None]
    persist_task: asyncio.Task[None]
    backends: list[STTBackend]
    diarizer: DiarizationProvider
    audio_writer: SessionAudioWriter | None
    stats: _CaptureStats


class SessionManager:
    """Own the lifecycle of the single active capture session."""

    def __init__(
        self,
        *,
        providers: ProviderRepository,
        glossaries: GlossaryRepository,
        sessions: SessionRepository,
        transcripts: TranscriptRepository,
        secrets: SecretStore,
        transcript_bus: EventBus[TranscriptEvent],
        diarizer_factory: BuildDiarizer,
        audio_store: AudioStore | None = None,
        alerter: AlertManager | None = None,
        capture_factory: CaptureFactory | None = None,
        backend_factory: BackendFactory | None = None,
    ) -> None:
        self._providers = providers
        self._glossaries = glossaries
        self._sessions = sessions
        self._transcripts = transcripts
        self._secrets = secrets
        self._bus = transcript_bus
        self._audio_store = audio_store
        self._alerter = alerter
        self._capture_factory = capture_factory or _default_capture
        self._backend_factory = backend_factory or create_backend
        self._diarizer_factory = diarizer_factory
        self._runtime: _Runtime | None = None
        self._lock = asyncio.Lock()
        # Holds the task that finalizes a session whose capture died on its own
        # (see _router_finished); asyncio only keeps a weak reference to a task,
        # so dropping this one could have the teardown collected mid-flight.
        self._unattended_end: asyncio.Task[None] | None = None

    @property
    def transcript_bus(self) -> EventBus[TranscriptEvent]:
        return self._bus

    def status(self) -> SessionStatus:
        return SessionStatus.CAPTURING if self._runtime is not None else SessionStatus.IDLE

    def current_session_id(self) -> str | None:
        return self._runtime.session.id if self._runtime is not None else None

    def stt_degraded_since(self) -> float | None:
        """Epoch time the active session's transcription started failing, or None."""
        runtime = self._runtime
        return runtime.router.degraded_since if runtime is not None else None

    def stt_error(self) -> str | None:
        """Why the active session stopped transcribing for good, or None.

        Carries the vendor's own sentence ("OpenAI: You have no credits
        remaining."), because "transcription stopped" on its own tells a GM
        nothing they can act on. Null while any provider still works, and while
        idle.
        """
        runtime = self._runtime
        return runtime.router.terminal_error if runtime is not None else None

    def captured_seconds(self) -> float | None:
        """Seconds of audio the active session has captured, or None while idle."""
        runtime = self._runtime
        return runtime.stats.seconds if runtime is not None else None

    def capture_last_frame_age(self) -> float | None:
        """Seconds since the mic last delivered a frame, or None while idle.

        The dashboard's proof that a running session is actually recording:
        a device delivers frames whether or not anyone is speaking, so an age
        that keeps climbing means the audio stopped, not the table.
        """
        runtime = self._runtime
        return runtime.stats.since_last_frame if runtime is not None else None

    def _check_live_capable(self, config: ProviderConfig, role: str) -> None:
        """Reject a provider that can only transcribe stored audio.

        OpenRouter's transcription API has no streaming mode at all (see
        loreline.capabilities.supports_live_capture). Caught here rather than
        left to fail mid-session: the UI already hides these from the live
        pickers, so reaching this is an API caller or a stale default.
        """
        if not supports_live_capture(config.kind):
            msg = (
                f"{role} provider {config.name!r} ({config.kind.value}) cannot drive a live "
                "capture - it is available for post-session re-processing only"
            )
            raise SessionConfigError(msg)

    def _check_inline_diarization(self, config: ProviderConfig, req: StartSessionRequest) -> None:
        """Reject "Inline (from STT)" for a model that returns no speakers.

        Silently producing an unlabelled transcript is the bad outcome here: the
        GM only finds out after the session, when there is nothing to re-run the
        live audio against. The UI hides the option, so reaching this means an
        API caller or a stored default that predates a model change.
        """
        if req.diarization.mode is not DiarizationMode.INLINE:
            return
        if not supports_inline_diarization(config.kind, req.model):
            msg = (
                f"model {req.model!r} on {config.name!r} returns no speaker labels - "
                "inline diarization would produce an unlabelled transcript"
            )
            raise SessionConfigError(msg)

    async def _resolve_providers(
        self, req: StartSessionRequest
    ) -> tuple[ProviderConfig, ProviderConfig | None]:
        """Look up + validate the primary/fallback providers for a start request."""
        primary_cfg = await self._providers.get(req.primary_provider)
        if primary_cfg is None:
            msg = f"unknown primary provider {req.primary_provider!r}"
            raise ProviderNotFoundError(msg)
        if not primary_cfg.enabled:
            msg = f"primary provider {req.primary_provider!r} is disabled"
            raise ProviderDisabledError(msg)
        self._check_live_capable(primary_cfg, "primary")
        self._check_inline_diarization(primary_cfg, req)

        fallback_cfg: ProviderConfig | None = None
        if req.fallback_provider:
            fallback_cfg = await self._providers.get(req.fallback_provider)
            if fallback_cfg is None:
                msg = f"unknown fallback provider {req.fallback_provider!r}"
                raise ProviderNotFoundError(msg)
            if not fallback_cfg.enabled:
                msg = f"fallback provider {req.fallback_provider!r} is disabled"
                raise ProviderDisabledError(msg)
            self._check_live_capable(fallback_cfg, "fallback")
        return primary_cfg, fallback_cfg

    def _build_backends(
        self,
        req: StartSessionRequest,
        primary_cfg: ProviderConfig,
        fallback_cfg: ProviderConfig | None,
    ) -> tuple[STTBackend, STTBackend | None, list[STTBackend]]:
        """Instantiate the primary (+ optional fallback) STT backend(s).

        Each gets the model the request chose for *that* provider: the two are
        different vendors with disjoint model lists, so the primary's pick
        means nothing to the fallback.

        Wraps the factory's ``ValueError`` (a provider kind with no registered
        backend) as ``SessionConfigError`` so the route can answer 400 instead
        of leaking an unhandled 500.
        """
        try:
            primary = self._backend_factory(primary_cfg, self._secrets, req.model)
            backends: list[STTBackend] = [primary]
            fallback: STTBackend | None = None
            if fallback_cfg is not None:
                fallback = self._backend_factory(fallback_cfg, self._secrets, req.fallback_model)
                backends.append(fallback)
        except ValueError as exc:
            raise SessionConfigError(str(exc)) from exc
        return primary, fallback, backends

    async def _build_diarizer(self, config: DiarizationConfig) -> DiarizationProvider:
        """Instantiate the diarizer, translating an invalid config to a 400."""
        try:
            return await self._diarizer_factory(config)
        except ValueError as exc:
            raise SessionConfigError(str(exc)) from exc

    async def start(self, req: StartSessionRequest) -> Session:
        """Begin a capture session; raise if one is already active."""
        async with self._lock:
            if self._runtime is not None:
                raise SessionActiveError

            primary_cfg, fallback_cfg = await self._resolve_providers(req)
            sample_rate = primary_cfg.sample_rate

            # Built and pre-flighted before anything else is: the microphone is
            # the one part of a session that cannot be retried later, and a
            # start that fails here costs nothing but an error message.
            source, detector = self._capture_factory(req, sample_rate)
            await self._preflight_capture(source, req.device)

            primary, fallback, backends = self._build_backends(req, primary_cfg, fallback_cfg)
            # Skipped entirely when the GM opted out, so no glossary reaches the
            # backend as keyterms or as a prompt.
            glossary = (
                await self._glossaries.get_effective(req.campaign_id) if req.use_glossary else None
            )
            diarizer = await self._build_diarizer(req.diarization)
            chunker = VadChunker(sample_rate=sample_rate)

            session = Session(
                id=uuid.uuid4().hex,
                status=SessionStatus.CAPTURING,
                started_at=time.time(),
                started_mono=time.monotonic(),
                campaign_id=req.campaign_id,
                primary_provider=req.primary_provider,
                fallback_provider=req.fallback_provider,
                diarization=req.diarization,
            )

            audio_writer: SessionAudioWriter | None = None
            if self._audio_store is not None:
                audio_writer = self._audio_store.writer(session.id, sample_rate=sample_rate)
                session.audio_path = str(self._audio_store.wav_path(session.id))

            await self._sessions.create(session)

            session_bus: EventBus[TranscriptEvent] = EventBus()
            router = SttRouter(
                primary,
                session_bus,
                RouterConfig(
                    session_id=session.id,
                    glossary=glossary,
                    diarization=req.diarization,
                ),
                fallback=fallback,
                diarizer=diarizer,
                on_failover=self._failover_alert,
            )
            persist_task = asyncio.create_task(
                self._persist(session_bus, session.started_mono, session.id)
            )
            stats = _CaptureStats(sample_rate=sample_rate)
            router_task = asyncio.create_task(
                self._run_router(router, source, detector, chunker, audio_writer, stats, session.id)
            )
            self._runtime = _Runtime(
                session=session,
                source=source,
                session_bus=session_bus,
                router=router,
                router_task=router_task,
                persist_task=persist_task,
                backends=backends,
                diarizer=diarizer,
                audio_writer=audio_writer,
                stats=stats,
            )
            # Nothing else awaits this task between here and stop(), so without
            # a callback a capture that dies mid-session would keep reporting
            # "capturing" until someone pressed Stop and waited out the drain.
            router_task.add_done_callback(self._router_finished)
            log.info("session.start", session_id=session.id, primary=req.primary_provider)
            return session

    async def _preflight_capture(self, source: CaptureSource, device: int | str | None) -> None:
        """Fail the start request now if the chosen microphone cannot be opened.

        The alternative is the failure the QA report found: a session that
        reports ``capturing`` with a ticking timer while the stored WAV never
        leaves its 44-byte header, because the device only refused once the
        capture task was already running behind the response. Fakes have no
        device, so they are simply not pre-flighted (see PreflightCapture).
        """
        if not isinstance(source, PreflightCapture):
            return
        try:
            await source.preflight()
        except Exception as exc:
            log.warning("session.start.device_unavailable", device=device, error=str(exc))
            named = "the default input device" if device is None else f"input device {device!r}"
            msg = (
                f"{named} could not be opened for recording ({exc}). Pick another "
                "microphone in Settings, or check that nothing else is using it."
            )
            raise SessionConfigError(msg) from exc

    async def stop(self) -> Session | None:
        """Stop the active session and finalize persistence."""
        async with self._lock:
            runtime = self._runtime
            if runtime is None:
                return None
            self._runtime = None

        with log_context(session_id=runtime.session.id):
            return await self._finish(runtime)

    def _router_finished(self, task: asyncio.Task[None]) -> None:
        """Notice a capture that ended itself, and end the session with it.

        Runs as ``router_task``'s done callback. A router that raised has taken
        the capture down with it (the frame source is gone, the recording is
        over), and nobody is waiting on that task: without this the manager
        would keep answering ``capturing`` until the GM pressed Stop, and only
        then find out - after the 30-second drain timeout - that the session had
        been dead for hours.

        A task that was *cancelled* is stop() doing its job, and one that ended
        cleanly is a frame source that ran out (the finite fakes tests inject);
        neither is a death to report.
        """
        if task.cancelled() or task.exception() is None:
            return
        runtime = self._runtime
        if runtime is None or runtime.router_task is not task:
            return  # stop() already owns the teardown
        self._unattended_end = asyncio.create_task(self._end_unattended(task))

    async def _end_unattended(self, task: asyncio.Task[None]) -> None:
        """Finalize a session nobody asked to stop (see :meth:`_router_finished`).

        Deliberately the same teardown ``stop()`` runs: ``_finish`` re-awaits the
        router task, so the exception that killed it decides the stored status
        and fires the "Session error" alert, exactly as it would have done had
        the GM pressed Stop.
        """
        async with self._lock:
            runtime = self._runtime
            if runtime is None or runtime.router_task is not task:
                return  # stop() got there first
            self._runtime = None

        with log_context(session_id=runtime.session.id):
            log.error("session.capture.died", session_id=runtime.session.id)
            try:
                await self._finish(runtime)
            except Exception:  # pragma: no cover - defensive: this task has no caller
                log.exception("session.finish.failed", session_id=runtime.session.id)

    async def _finish(self, runtime: _Runtime) -> Session:
        """Drain, close and finalize a stopped session's runtime.

        Split out of :meth:`stop` only so the whole teardown runs inside one
        ``log_context``: the lines it produces (a drain timeout, a router
        crash, the capture source closing) belong to the session that is ending
        even though none of the code emitting them is holding its id.
        """
        runtime.source.stop()
        status = SessionStatus.COMPLETED
        try:
            # Bounded drain: with STT healthy the router finishes its queue in
            # moments, but an unreachable backend leaves a deep utterance
            # backlog where every entry burns a full per-utterance timeout -
            # holding this stop request (and the shutdown path) hostage for up
            # to half an hour. The audio + index are already on disk, so cut
            # the drain short instead: wait_for cancels the router task, and
            # the skipped tail stays re-transcribable from stored audio.
            await asyncio.wait_for(runtime.router_task, timeout=_STOP_DRAIN_TIMEOUT_S)
        except TimeoutError:
            log.warning("session.stop.drain_timeout", session_id=runtime.session.id)
        except Exception:
            log.exception("session.router.failed", session_id=runtime.session.id)
            status = SessionStatus.ERROR

        await runtime.session_bus.aclose()
        await runtime.persist_task

        if runtime.audio_writer is not None:
            # Finalizing can mean flushing a long WAV header + a large index
            # sidecar; this runs inside the stop-session request handler, so
            # keep it off the event loop rather than stalling the response
            # (and every other concurrent request) on disk I/O.
            await asyncio.to_thread(runtime.audio_writer.close)

        for backend in runtime.backends:
            with contextlib.suppress(Exception):
                await backend.aclose()
        with contextlib.suppress(Exception):
            await runtime.diarizer.aclose()

        await self._sessions.finish(runtime.session.id, status)
        runtime.session.status = status
        log.info("session.stop", session_id=runtime.session.id, status=status.value)
        if status is SessionStatus.ERROR:
            await self._notify(
                "Session error",
                f"Session {runtime.session.id} ended with an error.",
                level=AlertLevel.ERROR,
            )
        return runtime.session

    async def _failover_alert(self, message: str) -> None:
        await self._notify("Transcription degraded", message, level=AlertLevel.WARNING)

    async def _notify(self, title: str, message: str, *, level: AlertLevel) -> None:
        """Best-effort push alert; never raises into the caller."""
        if self._alerter is None:
            return
        with contextlib.suppress(Exception):
            await self._alerter.send(title, message, level=level)

    async def _run_router(
        self,
        router: SttRouter,
        source: CaptureSource,
        detector: SpeechDetector,
        chunker: VadChunker,
        audio_writer: SessionAudioWriter | None,
        stats: _CaptureStats,
        session_id: str,
    ) -> None:
        # Everything below (the router, the STT backends, the VAD, the capture
        # task spawned here) logs without ever being told which session it is
        # serving. Bind it once for this task so those lines can be attributed
        # to the capture: the dashboard shows them, and they are what the
        # session's stored "original" log is made of.
        bind_log_context(session_id=session_id)
        # Decouple capture from STT: a dedicated task drains the mic + VAD into a
        # bounded queue that the router consumes independently, so a slow STT
        # round-trip (e.g. a long sentence) cannot stall frame capture and
        # overflow the device buffer. Under sustained overload the oldest queued
        # utterance is dropped (logged) instead of corrupting live frames.
        queue: asyncio.Queue[object] = asyncio.Queue(maxsize=_UTTERANCE_QUEUE_MAX)
        capture_task = asyncio.create_task(
            _capture_utterances(source, detector, chunker, audio_writer, stats, queue)
        )
        utterances = _dequeue(queue)
        try:
            await router.run(utterances)
        except ProvidersExhaustedError as exc:
            # Every STT provider has failed in a way that will not change (no
            # credits, rejected key, missing model). The capture deliberately
            # keeps running: the audio is the one artifact that cannot be
            # produced again, and a stopped microphone loses the rest of the
            # evening outright, while a transcript can be re-made from the
            # stored WAV the moment the provider works again. So the session
            # degrades to a recorder, and the vendor's reason is pushed at the
            # GM instead - as an alert, and on the dashboard via
            # ``stt_error``. Draining the queue is part of keeping the
            # recording clean: without a consumer it fills, and every further
            # utterance logs a drop it can do nothing about.
            log.error("session.stt.exhausted", session_id=session_id, error=str(exc))
            await self._notify(
                "Transcription stopped",
                f"Live transcription stopped ({exc}). Audio keeps recording; "
                "the session can be re-transcribed later.",
                level=AlertLevel.ERROR,
            )
            async for _utterance in utterances:
                pass
        finally:
            await capture_task

    async def _persist(
        self, session_bus: EventBus[TranscriptEvent], started_mono: float, session_id: str
    ) -> None:
        bind_log_context(session_id=session_id)  # see _run_router
        async with session_bus.subscribe(reliable=True) as stream:
            async for event in stream:
                rebased = rebase_transcript(event, started_mono)
                await self._transcripts.add(rebased)
                await self._bus.publish(rebased)


_UTTERANCE_QUEUE_MAX = 64
_CAPTURE_DONE = object()
# How long stop() lets the router drain queued utterances before cancelling it.
# One per-utterance STT timeout: a healthy drain finishes well inside this; a
# drain that can't is a dead backend working through a backlog.
_STOP_DRAIN_TIMEOUT_S = 30.0


def _offer(queue: asyncio.Queue[object], item: object) -> None:
    """Enqueue without blocking; drop the oldest item if the queue is full."""
    if queue.full():
        with contextlib.suppress(asyncio.QueueEmpty):
            queue.get_nowait()
            log.warning("capture.utterance.dropped")  # STT not keeping up
    queue.put_nowait(item)


async def _capture_utterances(
    source: CaptureSource,
    detector: SpeechDetector,
    chunker: VadChunker,
    audio_writer: SessionAudioWriter | None,
    stats: _CaptureStats,
    queue: asyncio.Queue[object],
) -> None:
    """Drain frames through VAD + chunking into ``queue`` (never blocks).

    Runs as its own task so capture keeps emptying the device buffer regardless
    of STT latency. Every frame is written to the continuous session recording
    and each completed utterance's span is indexed, so stored audio stays
    complete (and re-VAD-able) even if the live queue drops an utterance.

    However this ends - stopped, cancelled, or with the frame source raising
    before it ever yields - the closing sentinel is delivered. It is the only
    thing that ends ``_dequeue``, so skipping it leaves the router blocked on an
    empty queue for as long as the process lives: the session then reports
    ``capturing`` with a dead microphone, and even Stop only unblocks it by
    timing out. That is the zombie recording this ``finally`` exists to prevent.
    """
    try:
        async for frame, ts in source.frames():
            stats.record(frame)
            if audio_writer is not None:
                # Disk write off the event loop: this task is already decoupled
                # from STT latency, but a blocking write here would still stall
                # every other coroutine (health polls, WS streams, HTTP requests)
                # for its duration.
                await asyncio.to_thread(audio_writer.append_frame, frame)  # incl. silence
            # ONNX inference off the event loop for the same reason.
            is_speech = await asyncio.to_thread(detector, frame)
            utterance = chunker.feed(frame, ts=ts, is_speech=is_speech)
            if utterance is not None:
                if audio_writer is not None:
                    # mark_utterance persists the index sidecar - disk I/O, so off
                    # the event loop like the frame writes above.
                    await asyncio.to_thread(audio_writer.mark_utterance, utterance)
                _offer(queue, utterance)
    except Exception:
        # Logged here, where the session context is still bound, rather than
        # left for whoever eventually awaits this task.
        log.exception("capture.failed", captured_seconds=round(stats.seconds, 1))
        raise
    finally:
        try:
            final = chunker.flush()
            if final is not None:
                if audio_writer is not None:
                    await asyncio.to_thread(audio_writer.mark_utterance, final)
                _offer(queue, final)
        finally:
            _offer(queue, _CAPTURE_DONE)


async def _dequeue(queue: asyncio.Queue[object]) -> AsyncIterator[Utterance]:
    """Yield queued utterances until the capture-done sentinel arrives."""
    while True:
        item = await queue.get()
        if item is _CAPTURE_DONE:
            return
        yield cast("Utterance", item)
