"""Tests for the persistence layer."""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from pathlib import Path

import pytest_asyncio

from loreline.models import (
    DEFAULT_GLOSSARY_CAMPAIGN,
    Glossary,
    OpenRouterRouting,
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
from loreline.persistence.database import MIGRATIONS
from loreline.web.deps import ACTION_DEFAULTS_KEY
from loreline.web.schemas import ActionDefaults

# Migration list indices (0-based) for the scripts exercised directly below.
_V_DROP_GOOGLE = 10  # v11: delete rows of the removed Google STT v2 kind
_V_MERGE_KINDS = 11  # v12: fold openrouter_stt / openai_chat onto merged kinds
_V_DROP_VOSK = 14  # v15: delete rows of the removed vosk kind and what named them


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
        assert row[0] == 15


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


async def test_migration_removes_rows_of_the_dropped_google_kind(tmp_path: Path) -> None:
    """A provider row whose kind no longer exists in ``ProviderKind`` cannot be
    deserialized, so dropping the Google STT v2 (gRPC) kind had to take its rows
    with it - otherwise every provider read on an existing install would raise.
    """
    path = tmp_path / "legacy.db"
    async with Database(path) as database:
        conn = database.connection
        await conn.execute(
            """
            INSERT INTO providers
                (id, name, kind, base_url, auth_ref, protocol, model, sample_rate,
                 language, capabilities, enabled)
            VALUES ('g1', 'Google STT v2', 'google', 'my-project', NULL, 'grpc',
                    'chirp_2', 16000, 'de', '{}', 1);
            """
        )
        # Rewind past the removal migration so re-connecting replays it, the
        # way an upgrade of an install that already had this provider does.
        # Run just the migration under test, rather than rewinding
        # schema_version and reconnecting: that would replay every later
        # migration too, and an ALTER TABLE cannot be applied twice.
        await conn.executescript(MIGRATIONS[_V_DROP_GOOGLE])
        await conn.commit()

        async with conn.execute("SELECT COUNT(*) FROM providers;") as cur:
            row = await cur.fetchone()
        assert row is not None
        assert row[0] == 0, "the unloadable google row survived the migration"
        # And the repository can read the table again without raising.
        assert await ProviderRepository(database).list() == []


async def test_migration_removes_the_dropped_vosk_kind_and_its_references(
    tmp_path: Path,
) -> None:
    """The vosk kind never had a connector, so it is removed rather than fixed.

    Its provider rows have to go for the same reason the google ones did in
    v11: ProviderKind has no "vosk" member any more, so a survivor would raise
    on every provider read. What v11 did not have to consider is the rows that
    name a provider id - a picker default, jobs, sessions - and those must not
    be left pointing at an id that no longer resolves. Nothing of value is
    lost: with no backend registered, every use of such a provider failed
    before any audio was transcribed.
    """
    path = tmp_path / "legacy-vosk.db"
    async with Database(path) as database:
        conn = database.connection
        await conn.execute(
            """
            INSERT INTO providers
                (id, name, kind, base_url, auth_ref, protocol, model, sample_rate,
                 language, capabilities, enabled)
            VALUES ('v1', 'Vosk server', 'vosk', 'ws://localhost:2700', NULL, 'ws',
                    NULL, 16000, 'de', '{}', 1);
            """
        )
        await conn.execute(
            """
            INSERT INTO sessions
                (id, status, started_at, started_mono, campaign_id, primary_provider,
                 fallback_provider, summary_provider, diarization, speaker_names)
            VALUES ('s1', 'idle', 1.0, 0.0, 'camp1', 'v1', 'v1', 'v1', '{}', '{}');
            """
        )
        await conn.execute(
            """
            INSERT INTO reprocess_jobs
                (id, session_id, provider_id, diarization, status, created_at)
            VALUES ('j1', 's1', 'v1', '{}', 'error', 1.0);
            """
        )
        await conn.execute(
            """
            INSERT INTO transcript_segments
                (session_id, source, text, speaker, start_ts, end_ts, is_final,
                 words, created_at)
            VALUES ('s1', 'reprocess:j1', 'orphan', NULL, 0.0, 1.0, 1, '[]', 1.0);
            """
        )
        await conn.execute(
            """
            INSERT INTO video_jobs
                (id, session_id, provider_id, model, prompt, status, created_at)
            VALUES ('vj1', 's1', 'v1', 'm', 'p', 'error', 1.0);
            """
        )
        await conn.execute(
            "INSERT INTO kv_settings (key, value) VALUES (?, ?);",
            (
                ACTION_DEFAULTS_KEY,
                ActionDefaults(stt_provider="v1", stt_model="x").model_dump_json(),
            ),
        )
        # Apply only the migration under test: rewinding schema_version and
        # reconnecting would replay every later migration too, and an
        # ALTER TABLE cannot be applied twice.
        await conn.executescript(MIGRATIONS[_V_DROP_VOSK])
        await conn.commit()

        assert await ProviderRepository(database).list() == []
        for table in ("providers", "reprocess_jobs", "video_jobs", "transcript_segments"):
            async with conn.execute(f"SELECT COUNT(*) FROM {table};") as cur:
                row = await cur.fetchone()
            assert row is not None
            assert row[0] == 0, f"a row referencing the dropped vosk kind survived in {table}"

        stored = await SessionRepository(database).get("s1")
        assert stored is not None
        assert stored.primary_provider is None
        assert stored.fallback_provider is None
        assert stored.summary_provider is None

        async with conn.execute(
            "SELECT value FROM kv_settings WHERE key = ?;", (ACTION_DEFAULTS_KEY,)
        ) as cur:
            row = await cur.fetchone()
        assert row is not None
        defaults = ActionDefaults.model_validate_json(row[0])
        assert defaults.stt_provider == ""
        assert defaults.stt_model == "x", "only the provider reference is cleared"


async def test_migration_folds_retired_kinds_onto_the_merged_ones(tmp_path: Path) -> None:
    """Provider kinds are one-per-vendor now, with capabilities declared per
    kind. Stored rows naming a retired kind must be converted, not orphaned:
    ProviderKind no longer has them, so a survivor would raise on every read.

    `openai_chat` splits by where it pointed. No base_url meant OpenAI's own
    API; a base_url meant a self-hosted OpenAI-compatible server, which is what
    `openai_compat` covers.
    """
    path = tmp_path / "legacy-kinds.db"
    rows = [
        ("a", "openrouter_stt", None, "openrouter", None),
        ("b", "openai_chat", None, "openai", None),
        ("c", "openai_chat", "http://ollama:11434/v1", "openai_compat", "http://ollama:11434/v1"),
    ]
    async with Database(path) as database:
        conn = database.connection
        for pid, kind, base_url, _, _ in rows:
            await conn.execute(
                """
                INSERT INTO providers
                    (id, name, kind, base_url, auth_ref, protocol, model, sample_rate,
                     language, capabilities, enabled)
                VALUES (?, ?, ?, ?, NULL, 'http_batch', NULL, 16000, 'de', '{}', 1);
                """,
                (pid, pid, kind, base_url),
            )
        # As above: apply only the migration under test.
        await conn.executescript(MIGRATIONS[_V_MERGE_KINDS])
        await conn.commit()

        stored = {p.id: p for p in await ProviderRepository(database).list()}
        assert len(stored) == len(rows)
        for pid, _, _, expected_kind, expected_url in rows:
            assert stored[pid].kind.value == expected_kind
            assert stored[pid].base_url == expected_url


async def test_provider_routing_survives_a_round_trip(db: Database) -> None:
    """OpenRouter routing preferences must actually persist.

    They did not: the API accepted `routing`, the settings UI wrote it, and the
    repository dropped it on the floor because the column did not exist, so
    every request went out with OpenRouter's default routing no matter what the
    GM picked. The unit tests covered routing_payload() on an in-memory config
    and never crossed the persistence seam, which is exactly where it broke.
    """
    repo = ProviderRepository(db)
    await repo.upsert(
        ProviderConfig(
            id="or1",
            name="OpenRouter",
            kind=ProviderKind.OPENROUTER,
            protocol=Protocol.HTTP_BATCH,
            routing=OpenRouterRouting(sort="price", data_collection="deny", zdr=True),
        )
    )
    stored = await repo.get("or1")
    assert stored is not None
    assert stored.routing is not None
    assert (stored.routing.sort, stored.routing.data_collection, stored.routing.zdr) == (
        "price",
        "deny",
        True,
    )

    # A provider that never configured routing keeps None, which is what the
    # request builder reads as "send no routing object at all".
    await repo.upsert(
        ProviderConfig(id="dg1", name="Deepgram", kind=ProviderKind.DEEPGRAM, protocol=Protocol.WS)
    )
    plain = await repo.get("dg1")
    assert plain is not None
    assert plain.routing is None
