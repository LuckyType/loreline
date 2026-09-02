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
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, cast

from loreline.audio.chunker import SpeechDetector, Utterance, VadChunker
from loreline.bus import EventBus
from loreline.capabilities import supports_inline_diarization, supports_live_capture
from loreline.diarization.provider import create_diarizer
from loreline.logging import get_logger
from loreline.models import (
    DiarizationMode,
    Session,
    SessionStatus,
    TranscriptEvent,
    rebase_transcript,
)
from loreline.monitoring.alerts import AlertLevel
from loreline.stt.registry import create_backend
from loreline.stt.router import RouterConfig, SttRouter

if TYPE_CHECKING:
    from loreline.diarization.base import DiarizationProvider
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


CaptureFactory = Callable[["StartSessionRequest", int], tuple[CaptureSource, SpeechDetector]]
BackendFactory = Callable[["ProviderConfig", "SecretStore"], "STTBackend"]
DiarizerFactory = Callable[["DiarizationConfig"], "DiarizationProvider"]


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
        audio_store: AudioStore | None = None,
        alerter: AlertManager | None = None,
        capture_factory: CaptureFactory | None = None,
        backend_factory: BackendFactory | None = None,
        diarizer_factory: DiarizerFactory | None = None,
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
        self._diarizer_factory = diarizer_factory or create_diarizer
        self._runtime: _Runtime | None = None
        self._lock = asyncio.Lock()

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

    def _check_live_capable(self, config: ProviderConfig, role: str) -> None:
        """Reject a provider that can only transcribe stored audio.

        OpenRouter's transcription API has no streaming mode at all (see
        loreline.capabilities.LIVE_CAPTURE_EXCLUDED). Caught here rather than
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
        # The request's model override wins, exactly as it does for the backend.
        model = req.model or config.model
        if not supports_inline_diarization(config.kind, model):
            msg = (
                f"model {model or '(none)'!r} on {config.name!r} returns no speaker labels - "
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
        if req.model:
            # Model is chosen on demand at start time, overriding the stored default.
            primary_cfg = primary_cfg.model_copy(update={"model": req.model})

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
            if req.fallback_model:
                fallback_cfg = fallback_cfg.model_copy(update={"model": req.fallback_model})
        return primary_cfg, fallback_cfg

    def _build_backends(
        self, primary_cfg: ProviderConfig, fallback_cfg: ProviderConfig | None
    ) -> tuple[STTBackend, STTBackend | None, list[STTBackend]]:
        """Instantiate the primary (+ optional fallback) STT backend(s).

        Wraps the factory's ``ValueError`` (a provider kind with no registered
        backend) as ``SessionConfigError`` so the route can answer 400 instead
        of leaking an unhandled 500.
        """
        try:
            primary = self._backend_factory(primary_cfg, self._secrets)
            backends: list[STTBackend] = [primary]
            fallback: STTBackend | None = None
            if fallback_cfg is not None:
                fallback = self._backend_factory(fallback_cfg, self._secrets)
                backends.append(fallback)
        except ValueError as exc:
            raise SessionConfigError(str(exc)) from exc
        return primary, fallback, backends

    def _build_diarizer(self, config: DiarizationConfig) -> DiarizationProvider:
        """Instantiate the diarizer, translating an invalid config to a 400."""
        try:
            return self._diarizer_factory(config)
        except ValueError as exc:
            raise SessionConfigError(str(exc)) from exc

    async def start(self, req: StartSessionRequest) -> Session:
        """Begin a capture session; raise if one is already active."""
        async with self._lock:
            if self._runtime is not None:
                raise SessionActiveError

            primary_cfg, fallback_cfg = await self._resolve_providers(req)
            primary, fallback, backends = self._build_backends(primary_cfg, fallback_cfg)
            # Skipped entirely when the GM opted out, so no glossary reaches the
            # backend as keyterms or as a prompt.
            glossary = (
                await self._glossaries.get_effective(req.campaign_id) if req.use_glossary else None
            )
            diarizer = self._build_diarizer(req.diarization)
            sample_rate = primary_cfg.sample_rate

            source, detector = self._capture_factory(req, sample_rate)
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
            persist_task = asyncio.create_task(self._persist(session_bus, session.started_mono))
            router_task = asyncio.create_task(
                self._run_router(router, source, detector, chunker, audio_writer)
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
            )
            log.info("session.start", session_id=session.id, primary=req.primary_provider)
            return session

    async def stop(self) -> Session | None:
        """Stop the active session and finalize persistence."""
        async with self._lock:
            runtime = self._runtime
            if runtime is None:
                return None
            self._runtime = None

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
    ) -> None:
        # Decouple capture from STT: a dedicated task drains the mic + VAD into a
        # bounded queue that the router consumes independently, so a slow STT
        # round-trip (e.g. a long sentence) cannot stall frame capture and
        # overflow the device buffer. Under sustained overload the oldest queued
        # utterance is dropped (logged) instead of corrupting live frames.
        queue: asyncio.Queue[object] = asyncio.Queue(maxsize=_UTTERANCE_QUEUE_MAX)
        capture_task = asyncio.create_task(
            _capture_utterances(source, detector, chunker, audio_writer, queue)
        )
        try:
            await router.run(_dequeue(queue))
        finally:
            await capture_task

    async def _persist(self, session_bus: EventBus[TranscriptEvent], started_mono: float) -> None:
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
    queue: asyncio.Queue[object],
) -> None:
    """Drain frames through VAD + chunking into ``queue`` (never blocks).

    Runs as its own task so capture keeps emptying the device buffer regardless
    of STT latency. Every frame is written to the continuous session recording
    and each completed utterance's span is indexed, so stored audio stays
    complete (and re-VAD-able) even if the live queue drops an utterance.
    """
    async for frame, ts in source.frames():
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
    final = chunker.flush()
    if final is not None:
        if audio_writer is not None:
            await asyncio.to_thread(audio_writer.mark_utterance, final)
        _offer(queue, final)
    _offer(queue, _CAPTURE_DONE)


async def _dequeue(queue: asyncio.Queue[object]) -> AsyncIterator[Utterance]:
    """Yield queued utterances until the capture-done sentinel arrives."""
    while True:
        item = await queue.get()
        if item is _CAPTURE_DONE:
            return
        yield cast("Utterance", item)
