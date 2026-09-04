"""Tests for the reprocess-job repository (startup interruption sweep)."""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from pathlib import Path

import pytest_asyncio

from loreline.models import JobStatus, ReprocessJob, Session
from loreline.persistence import Database, ReprocessRepository, SessionRepository


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


async def test_delete_version_takes_the_diarize_jobs_aimed_at_it(db: Database) -> None:
    """Deleting a transcript version drops its own job row and every diarize job
    that targeted it: a diarize job relabels one version in place, and its
    output is deleted with that version's base rows, so its row would describe
    work on a transcript nobody can reach. Other versions' jobs stay."""
    sessions = SessionRepository(db)
    reprocess = ReprocessRepository(db)
    await sessions.create(Session(id="s1", started_at=time.time()))

    def job(
        job_id: str, *, operation: str = "transcribe", target: str = "original"
    ) -> ReprocessJob:
        return ReprocessJob(
            id=job_id,
            session_id="s1",
            provider_id="p",
            operation=operation,
            target=target,
            status=JobStatus.DONE,
            created_at=time.time(),
        )

    await reprocess.create(job("v1"))
    await reprocess.create(job("v2"))
    await reprocess.create(job("d1", operation="diarize", target="v1"))
    await reprocess.create(job("d2", operation="diarize", target="v2"))
    await reprocess.create(job("d0", operation="diarize"))

    await reprocess.delete_version("s1", "v1")

    left = {j.id for j in await reprocess.for_session("s1")}
    assert left == {"v2", "d2", "d0"}
