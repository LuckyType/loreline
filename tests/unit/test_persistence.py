"""Tests for the persistence layer."""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from pathlib import Path

import pytest_asyncio

from loreline.models import (
    DEFAULT_GLOSSARY_CAMPAIGN,
    Glossary,
    Protocol,
    ProviderCaps,
    ProviderConfig,
    ProviderKind,
    Session,
    SessionStatus,
    TranscriptEvent,
    Word,
)
from loreline.persistence import (
    Database,
    GlossaryRepository,
    ProviderRepository,
    SessionRepository,
    TranscriptRepository,
)


@pytest_asyncio.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    database = Database(tmp_path / "test.db")
    await database.connect()
    try:
        yield database
    finally:
        await database.close()


async def test_migrations_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "m.db"
    async with Database(path):
        pass
    # Re-open: migrations must not re-run or error.
    async with Database(path) as database:
        async with database.connection.execute("SELECT MAX(version) FROM schema_version;") as cur:
            row = await cur.fetchone()
        assert row is not None
        assert row[0] == 9


async def test_glossary_get_effective_merges_default_and_campaign(db: Database) -> None:
    repo = GlossaryRepository(db)
    await repo.put(Glossary(campaign_id=DEFAULT_GLOSSARY_CAMPAIGN, terms=["Aurora", "Mistwood"]))
    await repo.put(Glossary(campaign_id="camp1", terms=["Drizzt", "Mistwood"]))

    effective = await repo.get_effective("camp1")
    assert effective is not None
    assert effective.terms == ["Aurora", "Mistwood", "Drizzt"]  # default first, campaign deduped

    default_only = await repo.get_effective(None)
    assert default_only is not None
    assert default_only.terms == ["Aurora", "Mistwood"]

    await repo.put(Glossary(campaign_id=DEFAULT_GLOSSARY_CAMPAIGN, terms=[]))
    assert await repo.get_effective(None) is None


async def test_session_speaker_names_roundtrip(db: Database) -> None:
    repo = SessionRepository(db)
    await repo.create(Session(id="s1", started_at=1.0))
    fresh = await repo.get("s1")
    assert fresh is not None and fresh.speaker_names == {}  # default empty map

    await repo.set_speaker_names("s1", {"Speaker A": "GM", "Speaker B": "Player"})
    loaded = await repo.get("s1")
    assert loaded is not None
    assert loaded.speaker_names == {"Speaker A": "GM", "Speaker B": "Player"}


async def test_session_summary_roundtrip(db: Database) -> None:
    repo = SessionRepository(db)
    await repo.create(Session(id="s1", started_at=1.0))
    fresh = await repo.get("s1")
    assert fresh is not None and fresh.summary is None  # default null

    await repo.set_summary(
        "s1", "The party fought a dragon.", provider_id="llm-1", model="gpt-4o-mini"
    )
    loaded = await repo.get("s1")
    assert loaded is not None
    assert loaded.summary == "The party fought a dragon."
    assert loaded.summary_provider == "llm-1"
    assert loaded.summary_model == "gpt-4o-mini"


async def test_mark_interrupted_fails_stuck_capturing_sessions(db: Database) -> None:
    repo = SessionRepository(db)
    await repo.create(Session(id="crashed", status=SessionStatus.CAPTURING, started_at=1.0))
    await repo.create(Session(id="done", status=SessionStatus.CAPTURING, started_at=2.0))
    await repo.finish("done", SessionStatus.COMPLETED)

    await repo.mark_interrupted()

    crashed = await repo.get("crashed")
    done = await repo.get("done")
    assert crashed is not None
    assert crashed.status is SessionStatus.ERROR
    assert crashed.ended_at is not None  # no longer looks like it's still recording
    assert done is not None
    assert done.status is SessionStatus.COMPLETED  # already-finished sessions untouched


async def test_provider_roundtrip(db: Database) -> None:
    repo = ProviderRepository(db)
    provider = ProviderConfig(
        id="dg1",
        name="Deepgram Main",
        kind=ProviderKind.DEEPGRAM,
        base_url=None,
        auth_ref="deepgram",
        protocol=Protocol.WS,
        model="nova-3",
        capabilities=ProviderCaps(streaming=True, inline_diarization=True, vocab_param="keyterm"),
    )
    await repo.upsert(provider)

    fetched = await repo.get("dg1")
    assert fetched == provider

    provider.name = "Deepgram Renamed"
    await repo.upsert(provider)
    assert (await repo.get("dg1")) is not None
    assert (await repo.get("dg1")).name == "Deepgram Renamed"  # type: ignore[union-attr]
    assert len(await repo.list()) == 1

    await repo.delete("dg1")
    assert await repo.get("dg1") is None


async def test_glossary_roundtrip(db: Database) -> None:
    repo = GlossaryRepository(db)
    assert (await repo.get("camp1")).terms == []
    await repo.put(Glossary(campaign_id="camp1", terms=["Fireball", "Tasha", "Neverwinter"]))
    assert (await repo.get("camp1")).terms == ["Fireball", "Tasha", "Neverwinter"]


async def test_session_and_transcript(db: Database) -> None:
    sessions = SessionRepository(db)
    transcripts = TranscriptRepository(db)

    session = Session(id="s1", started_at=time.time(), primary_provider="dg1")
    await sessions.create(session)

    await transcripts.add(
        TranscriptEvent(
            session_id="s1",
            source="dg1",
            text="Du betrittst den Raum.",
            words=[Word(text="Du", start=0.0, end=0.2, speaker="SPEAKER_0")],
            speaker="SPEAKER_0",
            start_ts=0.0,
            end_ts=1.5,
            is_final=True,
        )
    )

    events = await transcripts.for_session("s1")
    assert len(events) == 1
    assert events[0].text == "Du betrittst den Raum."
    assert events[0].words[0].speaker == "SPEAKER_0"

    await sessions.finish("s1", SessionStatus.IDLE)
    finished = await sessions.get("s1")
    assert finished is not None
    assert finished.ended_at is not None
