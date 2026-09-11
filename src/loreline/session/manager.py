"""Session orchestration: capture -> transcription -> persistence -> bus.

``SessionManager`` owns the single active capture session. It wires a frame
source + speech detector into whichever live path the chosen model needs, and
persists every emitted ``TranscriptEvent``. Hardware-facing factories (audio
source/detector, STT backends, diarizer) are injectable so the manager can run
fully offline in tests.

There are two live paths, and the connector's shape decides which one a session
takes (ADR 0006). Both are fed by the one capture loop below, which is the same
loop either way: it records the stats, meters the level, writes every frame to
the continuous WAV, watches the disk, runs Silero and the ``VadChunker``, and
marks each completed utterance in the WAV's index. All it does differently is
hand what it produced to a different collaborator.

* **Utterances.** ``SttRouter`` gets one ``Utterance`` at a time through a
  bounded queue and calls a connector per utterance. Every batch model and
  every realtime model whose connector has not been migrated yet.
* **Frames.** ``StreamPath`` (``loreline.session.streaming``) gets every frame
  and hands them to a connector that decides its own turns, publishing interims
  while a turn is open. Taken when the primary connector implements the
  streaming shape. The chunker keeps running for the WAV index; its utterances
  simply go nowhere until the session falls back to the other path.
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Generator
from dataclasses import dataclass, field
from functools import partial
from typing import TYPE_CHECKING, Protocol, cast, runtime_checkable

from loreline.audio.chunker import SpeechDetector, Utterance, VadChunker
from loreline.audio.level import levels
from loreline.audio.source import CaptureUnavailableError
from loreline.bus import EventBus
from loreline.capabilities import supports_inline_diarization, supports_live_capture
from loreline.logging import bind_log_context, get_logger, log_context
from loreline.models import (
    CaptureSourceKind,
    DiarizationMode,
    Session,
    SessionStatus,
    TranscriptEvent,
    rebase_transcript,
)
from loreline.monitoring.alerts import AlertLevel
from loreline.monitoring.health import disk_usage
from loreline.session.streaming import FrameSink, PathEnd, StreamPath
from loreline.stt.registry import BackendFactory, create_backend
from loreline.stt.router import ProvidersExhaustedError, RouterConfig, SttRouter
from loreline.stt.streaming import is_streaming

if TYPE_CHECKING:
    from loreline.audio.client_source import ClientMic
    from loreline.diarization.base import DiarizationProvider
    from loreline.diarization.provider import BuildDiarizer
    from loreline.models import DiarizationConfig, Glossary, ProviderConfig
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
    """A stoppable source of timestamped PCM frames.

    Two things are one: ``SoundDeviceSource`` opens a sound card on this
    machine through PortAudio, and ``ClientCaptureSource`` is fed by a
    browser's microphone over a WebSocket (``docs/adr/0010``). Everything
    below this protocol - the capture loop, ``_CaptureStats``, the level
    publisher, the disk watch, the WAV writer, ``StreamPath``, ``SttRouter`` -
    is written against the frames and has no idea which one it is reading, and
    keeping it that way is the whole of what makes a browser capture an
    ordinary session rather than a second pipeline.
    """

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


class DiskFullError(RuntimeError):
    """The recording disk ran out of space; the audio written so far is intact.

    Raised in place of the raw ``ENOSPC`` ``OSError`` the audio writer throws,
    so the teardown can tell this apart from a capture that crashed. Nothing can
    keep recording without space to record into, so the session still ends - but
    it ends *completed*, because everything written up to that point is a
    complete, re-transcribable WAV. Carries ``saved_seconds`` so the alert can
    say how much audio is safe, which is exactly what a GM reading a generic
    "session error" would have every reason to doubt.
    """

    def __init__(self, *, saved_seconds: float) -> None:
        super().__init__(f"no space left on device after {saved_seconds:.1f}s of audio")
        self.saved_seconds = saved_seconds


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


def capture_factory_for(
    mic: ClientMic, *, build_detector: Callable[[int], SpeechDetector] | None = None
) -> CaptureFactory:
    """The application's capture factory: the server's device, or a browser's.

    The whole of the client-microphone feature below the seam is this one
    branch. ``ClientMic`` is the process's single pending capture socket, so
    the factory is built once, over it, and closes over nothing else; what a
    start request chooses is which ``CaptureSource`` gets built, and every
    collaborator past that point is handed the same thing either way.

    The detector is the same VAD both ways, because the frames are: a client
    source resamples to the session's rate before anyone downstream sees them,
    exactly as a device that serves another rate does. ``build_detector`` is
    injectable for the same reason the factory itself is - constructing Silero
    loads the optional ``audio`` extra - and defaults to the one detector every
    VAD pass in this app runs on.
    """

    def build(req: StartSessionRequest, sample_rate: int) -> tuple[CaptureSource, SpeechDetector]:
        if req.source is not CaptureSourceKind.CLIENT:
            return _default_capture(req, sample_rate)
        from loreline.audio.vad import default_detector  # noqa: PLC0415

        detector = (build_detector or default_detector)(sample_rate)
        return mic.open(sample_rate), detector

    return build


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


_BYTES_PER_SAMPLE = 2  # mono s16le, the one format capture writes
# How often an active capture re-reads free space. A session writes about
# 115 MB an hour, so half a minute of recording costs under a megabyte of
# headroom: often enough to warn long before the floor is gone, rare enough to
# be one stat() per ~1500 frames.
_DISK_CHECK_INTERVAL_S = 30.0
# Free space has to climb this far back above the floor before a second low-disk
# alert can fire. Without it, a session sitting on the boundary (a byte over,
# a byte under) would alert every half minute for the rest of the evening.
_DISK_REARM_FACTOR = 1.1
# Past an hour and a half, an alert reads better in hours than in minutes.
_MINUTES_BEFORE_HOURS = 90
# How often a level reading is pushed to the dashboard's live gain meter. Frames
# arrive every 20 ms; pushing on every one would be both wasted bandwidth and
# more updates than the eye needs from a bar graph.
_LEVEL_PUSH_INTERVAL_S = 0.1


@dataclass(slots=True)
class _DiskWatch:
    """Watches the recording disk's headroom for as long as a session records.

    ``/healthz`` already grades free space, but nothing was looking at it *while
    a session ran*: an evening of capture writes gigabytes, so a disk that was
    comfortable at Start can cross the floor hours later with nothing but a
    badge on a page nobody is watching to say so. This runs from inside the
    capture loop rather than as a task of its own - it then lives exactly as
    long as the recording does, with no second lifecycle to start, cancel and
    await - and self-throttles to one reading per ``interval_s``.

    ``on_low`` fires once per crossing, not once per check.
    """

    free_bytes: Callable[[], int]
    threshold_bytes: int
    on_low: Callable[[int], Awaitable[None]]
    interval_s: float = _DISK_CHECK_INTERVAL_S
    _next_check: float = field(default=0.0, init=False)  # 0 -> the first frame checks
    _alerted: bool = field(default=False, init=False)

    async def check(self) -> None:
        """Read free space at most once per interval; alert on a new crossing."""
        now = time.monotonic()
        if now < self._next_check:
            return
        self._next_check = now + self.interval_s
        free = await asyncio.to_thread(self.free_bytes)
        if free >= self.threshold_bytes * _DISK_REARM_FACTOR:
            self._alerted = False  # recovered, so a later crossing alerts again
        elif free < self.threshold_bytes and not self._alerted:
            self._alerted = True
            await self.on_low(free)


@dataclass(slots=True)
class _LevelWatch:
    """Throttled peak/RMS publisher for the frames flowing through capture.

    The dashboard's live gain meter needs the same reading the pre-session
    mic-test meter shows (``loreline.audio.level.levels``, also behind
    ``/ws/audio/level``), but taken from the frames already flowing through
    this loop rather than a second device stream - some devices refuse a
    second simultaneous reader (see the mic-resampling fix). Runs from inside
    the capture loop for the same reason ``_DiskWatch`` does: it then lives
    exactly as long as the recording, with no lifecycle of its own to start,
    cancel and await.

    ``peak`` is held across the interval rather than read fresh at the end of
    it, the same way the mic-test meter holds its own reading between
    throttled sends - a loud spike between two pushes must not be lost to
    whatever quieter frame happens to land on the boundary.
    """

    publish: Callable[[tuple[float, float]], Awaitable[None]]
    interval_s: float = _LEVEL_PUSH_INTERVAL_S
    _hold_peak: float = field(default=0.0, init=False)
    _next_push: float = field(default=0.0, init=False)  # 0 -> the first frame publishes

    async def record(self, frame: bytes) -> None:
        """Fold in one frame's level; publish at most once per interval."""
        peak, rms = levels(frame)
        self._hold_peak = max(self._hold_peak, peak)
        now = time.monotonic()
        if now < self._next_push:
            return
        self._next_push = now + self.interval_s
        await self.publish((self._hold_peak, rms))
        self._hold_peak = 0.0


def _megabytes(byte_count: float) -> int:
    return round(byte_count / (1024 * 1024))


def _approx_duration(seconds: float) -> str:
    """A rough spoken length ("40 minutes", "3.5 hours") for an alert."""
    minutes = seconds / 60
    if minutes < 1:
        return "less than a minute"
    if minutes < _MINUTES_BEFORE_HOURS:
        whole = round(minutes)
        return f"{whole} minute{'s' if whole != 1 else ''}"
    return f"{minutes / 60:.1f} hours"


def _low_disk_message(free: int, threshold: int, bytes_per_second: int) -> str:
    """What the GM is told while there is still time to do something about it."""
    left = _approx_duration(free / bytes_per_second) if bytes_per_second > 0 else "unknown"
    return (
        f"Free space is down to {_megabytes(free)} MB, below the "
        f"{_megabytes(threshold)} MB floor. That is about {left} of recording left. "
        "Free some space now: when the disk fills, the recording stops."
    )


def _disk_full_message(saved_seconds: float) -> str:
    """What the GM is told once the disk is actually full.

    Deliberately not a crash report. The recording did stop early, but the audio
    already on disk is complete and re-transcribable, and "session error" on its
    own invites exactly the opposite conclusion - the same reason ``stt_error``
    carries the vendor's own sentence instead of "transcription stopped".
    """
    return (
        "The disk is full, so recording stopped and the session was closed. The "
        f"{_approx_duration(saved_seconds)} of audio captured before that is saved and "
        "complete: it can still be transcribed and re-processed. Free some space before "
        "starting the next session."
    )


@runtime_checkable
class _SttHealth(Protocol):
    """What a live path answers about its own transcription, for the dashboard.

    Both paths answer it, which is the point: ``/healthz`` and the dashboard
    ask the manager, and the manager must not need to know which path is
    running to have an answer. ``SttRouter`` has had both properties since
    before there was a second path; ``StreamPath`` grew them to match, and
    delegates to a router once a session has handed over to one.
    """

    @property
    def degraded_since(self) -> float | None:
        """Epoch time live transcription entered its current failing streak."""
        ...

    @property
    def terminal_error(self) -> str | None:
        """Why transcription stopped for good, or None while it still works."""
        ...


@dataclass(slots=True)
class _Capture:
    """Everything the capture loop needs, as one value the live paths share.

    Both paths start the same capture task from it and both read ``queue``,
    one to drive the router and one to hold what it might hand over, so
    passing it as a bundle keeps the two constructions honestly identical.
    """

    source: CaptureSource
    detector: SpeechDetector
    chunker: VadChunker
    audio_writer: SessionAudioWriter | None
    stats: _CaptureStats
    queue: asyncio.Queue[object]


@dataclass(slots=True)
class _Runtime:
    session: Session
    source: CaptureSource
    source_kind: CaptureSourceKind
    session_bus: EventBus[TranscriptEvent]
    stt: _SttHealth
    live_task: asyncio.Task[None]
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
        disk_threshold_bytes: int = 0,
    ) -> None:
        self._providers = providers
        self._glossaries = glossaries
        self._sessions = sessions
        self._transcripts = transcripts
        self._secrets = secrets
        self._bus = transcript_bus
        # Long-lived like `_bus` above, not per-session: readings only ever
        # arrive while a capture is running, so a subscriber needs no session
        # filter of its own to know a reading is about "the mic right now".
        self._level_bus: EventBus[tuple[float, float]] = EventBus()
        self._audio_store = audio_store
        self._alerter = alerter
        # Free-space floor for the live check during capture; the same number
        # /healthz grades the badge against. 0 turns the check off.
        self._disk_threshold_bytes = disk_threshold_bytes
        self._capture_factory = capture_factory or _default_capture
        self._backend_factory = backend_factory or create_backend
        self._diarizer_factory = diarizer_factory
        self._runtime: _Runtime | None = None
        # Set beside the runtime and cleared a teardown later, which is the
        # whole difference between the two (see live_view_session_id).
        self._live_view_session_id: str | None = None
        self._lock = asyncio.Lock()
        # Holds the task that finalizes a session whose capture died on its own
        # (see _router_finished); asyncio only keeps a weak reference to a task,
        # so dropping this one could have the teardown collected mid-flight.
        self._unattended_end: asyncio.Task[None] | None = None

    @property
    def transcript_bus(self) -> EventBus[TranscriptEvent]:
        return self._bus

    @property
    def level_bus(self) -> EventBus[tuple[float, float]]:
        """Throttled ``(peak, rms)`` readings from the active capture, if any.

        What ``/ws/audio/live-level`` subscribes to for the dashboard's live
        gain meter - see ``_LevelWatch``.
        """
        return self._level_bus

    def status(self) -> SessionStatus:
        return SessionStatus.CAPTURING if self._runtime is not None else SessionStatus.IDLE

    def current_session_id(self) -> str | None:
        """The session with the microphone open right now, or None.

        Strictly "is a capture running": it goes to None the moment Stop takes
        the runtime out of its slot, before a single line of the teardown has
        been written. Callers that guard the recording itself want exactly that
        (a merge must refuse the growing WAV, an index rebuild must skip it).
        Callers showing a GM what the session is *doing* want
        :meth:`live_view_session_id` instead.
        """
        return self._runtime.session.id if self._runtime is not None else None

    def live_view_session_id(self) -> str | None:
        """The session the dashboard's live panes are currently about, or None.

        The same id as :meth:`current_session_id` while a capture runs, and it
        stays set for the whole of the teardown that follows - which is the
        only difference, and the reason both exist. Everything a session says
        on its way out is said after the runtime is gone: the settled finals
        the drain squeezes out of open streaming turns, the trailing gap
        marker, ``audio.capture.stop``, ``session.stop`` itself. Filtered on
        "a capture is running right now", the panes drop all of it - the log
        pane freezes on the line before Stop and a dimmed interim row stays
        dimmed for good, while the card above it promises "Transcribing what is
        still queued and closing the recording".

        None again once the teardown ends, so an idle dashboard is silent: this
        says which session the live view belongs to, never that the process is
        busy.
        """
        return self._live_view_session_id

    def stt_degraded_since(self) -> float | None:
        """Epoch time the active session's transcription started failing, or None."""
        runtime = self._runtime
        return runtime.stt.degraded_since if runtime is not None else None

    def stt_error(self) -> str | None:
        """Why the active session stopped transcribing for good, or None.

        Carries the vendor's own sentence ("OpenAI: You have no credits
        remaining."), because "transcription stopped" on its own tells a GM
        nothing they can act on. Null while any provider still works, and while
        idle.
        """
        runtime = self._runtime
        return runtime.stt.terminal_error if runtime is not None else None

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

    def capture_source(self) -> CaptureSourceKind | None:
        """Which microphone the active session is listening to, or None while idle.

        The dashboard's answer to "where is this audio coming from". It matters
        on a second screen: a phone opening the dashboard while the laptop on
        the table is the microphone has to read the session as capturing and
        still know that closing *this* page costs nothing, while closing the
        laptop's tab ends the evening.
        """
        runtime = self._runtime
        return runtime.source_kind if runtime is not None else None

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
            persist_task = asyncio.create_task(
                self._persist(session_bus, session.started_mono, session.id)
            )
            stats = _CaptureStats(sample_rate=sample_rate)
            capture = _Capture(
                source=source,
                detector=detector,
                chunker=chunker,
                audio_writer=audio_writer,
                stats=stats,
                # Decouples capture from STT: a dedicated task drains the mic
                # into this queue and the consumer works through it
                # independently, so a slow round-trip cannot stall frame
                # capture and overflow the device buffer. The streaming path
                # leaves it empty until it hands a session over.
                queue=asyncio.Queue(maxsize=_UTTERANCE_QUEUE_MAX),
            )
            build_router = partial(
                self._build_router,
                bus=session_bus,
                config=RouterConfig(
                    session_id=session.id,
                    glossary=glossary,
                    diarization=req.diarization,
                ),
                diarizer=diarizer,
            )
            stt, live_task = self._start_live_path(
                primary,
                fallback,
                capture=capture,
                session=session,
                session_bus=session_bus,
                glossary=glossary,
                diarization=req.diarization,
                diarizer=diarizer,
                build_router=build_router,
            )
            self._runtime = _Runtime(
                session=session,
                source=source,
                source_kind=req.source,
                session_bus=session_bus,
                stt=stt,
                live_task=live_task,
                persist_task=persist_task,
                backends=backends,
                diarizer=diarizer,
                audio_writer=audio_writer,
                stats=stats,
            )
            # From here until this session has finished ending, the dashboard's
            # panes are about it - teardown included, which the runtime above
            # does not cover (see live_view_session_id).
            self._live_view_session_id = session.id
            # Nothing else awaits this task between here and stop(), so without
            # a callback a capture that dies mid-session would keep reporting
            # "capturing" until someone pressed Stop and waited out the drain.
            live_task.add_done_callback(self._live_finished)
            log.info("session.start", session_id=session.id, primary=req.primary_provider)
            return session

    def _start_live_path(
        self,
        primary: STTBackend,
        fallback: STTBackend | None,
        *,
        capture: _Capture,
        session: Session,
        session_bus: EventBus[TranscriptEvent],
        glossary: Glossary | None,
        diarization: DiarizationConfig,
        diarizer: DiarizationProvider,
        build_router: Callable[..., SttRouter],
    ) -> tuple[_SttHealth, asyncio.Task[None]]:
        """Pick the live path this session's primary connector needs.

        The shape of the connector decides, not the model row and not a flag:
        ``create_backend`` already returns the realtime connector for a model
        whose ``prefer`` says so, and whether that connector can be fed raw
        frames is a fact about its class (ADR 0006, Decision 2). A realtime
        connector that has not been migrated yet is still call-shaped, so its
        models keep today's path and today's 800 ms floor until it is.
        """
        if is_streaming(primary):
            path = StreamPath(
                primary,
                session_bus,
                session_id=session.id,
                capture_rate=capture.stats.sample_rate,
                glossary=glossary,
                diarization=diarization,
                diarizer=diarizer,
                fallback=fallback,
                on_failover=self._failover_alert,
                queue_utterance=partial(_offer, capture.queue),
            )
            return path, asyncio.create_task(
                self._run_stream(path, capture, build_router, session.id)
            )
        router = build_router(primary, fallback=fallback)
        return router, asyncio.create_task(self._run_router(router, capture, session.id))

    def _build_router(
        self,
        primary: STTBackend,
        *,
        bus: EventBus[TranscriptEvent],
        config: RouterConfig,
        diarizer: DiarizationProvider,
        fallback: STTBackend | None = None,
    ) -> SttRouter:
        """One router, however this session came to need one.

        Built here rather than at each call site because the streaming path can
        need one mid-session, when it hands over to a call-shaped fallback, and
        that router must be configured identically to the one the session would
        have had if it had never streamed at all.
        """
        return SttRouter(
            primary,
            bus,
            config,
            fallback=fallback,
            diarizer=diarizer,
            on_failover=self._failover_alert,
        )

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
        except CaptureUnavailableError as exc:
            # A source that already knows what to tell a GM (the browser is not
            # streaming, the tab has gone). Passed through verbatim rather than
            # wrapped in the device sentence below, which would send someone to
            # Settings to pick a microphone that was never the problem.
            log.warning("session.start.capture_unavailable", error=str(exc))
            raise SessionConfigError(str(exc)) from exc
        except Exception as exc:
            log.warning("session.start.device_unavailable", device=device, error=str(exc))
            named = "the default input device" if device is None else f"input device {device!r}"
            msg = (
                f"{named} could not be opened for recording ({exc}). Pick another "
                "microphone in Settings, or check that nothing else is using it."
            )
            raise SessionConfigError(msg) from exc

    async def stop(self) -> Session | None:
        """Stop the active session and finalize persistence.

        The runtime is still taken out of its slot first thing under the lock,
        because that swap is what makes a teardown owned by exactly one caller:
        whoever empties the slot runs it, and :meth:`_end_unattended` finding it
        already empty knows stop() got there first.

        What changed is that the teardown now runs *while the lock is still
        held*. Finalizing outside it left the slot free for a Start arriving in
        those seconds, with the old capture still holding the microphone, still
        patching its WAV header and still writing its last rows - so the new
        session pre-flighted a device that had not been given back, and two
        capture loops could overlap. "One session at a time" has to mean until
        the previous one has finished ending, not until it has stopped
        recording. The price is that a second Stop, or a Start from another
        tab, waits rather than being answered instantly; the wait is the drain
        timeout at worst, plus finalizing steps that are each a bounded write
        or a call with its own timeout, and it ends with a session that can
        actually record.
        """
        async with self._lock:
            runtime = self._runtime
            if runtime is None:
                return None
            self._runtime = None
            with self._tearing_down(runtime.session.id):
                return await self._finish(runtime)

    @contextlib.contextmanager
    def _tearing_down(self, session_id: str) -> Generator[None, None, None]:
        """Keep a session's log context and its live view up for the teardown.

        Both outlive the runtime for the same reason: they are about what a
        session *says* on the way out, not about whether it is still recording.
        The lines come from code holding no session id at all (the drain, the
        audio writer, the router shutting its backends), so the context binds it
        on their behalf; and the dashboard's panes stay pointed at this session
        until the last of those lines and the last settled final have gone out
        (see :meth:`live_view_session_id`).

        The live view is dropped in a ``finally`` so a teardown step that raises
        cannot leave the panes stuck on a session that ended, and only if this
        session is still the one on show - defensive today, since the lock is
        held throughout, but the alternative if that ever changes is blanking
        the pane of whichever session took over.
        """
        with log_context(session_id=session_id):
            try:
                yield
            finally:
                if self._live_view_session_id == session_id:
                    self._live_view_session_id = None

    def _live_finished(self, task: asyncio.Task[None]) -> None:
        """Notice a capture that ended itself, and end the session with it.

        Runs as ``live_task``'s done callback. A live path that raised has taken
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
        if runtime is None or runtime.live_task is not task:
            return  # stop() already owns the teardown
        self._unattended_end = asyncio.create_task(self._end_unattended(task))

    async def _end_unattended(self, task: asyncio.Task[None]) -> None:
        """Finalize a session nobody asked to stop (see :meth:`_live_finished`).

        Deliberately the same teardown ``stop()`` runs, under the same lock and
        the same :meth:`_tearing_down` scope: ``_finish`` re-awaits the live
        task, so the exception that killed it decides the stored status and
        fires the "Session error" alert, exactly as it would have done had the
        GM pressed Stop, and the dashboard sees the ending either way.
        """
        async with self._lock:
            runtime = self._runtime
            if runtime is None or runtime.live_task is not task:
                return  # stop() got there first
            self._runtime = None

            with self._tearing_down(runtime.session.id):
                # A full disk is not a death: the capture stopped on purpose
                # because there was nowhere left to write, and _finish logs it
                # as such.
                if not isinstance(task.exception(), DiskFullError):
                    log.error("session.capture.died", session_id=runtime.session.id)
                try:
                    await self._finish(runtime)
                except Exception:  # pragma: no cover - defensive: this task has no caller
                    log.exception("session.finish.failed", session_id=runtime.session.id)

    async def _finish(self, runtime: _Runtime) -> Session:
        """Drain, close and finalize a stopped session's runtime.

        Split out of :meth:`stop` only so the whole teardown runs inside one
        :meth:`_tearing_down` scope: the lines it produces (a drain timeout, a
        router crash, the capture source closing) belong to the session that is
        ending, and reach its log file and the dashboard, even though none of
        the code emitting them is holding its id.
        """
        runtime.source.stop()
        session_id = runtime.session.id
        status = SessionStatus.COMPLETED
        disk_full: DiskFullError | None = None
        try:
            # Bounded drain: with STT healthy the live path finishes in
            # moments, but an unreachable backend leaves a deep utterance
            # backlog where every entry burns a full per-utterance timeout -
            # holding this stop request (and the shutdown path) hostage for up
            # to half an hour. The audio + index are already on disk, so cut
            # the drain short instead: wait_for cancels the live task, and the
            # skipped tail stays re-transcribable from stored audio.
            await asyncio.wait_for(runtime.live_task, timeout=_STOP_DRAIN_TIMEOUT_S)
        except TimeoutError:
            log.warning("session.stop.drain_timeout", session_id=session_id)
        except DiskFullError as exc:
            # The recording ended because the disk did, which is not a crash:
            # the WAV is complete up to this point, so the session finalizes as
            # completed and the alert below explains why the evening is short.
            disk_full = exc
            log.error(
                "session.capture.disk_full",
                session_id=session_id,
                saved_seconds=round(exc.saved_seconds, 1),
            )
        except Exception:
            log.exception("session.live_path.failed", session_id=session_id)
            status = SessionStatus.ERROR

        await runtime.session_bus.aclose()
        with _keep_finalizing("persist", session_id):
            # Every step from here on is best-effort for one reason: a disk with
            # no room left fails the transcript write, the sidecar write and the
            # status update alike, and a session left at "capturing" forever is
            # the one outcome worse than a session that ended early.
            await runtime.persist_task
        with _keep_finalizing("interims", session_id):
            # After the last event is stored, never before: the streaming path
            # settles its open turn on the way out, and that final has to land
            # first or this would delete the interim it was about to replace.
            # A stored session holds no half-typed rows (see
            # TranscriptRepository.delete_interims); the utterance path writes
            # none, so this is a no-op for it.
            await self._transcripts.delete_interims(session_id)

        if runtime.audio_writer is not None:
            with _keep_finalizing("audio_writer", session_id):
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

        with _keep_finalizing("sessions.finish", session_id):
            await self._sessions.finish(session_id, status)
        runtime.session.status = status
        log.info("session.stop", session_id=session_id, status=status.value)
        # Sent last, and never skipped by a failed step above: when the disk is
        # what broke, a push notification is the only channel still working.
        if disk_full is not None:
            await self._notify(
                "Recording stopped: disk full",
                _disk_full_message(disk_full.saved_seconds),
                level=AlertLevel.ERROR,
            )
        elif status is SessionStatus.ERROR:
            await self._notify(
                "Session error",
                f"Session {session_id} ended with an error.",
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

    def _make_disk_watch(self, sample_rate: int) -> _DiskWatch | None:
        """Watch headroom on the filesystem this session's audio lands on.

        None when there is nothing to watch: no audio store (this session writes
        no recording) or no configured floor. The path is the audio store's own
        directory rather than the data dir, so the check follows the recording
        even where audio is mounted on a separate disk.
        """
        if self._audio_store is None or self._disk_threshold_bytes <= 0:
            return None
        root = self._audio_store.root
        threshold = self._disk_threshold_bytes
        bytes_per_second = sample_rate * _BYTES_PER_SAMPLE

        def free_bytes() -> int:
            return disk_usage(root)[0]

        async def on_low(free: int) -> None:
            log.warning("session.disk.low", free_bytes=free, threshold_bytes=threshold)
            await self._notify(
                "Disk space low",
                _low_disk_message(free, threshold, bytes_per_second),
                level=AlertLevel.WARNING,
            )

        return _DiskWatch(free_bytes=free_bytes, threshold_bytes=threshold, on_low=on_low)

    def _start_capture(
        self, capture: _Capture, *, frames: FrameSink | None = None
    ) -> asyncio.Task[None]:
        """Spawn the one capture task, whichever live path consumes it."""
        return asyncio.create_task(
            _capture_utterances(
                capture.source,
                capture.detector,
                capture.chunker,
                capture.audio_writer,
                capture.stats,
                capture.queue,
                disk_watch=self._make_disk_watch(capture.stats.sample_rate),
                level_watch=_LevelWatch(publish=self._level_bus.publish),
                frames=frames,
            )
        )

    async def _run_router(self, router: SttRouter, capture: _Capture, session_id: str) -> None:
        # Everything below (the router, the STT backends, the VAD, the capture
        # task spawned here) logs without ever being told which session it is
        # serving. Bind it once for this task so those lines can be attributed
        # to the capture: the dashboard shows them, and they are what the
        # session's stored "original" log is made of.
        bind_log_context(session_id=session_id)
        capture_task = self._start_capture(capture)
        try:
            await self._drive_router(router, capture.queue, session_id)
        finally:
            await capture_task

    async def _run_stream(
        self,
        path: StreamPath,
        capture: _Capture,
        build_router: Callable[..., SttRouter],
        session_id: str,
    ) -> None:
        """Drive the streaming live path, including whatever it falls back to.

        Same three endings the router has, reached differently (ADR 0006,
        Decision 3): the input ran out, a call-shaped fallback takes over the
        rest of the session, or nothing is left to transcribe with.
        """
        bind_log_context(session_id=session_id)  # see _run_router
        capture_task = self._start_capture(capture, frames=path)
        try:
            end = await path.run()
            if end == PathEnd.HANDOFF:
                await self._hand_off(path, capture, build_router, session_id)
            elif end == PathEnd.EXHAUSTED:
                await self._stt_exhausted(path.terminal_error or "", capture.queue, session_id)
        finally:
            await capture_task

    async def _hand_off(
        self,
        path: StreamPath,
        capture: _Capture,
        build_router: Callable[..., SttRouter],
        session_id: str,
    ) -> None:
        """Finish the session on the utterance path (see ``StreamPath.handoff``).

        Either the streaming providers are gone and a call-shaped fallback is
        left, or the primary turned out not to stream the model this session
        picked. Either way, from the next completed utterance on this is an
        ordinary session. The chunker never stopped running for the WAV index,
        so there is nothing to start: the utterances simply begin reaching the
        queue, and the router built here drains it exactly as it would have
        from the beginning.
        """
        handoff = path.handoff
        if handoff is None:  # pragma: no cover - run() only returns HANDOFF with one
            return
        primary, fallback = handoff
        router = build_router(primary, fallback=fallback)
        path.router = router  # the session's health is this router's from now on
        path.hand_off()
        log.warning("session.stream.handoff", session_id=session_id, provider_id=primary.config.id)
        await path.notify_failover(
            f"Live transcription is running through {primary.config.name} one utterance "
            "at a time: text arrives after each sentence rather than while it is spoken."
        )
        await self._drive_router(router, capture.queue, session_id)

    async def _drive_router(
        self, router: SttRouter, queue: asyncio.Queue[object], session_id: str
    ) -> None:
        """Run the utterance path until capture ends or every provider is dead."""
        utterances = _dequeue(queue)
        try:
            await router.run(utterances)
        except ProvidersExhaustedError as exc:
            await self._stt_exhausted(str(exc), queue, session_id, drain=utterances)

    async def _stt_exhausted(
        self,
        error: str,
        queue: asyncio.Queue[object],
        session_id: str,
        drain: AsyncIterator[Utterance] | None = None,
    ) -> None:
        """Every provider has failed for good: keep recording, stop transcribing.

        The failures that get here will not change (no credits, a rejected key,
        a missing model). The capture deliberately keeps running: the audio is
        the one artifact that cannot be produced again, and a stopped
        microphone loses the rest of the evening outright, while a transcript
        can be re-made from the stored WAV the moment the provider works again.
        So the session degrades to a recorder, and the vendor's reason is
        pushed at the GM instead, as an alert and on the dashboard via
        ``stt_error``.

        Draining is part of keeping the recording clean: without a consumer the
        queue fills, and every further utterance logs a drop it can do nothing
        about. The streaming path passes no ``drain`` because nothing was ever
        queued while it ran, but it still has to keep draining once the chunker
        starts filling the queue behind it.
        """
        log.error("session.stt.exhausted", session_id=session_id, error=error)
        await self._notify(
            "Transcription stopped",
            f"Live transcription stopped ({error}). Audio keeps recording; "
            "the session can be re-transcribed later.",
            level=AlertLevel.ERROR,
        )
        async for _utterance in drain if drain is not None else _dequeue(queue):
            pass

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


@contextlib.contextmanager
def _keep_finalizing(step: str, session_id: str) -> Generator[None, None, None]:
    """Log a teardown step that failed and carry on with the next one.

    Used only inside :meth:`SessionManager._finish`, where giving up halfway
    leaves the session row at ``capturing`` for good - no code path revisits it
    short of a restart, and the GM is told nothing at all.
    """
    try:
        yield
    except Exception:
        log.exception("session.teardown.failed", step=step, session_id=session_id)


async def _store_audio(write: Callable[[], None], saved_seconds: float) -> None:
    """Run one blocking audio-store write off the loop, naming a full disk.

    ``ENOSPC`` is the write failure worth telling apart: everything already
    written is intact, so it ends the recording as a clean stop rather than as
    the crash every other ``OSError`` here really is. Raised as
    :class:`DiskFullError` for the teardown to recognise, and translated at this
    call site rather than in the writer so any writer - the real one, or a fake
    in a test - gets the same treatment.
    """
    try:
        await asyncio.to_thread(write)
    except OSError as exc:
        if exc.errno != errno.ENOSPC:
            raise
        raise DiskFullError(saved_seconds=saved_seconds) from exc


def _dispatch(queue: asyncio.Queue[object], frames: FrameSink | None, utterance: Utterance) -> None:
    """Give a completed utterance to whoever transcribes those, if anyone does.

    Nobody does while a session streams: the vendor is deciding the turns, so
    the utterance was only ever the WAV index's business. The sink says when
    that changes, which is when a session hands over to the utterance path.
    """
    if frames is None:
        _offer(queue, utterance)
    elif frames.queues_utterances:
        frames.utterance(utterance)


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
    disk_watch: _DiskWatch | None = None,
    level_watch: _LevelWatch | None = None,
    frames: FrameSink | None = None,
) -> None:
    """Drain frames through VAD + chunking to whoever is transcribing them.

    Runs as its own task so capture keeps emptying the device buffer regardless
    of STT latency, and never blocks on either consumer. Every frame is written
    to the continuous session recording and each completed utterance's span is
    indexed, so stored audio stays complete (and re-VAD-able) whatever the live
    path does with it. Each frame also feeds ``level_watch``, if given, so the
    dashboard's gain meter sees the same audio the recording does.

    ``frames`` is the streaming path's sink (``StreamPath``). With one, every
    frame goes to it as well, and completed utterances stop reaching ``queue``:
    the vendor is deciding the turns, so an utterance is the WAV index's
    business only, until the session hands over to the utterance path and the
    sink says it wants them again. Without one this is exactly the loop it has
    always been.

    However this ends - stopped, cancelled, or with the frame source raising
    before it ever yields - both consumers are closed; see
    :func:`_close_capture` for why that matters more than anything else here.
    """
    try:
        async for frame, ts in source.frames():
            stats.record(frame)
            if level_watch is not None:
                await level_watch.record(frame)
            if audio_writer is not None:
                # Disk write off the event loop: this task is already decoupled
                # from STT latency, but a blocking write here would still stall
                # every other coroutine (health polls, WS streams, HTTP requests)
                # for its duration.
                write = partial(audio_writer.append_frame, frame)  # incl. silence
                await _store_audio(write, stats.seconds)
            if disk_watch is not None:
                # Watched from in here because this loop runs for exactly as
                # long as the recording does; the watch reads the disk once
                # every _DISK_CHECK_INTERVAL_S, not once per frame.
                await disk_watch.check()
            # ONNX inference off the event loop for the same reason.
            is_speech = await asyncio.to_thread(detector, frame)
            if frames is not None:
                # After the VAD rather than before it: the streaming path needs
                # the verdict with the frame, for its liveness watchdog. The
                # cost is one thread hop of latency on a network round trip.
                frames.frame(frame, ts, is_speech=is_speech)
            utterance = chunker.feed(frame, ts=ts, is_speech=is_speech)
            if utterance is not None:
                if audio_writer is not None:
                    # mark_utterance persists the index sidecar - disk I/O, so off
                    # the event loop like the frame writes above.
                    mark = partial(audio_writer.mark_utterance, utterance)
                    await _store_audio(mark, stats.seconds)
                _dispatch(queue, frames, utterance)
    except DiskFullError:
        # Not a crash, so not a traceback: the recording stopped because there
        # was nowhere left to put it, and what reached the disk is complete.
        log.error("capture.disk_full", saved_seconds=round(stats.seconds, 1))
        raise
    except Exception:
        # Logged here, where the session context is still bound, rather than
        # left for whoever eventually awaits this task.
        log.exception("capture.failed", captured_seconds=round(stats.seconds, 1))
        raise
    finally:
        await _close_capture(chunker, audio_writer, stats, queue, frames)


async def _close_capture(
    chunker: VadChunker,
    audio_writer: SessionAudioWriter | None,
    stats: _CaptureStats,
    queue: asyncio.Queue[object],
    frames: FrameSink | None,
) -> None:
    """Flush the chunker and close both consumers, however capture ended.

    Runs from ``_capture_utterances``'s ``finally``, which is the point: this
    has to happen when the source was stopped, when the task was cancelled, and
    when the frame source raised before it ever yielded.

    The queue's sentinel is the only thing that ends ``_dequeue`` and the
    sink's ``done`` is the only thing that ends an open stream, so skipping
    either leaves a consumer blocked for as long as the process lives: the
    session then reports ``capturing`` with a dead microphone, and even Stop
    only unblocks it by timing out. That is the zombie recording this exists to
    prevent, which is why the sentinel is delivered from inside a ``finally``
    of its own rather than after a flush that might raise.
    """
    try:
        final = chunker.flush()
        if final is not None:
            if audio_writer is not None:
                await _store_audio(partial(audio_writer.mark_utterance, final), stats.seconds)
            _dispatch(queue, frames, final)
    finally:
        try:
            if frames is not None:
                frames.done()
        finally:
            _offer(queue, _CAPTURE_DONE)


async def _dequeue(queue: asyncio.Queue[object]) -> AsyncIterator[Utterance]:
    """Yield queued utterances until the capture-done sentinel arrives."""
    while True:
        item = await queue.get()
        if item is _CAPTURE_DONE:
            return
        yield cast("Utterance", item)
