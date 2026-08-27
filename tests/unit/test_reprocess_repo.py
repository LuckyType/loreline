"""Tests for the reprocess-job repository (startup interruption sweep)."""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from pathlib import Path

import pytest_asyncio

from loreline.diarization.openai_diarizer import OpenAIDiarizer
from loreline.models import (
    DiarizationConfig,
    DiarizationMode,
    JobStatus,
    Protocol,
    ProviderConfig,
    ProviderKind,
    ReprocessJob,
    Session,
)
from loreline.persistence import (
    AudioStore,
    Database,
    GlossaryRepository,
    ProviderRepository,
    ReprocessRepository,
    SessionRepository,
    TranscriptRepository,
)
from loreline.reprocess.jobs import (
    ReprocessManager,
    _resolve_openai_key,  # pyright: ignore[reportPrivateUsage]
)
from loreline.secrets import SecretStore


@pytest_asyncio.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    database = Database(tmp_path / "test.db")
    await database.connect()
    try:
        yield database
    finally:
        await database.close()


async def test_mark_interrupted_fails_running_and_queued(db: Database) -> None:
    sessions = SessionRepository(db)
    reprocess = ReprocessRepository(db)
    await sessions.create(Session(id="s1", started_at=time.time()))
    for job_id, status in (
        ("j1", JobStatus.RUNNING),
        ("j2", JobStatus.QUEUED),
        ("j3", JobStatus.DONE),
    ):
        await reprocess.create(
            ReprocessJob(
                id=job_id, session_id="s1", provider_id="p", status=status, created_at=time.time()
            )
        )

    await reprocess.mark_interrupted()

    running = await reprocess.get("j1")
    queued = await reprocess.get("j2")
    done = await reprocess.get("j3")
    assert running is not None and running.status is JobStatus.ERROR
    assert queued is not None and queued.status is JobStatus.ERROR
    assert done is not None and done.status is JobStatus.DONE  # completed jobs untouched
    assert running.error == "interrupted by restart"


def test_resolve_openai_key_prefers_configured_provider(tmp_path: Path) -> None:
    secrets = SecretStore(tmp_path / "secrets.json")
    secrets.set("provider:oai", "sk-from-store")

    def provider(kind: ProviderKind, auth_ref: str | None) -> ProviderConfig:
        return ProviderConfig(
            id=kind.value, name=kind.value, kind=kind, protocol=Protocol.WS, auth_ref=auth_ref
        )

    providers = [
        provider(ProviderKind.DEEPGRAM, "provider:dg"),
        provider(ProviderKind.OPENAI, "provider:oai"),
    ]
    # Reuses the stored OpenAI provider key (so batch diarization needs no env var).
    assert _resolve_openai_key(providers, secrets) == "sk-from-store"
    # No OpenAI provider configured -> None (OpenAIDiarizer then falls back to the env var).
    assert _resolve_openai_key([provider(ProviderKind.DEEPGRAM, "provider:dg")], secrets) is None


async def test_build_diarizer_openai_mode_reuses_stored_key(db: Database, tmp_path: Path) -> None:
    """``_build_diarizer`` is shared by both the "diarize" and "transcribe" reprocess
    operations (see ``_transcribe_session``) - it must resolve the stored OpenAI
    provider key for either, not just the dedicated "diarize" op."""
    providers = ProviderRepository(db)
    secrets = SecretStore(tmp_path / "secrets.json")
    await providers.upsert(
        ProviderConfig(
            id="oai",
            name="OpenAI",
            kind=ProviderKind.OPENAI,
            protocol=Protocol.WS,
            auth_ref="provider:oai",
        )
    )
    secrets.set("provider:oai", "sk-from-store")

    def _unreachable_factory(_cfg: DiarizationConfig) -> object:
        msg = "OPENAI mode must not fall through to the generic diarizer_factory"
        raise AssertionError(msg)

    manager = ReprocessManager(
        providers=providers,
        glossaries=GlossaryRepository(db),
        sessions=SessionRepository(db),
        transcripts=TranscriptRepository(db),
        reprocess=ReprocessRepository(db),
        secrets=secrets,
        audio_store=AudioStore(tmp_path / "audio"),
        diarizer_factory=_unreachable_factory,  # pyright: ignore[reportArgumentType]
    )

    diarizer = await manager._build_diarizer(  # pyright: ignore[reportPrivateUsage]
        DiarizationConfig(mode=DiarizationMode.OPENAI)
    )
    assert isinstance(diarizer, OpenAIDiarizer)
    assert diarizer._api_key == "sk-from-store"  # pyright: ignore[reportPrivateUsage]
