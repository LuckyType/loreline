"""FastAPI application factory."""

from __future__ import annotations

import asyncio
import contextlib
import os
import threading
import time
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from fastapi import FastAPI

from loreline import __version__
from loreline.bus import EventBus
from loreline.logbus import LogBroadcaster
from loreline.logging import configure_logging, get_logger
from loreline.models import TranscriptEvent
from loreline.monitoring import AlertManager
from loreline.monitoring.alerts import ClientFactory
from loreline.persistence import (
    AudioStore,
    Database,
    GlossaryRepository,
    LogStore,
    ProviderRepository,
    ReprocessRepository,
    SessionRepository,
    SettingsRepository,
    TranscriptRepository,
    VideoRepository,
)
from loreline.reprocess import ReprocessManager
from loreline.secrets import SecretStore
from loreline.services import ServiceManager
from loreline.session import SessionManager
from loreline.session.manager import BackendFactory, CaptureFactory, DiarizerFactory
from loreline.session.recovery import recover_orphaned_indexes
from loreline.settings import Settings, get_settings
from loreline.staleness import warn_about_stale_favorites
from loreline.updater import Autostart, Updater
from loreline.updater.process import CommandRunner
from loreline.video import VideoManager, VideoStore
from loreline.video.client import ClientFactory as VideoClientFactory
from loreline.web.auth import LoginRateLimiter, ensure_jwt_secret
from loreline.web.routes import (
    audio,
    auth,
    capabilities,
    glossary,
    logs_ws,
    providers,
    reprocess,
    sessions,
    system,
    transcript_ws,
    video,
)
from loreline.web.spa import SpaStaticFiles, spa_directory


@dataclass(slots=True)
class AppState:
    """Shared application state attached to ``app.state``."""

    settings: Settings
    secrets: SecretStore
    db: Database
    providers: ProviderRepository
    glossaries: GlossaryRepository
    sessions: SessionRepository
    transcripts: TranscriptRepository
    reprocess_jobs: ReprocessRepository
    video_jobs: VideoRepository
    audio_store: AudioStore
    video_store: VideoStore
    log_store: LogStore
    manager: SessionManager
    reprocess: ReprocessManager
    video: VideoManager
    settings_repo: SettingsRepository
    alerts: AlertManager
    updater: Updater
    autostart: Autostart
    log_broadcaster: LogBroadcaster
    login_limiter: LoginRateLimiter
    services: ServiceManager
    started_at: float


def _build_state(
    settings: Settings,
    broadcaster: LogBroadcaster,
    log_store: LogStore,
    *,
    capture_factory: CaptureFactory | None,
    backend_factory: BackendFactory | None,
    diarizer_factory: DiarizerFactory | None,
    command_runner: CommandRunner | None,
    alert_client_factory: ClientFactory | None,
    video_client_factory: VideoClientFactory | None,
) -> AppState:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    db = Database(settings.db_path)
    secrets = SecretStore(settings.secrets_path)
    ensure_jwt_secret(settings, secrets)
    provider_repo = ProviderRepository(db)
    glossary_repo = GlossaryRepository(db)
    session_repo = SessionRepository(db)
    transcript_repo = TranscriptRepository(db)
    reprocess_repo = ReprocessRepository(db)
    video_repo = VideoRepository(db)
    audio_store = AudioStore(settings.audio_dir)
    video_store = VideoStore(settings.video_dir)
    settings_repo = SettingsRepository(db)
    alert_manager = AlertManager(
        settings=settings_repo, secrets=secrets, client_factory=alert_client_factory
    )
    updater = Updater(app_dir=settings.app_dir, unit=settings.systemd_unit, runner=command_runner)
    autostart = Autostart(unit=settings.systemd_unit, runner=command_runner)
    transcript_bus: EventBus[TranscriptEvent] = EventBus()
    manager = SessionManager(
        providers=provider_repo,
        glossaries=glossary_repo,
        sessions=session_repo,
        transcripts=transcript_repo,
        secrets=secrets,
        transcript_bus=transcript_bus,
        audio_store=audio_store,
        alerter=alert_manager,
        capture_factory=capture_factory,
        backend_factory=backend_factory,
        diarizer_factory=diarizer_factory,
    )
    reprocess_manager = ReprocessManager(
        providers=provider_repo,
        glossaries=glossary_repo,
        sessions=session_repo,
        transcripts=transcript_repo,
        reprocess=reprocess_repo,
        secrets=secrets,
        audio_store=audio_store,
        transcript_bus=transcript_bus,
        backend_factory=backend_factory,
        diarizer_factory=diarizer_factory,
    )
    video_manager = VideoManager(
        providers=provider_repo,
        sessions=session_repo,
        videos=video_repo,
        video_store=video_store,
        secrets=secrets,
        client_factory=video_client_factory,
    )
    return AppState(
        settings=settings,
        secrets=secrets,
        db=db,
        providers=provider_repo,
        glossaries=glossary_repo,
        sessions=session_repo,
        transcripts=transcript_repo,
        reprocess_jobs=reprocess_repo,
        video_jobs=video_repo,
        audio_store=audio_store,
        video_store=video_store,
        log_store=log_store,
        manager=manager,
        reprocess=reprocess_manager,
        video=video_manager,
        settings_repo=settings_repo,
        alerts=alert_manager,
        updater=updater,
        autostart=autostart,
        log_broadcaster=broadcaster,
        login_limiter=LoginRateLimiter(),
        services=ServiceManager(
            settings.docker_api,
            # Scope to this compose project so the UI can only ever see its
            # own stack, never other containers on the same host.
            project=os.environ.get("COMPOSE_PROJECT_NAME", "loreline"),
        ),
        started_at=time.monotonic(),
    )


async def _drain(task: asyncio.Task[None]) -> None:
    """Cancel a background task at shutdown and wait for it to notice.

    Whatever it raises on the way out is the shutdown path's business to
    ignore: the app is going down either way, and a cancelled courtesy check
    has nothing to report.
    """
    task.cancel()
    with contextlib.suppress(Exception, asyncio.CancelledError):
        await task


async def _warn_stale_favorites(state: AppState, settings: Settings) -> None:
    """Startup favourites check, wrapped so nothing it does can escape.

    ``warn_about_stale_favorites`` already swallows its own failures; this adds
    the database read to the same guarantee, because a background task that
    raises would log a traceback at every boot for a warning nobody asked for.
    """
    if not settings.check_favorite_models:
        return
    with contextlib.suppress(Exception):
        await warn_about_stale_favorites(await state.providers.list(), secrets=state.secrets)


def create_app(
    settings: Settings | None = None,
    *,
    capture_factory: CaptureFactory | None = None,
    backend_factory: BackendFactory | None = None,
    diarizer_factory: DiarizerFactory | None = None,
    command_runner: CommandRunner | None = None,
    alert_client_factory: ClientFactory | None = None,
    video_client_factory: VideoClientFactory | None = None,
) -> FastAPI:
    """Create and configure the Loreline FastAPI app.

    The ``*_factory`` overrides let tests run the session pipeline without audio
    hardware or live STT endpoints.
    """
    settings = settings or get_settings()
    broadcaster = LogBroadcaster()
    log_store = LogStore(settings.logs_dir)
    configure_logging(
        level=settings.log_level,
        json_logs=settings.log_json,
        broadcaster=broadcaster,
        log_store=log_store,
    )
    log = get_logger(__name__)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
        state = _build_state(
            settings,
            broadcaster,
            log_store,
            capture_factory=capture_factory,
            backend_factory=backend_factory,
            diarizer_factory=diarizer_factory,
            command_runner=command_runner,
            alert_client_factory=alert_client_factory,
            video_client_factory=video_client_factory,
        )
        await state.db.connect()
        await state.sessions.mark_interrupted()
        await state.reprocess.reconcile()
        await state.video.reconcile()
        # Retention for the per-version log files. Deleting a session takes its
        # logs with its audio, so this only finds what an interrupted delete or
        # a pre-log-store build left behind - which is exactly what nothing
        # else would ever clean up. Off the event loop: it walks a directory.
        known = {session.id for session in await state.sessions.list()}
        pruned = await asyncio.to_thread(state.log_store.prune, known)
        if pruned:
            log.info("logs.pruned", sessions=pruned)
        # Rebuild index sidecars for recordings a dead process left behind, in
        # the background - a multi-hour WAV takes minutes of VAD, and startup
        # must not wait on it. The abort event lets shutdown cut it short
        # (nothing partial is written; the sweep re-runs next boot).
        recovery_abort = threading.Event()
        recovery_task = asyncio.create_task(
            recover_orphaned_indexes(
                audio_store=state.audio_store,
                sessions=state.sessions,
                alerter=state.alerts,
                abort=recovery_abort,
                active_session_id=state.manager.current_session_id,
            )
        )
        # Tell the GM when a model they favourited has been retired or has
        # vanished from its vendor's catalogue. In the background and never on
        # the critical path: it talks to vendor APIs over the network, so a
        # slow or unreachable vendor must not be able to delay a boot, and it
        # is scoped to favourites because warning about the whole curated
        # catalogue at every startup is noise nobody reads.
        staleness_task = asyncio.create_task(_warn_stale_favorites(state, settings))
        app.state.ctx = state
        log.info("loreline.startup", version=__version__, environment=settings.environment)
        try:
            yield
        finally:
            recovery_abort.set()
            await _drain(staleness_task)
            with contextlib.suppress(Exception):
                await recovery_task
            await state.manager.stop()
            await state.reprocess.aclose()
            await state.video.aclose()
            await state.db.close()
            log.info("loreline.shutdown")

    app = FastAPI(
        title="Loreline",
        version=__version__,
        summary="Tabletop session transcriber - capture + STT orchestration.",
        lifespan=lifespan,
    )

    app.include_router(system.router)
    app.include_router(capabilities.router)
    app.include_router(auth.router)
    app.include_router(audio.router)
    app.include_router(providers.router)
    app.include_router(glossary.router)
    app.include_router(sessions.router)
    app.include_router(reprocess.router)
    app.include_router(video.router)
    app.include_router(transcript_ws.router)
    app.include_router(logs_ws.router)

    spa = spa_directory()
    if spa is not None:
        app.mount("/", SpaStaticFiles(directory=spa, html=True), name="spa")
    return app
