"""FastAPI application factory."""

from __future__ import annotations

import os
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
    ProviderRepository,
    ReprocessRepository,
    SessionRepository,
    SettingsRepository,
    TranscriptRepository,
)
from loreline.reprocess import ReprocessManager
from loreline.secrets import SecretStore
from loreline.services import ServiceManager
from loreline.session import SessionManager
from loreline.session.manager import BackendFactory, CaptureFactory, DiarizerFactory
from loreline.settings import Settings, get_settings
from loreline.updater import Autostart, Updater
from loreline.updater.process import CommandRunner
from loreline.web.auth import LoginRateLimiter, ensure_jwt_secret
from loreline.web.routes import (
    audio,
    auth,
    glossary,
    logs_ws,
    providers,
    reprocess,
    sessions,
    system,
    transcript_ws,
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
    audio_store: AudioStore
    manager: SessionManager
    reprocess: ReprocessManager
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
    *,
    capture_factory: CaptureFactory | None,
    backend_factory: BackendFactory | None,
    diarizer_factory: DiarizerFactory | None,
    command_runner: CommandRunner | None,
    alert_client_factory: ClientFactory | None,
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
    audio_store = AudioStore(settings.audio_dir)
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
        backend_factory=backend_factory,
        diarizer_factory=diarizer_factory,
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
        audio_store=audio_store,
        manager=manager,
        reprocess=reprocess_manager,
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


def create_app(
    settings: Settings | None = None,
    *,
    capture_factory: CaptureFactory | None = None,
    backend_factory: BackendFactory | None = None,
    diarizer_factory: DiarizerFactory | None = None,
    command_runner: CommandRunner | None = None,
    alert_client_factory: ClientFactory | None = None,
) -> FastAPI:
    """Create and configure the Loreline FastAPI app.

    The ``*_factory`` overrides let tests run the session pipeline without audio
    hardware or live STT endpoints.
    """
    settings = settings or get_settings()
    broadcaster = LogBroadcaster()
    configure_logging(
        level=settings.log_level, json_logs=settings.log_json, broadcaster=broadcaster
    )
    log = get_logger(__name__)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
        state = _build_state(
            settings,
            broadcaster,
            capture_factory=capture_factory,
            backend_factory=backend_factory,
            diarizer_factory=diarizer_factory,
            command_runner=command_runner,
            alert_client_factory=alert_client_factory,
        )
        await state.db.connect()
        await state.sessions.mark_interrupted()
        await state.reprocess.reconcile()
        app.state.ctx = state
        log.info("loreline.startup", version=__version__, environment=settings.environment)
        try:
            yield
        finally:
            await state.manager.stop()
            await state.reprocess.aclose()
            await state.db.close()
            log.info("loreline.shutdown")

    app = FastAPI(
        title="Loreline",
        version=__version__,
        summary="Tabletop session transcriber - capture + STT orchestration.",
        lifespan=lifespan,
    )

    app.include_router(system.router)
    app.include_router(auth.router)
    app.include_router(audio.router)
    app.include_router(providers.router)
    app.include_router(glossary.router)
    app.include_router(sessions.router)
    app.include_router(reprocess.router)
    app.include_router(transcript_ws.router)
    app.include_router(logs_ws.router)

    spa = spa_directory()
    if spa is not None:
        app.mount("/", SpaStaticFiles(directory=spa, html=True), name="spa")
    return app
