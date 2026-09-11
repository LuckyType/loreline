"""Tests for the persistence layer."""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from pathlib import Path

import pytest_asyncio

from loreline.models import (
    DEFAULT_GLOSSARY_CAMPAIGN,
    Glossary,
    JobStatus,
    OpenRouterRouting,
    ProviderConfig,
    ProviderKind,
    ReprocessJob,
    Session,
    SessionOrigin,
    SessionStatus,
    TranscriptEvent,
    VideoJob,
    Word,
)
from loreline.persistence import (
    Database,
    GlossaryRepository,
    ProviderRepository,
    ReprocessRepository,
    SessionRepository,
    TranscriptRepository,
    VideoRepository,
)
from loreline.persistence.database import MIGRATIONS
from loreline.web.deps import ACTION_DEFAULTS_KEY
from loreline.web.schemas import ActionDefaults

# Migration list indices (0-based) for the scripts exercised directly below.
_V_DROP_GOOGLE = 10  # v11: delete rows of the removed Google STT v2 kind
_V_MERGE_KINDS = 11  # v12: fold openrouter_stt / openai_chat onto merged kinds
_V_DROP_VOSK = 14  # v15: delete rows of the removed vosk kind and what named them
_V_DROP_PROVIDER_MODEL = 15  # v16: drop providers.model, chosen per request now
_V_DROP_PROVIDER_WIRE = 16  # v17: drop providers.protocol and providers.capabilities
_V_HAS_SPEAKERS = 20  # v21: reprocess_jobs.has_speakers, backfilled from each job's rows
_V_VIDEO_SCENE = 24  # v25: video_jobs.scene_model / scene_source, the converted prompt's record


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
        # Read from the list rather than restated: the number changes with
        # every migration, and this test is about re-running, not counting.
        assert row[0] == len(MIGRATIONS)


async def test_glossary_get_effective_merges_default_and_campaign(db: Database) -> None:
    repo = GlossaryRepository(db)
    await repo.put(Glossary(campaign_id=DEFAULT_GLOSSARY_CAMPAIGN, terms=["Aurora", "Mistwood"]))
    await repo.put(Glossary(campaign_id="camp1", terms=["Drizzt", "Mistwood"]))

    effective = await repo.get_effective("camp1")
    assert effective is not None
    # The campaign's own terms first, then the default list, deduped: the
    # ceiling is spent from the head, and a term on this campaign is likelier
    # to be in this audio than one on the list every table shares.
    assert effective.terms == ["Drizzt", "Mistwood", "Aurora"]

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


async def test_session_origin_roundtrip(db: Database) -> None:
    """A capture says nothing about its origin; an import says both things."""
    repo = SessionRepository(db)
    await repo.create(Session(id="captured", started_at=1.0))
    captured = await repo.get("captured")
    assert captured is not None
    assert captured.origin is SessionOrigin.CAPTURE  # the v22 default
    assert captured.import_name is None  # a capture has no file name at all

    await repo.create(
        Session(
            id="imported",
            started_at=2.0,
            origin=SessionOrigin.IMPORT,
            import_name="session 12 - the vault.m4a",
        )
    )
    loaded = await repo.get("imported")
    assert loaded is not None
    assert loaded.origin is SessionOrigin.IMPORT
    assert loaded.import_name == "session 12 - the vault.m4a"
    # And through the list, which is what the history page reads.
    listed = {s.id: s.origin for s in await repo.list()}
    assert listed == {"captured": SessionOrigin.CAPTURE, "imported": SessionOrigin.IMPORT}


async def test_session_summary_roundtrip(db: Database) -> None:
    repo = SessionRepository(db)
    await repo.create(Session(id="s1", started_at=1.0))
    fresh = await repo.get("s1")
    assert fresh is not None and fresh.summary is None  # default null

    await repo.set_summary(
        "s1",
        "The party fought a dragon.",
        provider_id="llm-1",
        model="gpt-4o-mini",
        version="2680abb4",
    )
    loaded = await repo.get("s1")
    assert loaded is not None
    assert loaded.summary == "The party fought a dragon."
    assert loaded.summary_provider == "llm-1"
    assert loaded.summary_model == "gpt-4o-mini"
    # The version belongs with the provider and the model: the same model over
    # two versions of one session writes two different summaries.
    assert loaded.summary_version == "2680abb4"


async def test_mark_interrupted_fails_stuck_capturing_sessions(db: Database) -> None:
    repo = SessionRepository(db)
    transcripts = TranscriptRepository(db)
    await repo.create(Session(id="crashed", status=SessionStatus.CAPTURING, started_at=1.0))
    await repo.create(Session(id="done", status=SessionStatus.CAPTURING, started_at=2.0))
    await repo.finish("done", SessionStatus.COMPLETED)

    await repo.mark_interrupted(transcripts)

    crashed = await repo.get("crashed")
    done = await repo.get("done")
    assert crashed is not None
    assert crashed.status is SessionStatus.ERROR
    assert crashed.ended_at is not None  # no longer looks like it's still recording
    assert done is not None
    assert done.status is SessionStatus.COMPLETED  # already-finished sessions untouched


async def test_mark_interrupted_sweeps_leftover_interim_rows(db: Database) -> None:
    """A process killed mid-turn can leave an unreplaced interim behind.

    ``TranscriptRepository.delete_interims`` otherwise only runs from
    ``SessionManager._finish``, which a killed process never reaches, so the
    startup sweep has to run it for every session it is about to fail here -
    or a reloaded history shows the half-typed line as settled text forever.
    """
    sessions = SessionRepository(db)
    transcripts = TranscriptRepository(db)
    await sessions.create(Session(id="crashed", status=SessionStatus.CAPTURING, started_at=1.0))
    await sessions.create(Session(id="finished", status=SessionStatus.CAPTURING, started_at=2.0))
    await sessions.finish("finished", SessionStatus.COMPLETED)

    for session_id in ("crashed", "finished"):
        await transcripts.add(
            TranscriptEvent(
                session_id=session_id,
                source="oai",
                text="half a sen",
                start_ts=1.0,
                end_ts=1.5,
                is_final=False,
                turn_id="oai:1:item_001",
            )
        )

    await sessions.mark_interrupted(transcripts)

    assert await transcripts.for_session("crashed") == []
    # A session that had already ended cleanly keeps its own interim - the
    # sweep only touches sessions it is actually failing right now, not every
    # interim row in the table.
    assert len(await transcripts.for_session("finished")) == 1


async def test_provider_roundtrip(db: Database) -> None:
    repo = ProviderRepository(db)
    provider = ProviderConfig(
        id="dg1",
        name="Deepgram Main",
        kind=ProviderKind.DEEPGRAM,
        base_url=None,
        auth_ref="deepgram",
        favorite_models=["nova-3"],
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


async def test_a_turn_is_one_row_however_many_revisions_it_has(db: Database) -> None:
    """The streaming path publishes a turn as a growing interim and then a final.

    All of them carry one ``turn_id``, and the table must hold one row per turn
    rather than one per revision: otherwise a session reloaded mid-capture, and
    every export of it afterwards, reads the same sentence once per word.
    """
    sessions = SessionRepository(db)
    transcripts = TranscriptRepository(db)
    await sessions.create(Session(id="s1", started_at=time.time()))

    for text, final in (("the", False), ("the goblin", False), ("the goblin runs", True)):
        await transcripts.add(
            TranscriptEvent(
                session_id="s1",
                source="oai",
                text=text,
                start_ts=1.0,
                end_ts=2.0 if final else 1.5,
                is_final=final,
                turn_id="oai:1:item_001",
            )
        )

    events = await transcripts.for_session("s1")
    assert len(events) == 1
    assert events[0].text == "the goblin runs"
    assert events[0].is_final
    assert events[0].end_ts == 2.0
    assert events[0].turn_id == "oai:1:item_001"


async def test_two_turns_are_two_rows_and_the_utterance_path_still_appends(
    db: Database,
) -> None:
    """The upsert must not collapse anything it was not asked to.

    Two turns differ by their key, and every row the utterance path writes has
    no key at all, which SQLite counts as distinct from every other NULL. So a
    session that failed over from streaming to utterances keeps both halves.
    """
    sessions = SessionRepository(db)
    transcripts = TranscriptRepository(db)
    await sessions.create(Session(id="s1", started_at=time.time()))

    for turn in ("oai:1:item_001", "oai:1:item_002"):
        await transcripts.add(
            TranscriptEvent(
                session_id="s1",
                source="oai",
                text=turn,
                start_ts=1.0,
                end_ts=2.0,
                is_final=True,
                turn_id=turn,
            )
        )
    for _ in range(3):
        await transcripts.add(
            TranscriptEvent(
                session_id="s1",
                source="dg1",
                text="same text, same span",
                start_ts=5.0,
                end_ts=6.0,
                is_final=True,
            )
        )

    assert len(await transcripts.for_session("s1")) == 5


async def test_an_interim_never_survives_the_session_that_made_it(db: Database) -> None:
    """The ending that cannot settle anything: the connector died mid-turn."""
    sessions = SessionRepository(db)
    transcripts = TranscriptRepository(db)
    await sessions.create(Session(id="s1", started_at=time.time()))
    await transcripts.add(
        TranscriptEvent(
            session_id="s1",
            source="oai",
            text="half a sen",
            start_ts=1.0,
            end_ts=1.5,
            is_final=False,
            turn_id="oai:1:item_001",
        )
    )
    await transcripts.add(
        TranscriptEvent(
            session_id="s1",
            source="oai",
            text="a settled one",
            start_ts=3.0,
            end_ts=4.0,
            is_final=True,
            turn_id="oai:1:item_002",
        )
    )

    await transcripts.delete_interims("s1")

    events = await transcripts.for_session("s1")
    assert [e.text for e in events] == ["a settled one"]


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
                (id, name, kind, base_url, auth_ref, sample_rate, language, enabled)
            VALUES ('g1', 'Google STT v2', 'google', 'my-project', NULL, 16000, 'de', 1);
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
                (id, name, kind, base_url, auth_ref, sample_rate, language, enabled)
            VALUES ('v1', 'Vosk server', 'vosk', 'ws://localhost:2700', NULL, 16000, 'de', 1);
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
                    (id, name, kind, base_url, auth_ref, sample_rate, language, enabled)
                VALUES (?, ?, ?, ?, NULL, 16000, 'de', 1);
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


async def test_migration_drops_the_provider_model_column(tmp_path: Path) -> None:
    """A provider row cannot hold one model, because it serves several roles.

    An OpenRouter row transcribes, summarizes and generates video, so a single
    stored model was wrong for at least two of them - and it was consulted
    *before* the per-action defaults that do this properly, so it shadowed
    them. The column goes, and the values with it: nothing records which of the
    row's roles the value was meant for, so there is nowhere honest to migrate
    it to. Nothing is lost but a pre-selection.
    """
    path = tmp_path / "legacy-model.db"
    async with Database(path) as database:
        conn = database.connection
        # Put the column back to stand in for a pre-v16 database, then run only
        # the migration under test: rewinding schema_version and reconnecting
        # would replay every later migration too, and an ALTER TABLE cannot be
        # applied twice.
        await conn.execute("ALTER TABLE providers ADD COLUMN model TEXT;")
        await conn.execute(
            """
            INSERT INTO providers
                (id, name, kind, base_url, auth_ref, model, sample_rate,
                 language, enabled, favorite_models)
            VALUES ('or1', 'OpenRouter', 'openrouter', NULL, NULL,
                    'openai/gpt-4o-mini', 16000, 'de', 1, '["deepgram/nova-3"]');
            """
        )
        await conn.executescript(MIGRATIONS[_V_DROP_PROVIDER_MODEL])
        await conn.commit()

        async with conn.execute("SELECT * FROM providers;") as cur:
            row = await cur.fetchone()
        assert row is not None
        assert "model" not in set(row.keys()), "the column survived the migration"

        # The row still loads, and keeps everything that was not the model.
        stored = await ProviderRepository(database).get("or1")
        assert stored is not None
        assert stored.name == "OpenRouter"
        assert stored.favorite_models == ["deepgram/nova-3"]


async def test_migration_drops_the_unread_protocol_and_capabilities_columns(
    tmp_path: Path,
) -> None:
    """A v16 database still carries `protocol` and `capabilities` on every
    provider row. Nothing read either: the transport follows the chosen model
    and what a kind can do is in capabilities.yaml. v17 drops both, and the row
    reads back with everything else intact."""
    path = tmp_path / "legacy-wire.db"
    async with Database(path) as database:
        conn = database.connection
        # Put the columns back to stand in for a v16 database, then run only
        # the migration under test (see the v16 test above for why).
        await conn.execute("ALTER TABLE providers ADD COLUMN protocol TEXT NOT NULL DEFAULT '';")
        await conn.execute(
            "ALTER TABLE providers ADD COLUMN capabilities TEXT NOT NULL DEFAULT '{}';"
        )
        await conn.execute(
            """
            INSERT INTO providers
                (id, name, kind, base_url, auth_ref, protocol, sample_rate, language,
                 capabilities, enabled, favorite_models, routing)
            VALUES ('dg1', 'Deepgram Main', 'deepgram', 'wss://dg.example', 'provider:dg1',
                    'ws', 16000, 'en', '{"streaming": true, "inline_diarization": true}',
                    0, '["nova-3"]', NULL);
            """
        )
        await conn.executescript(MIGRATIONS[_V_DROP_PROVIDER_WIRE])
        await conn.commit()

        async with conn.execute("SELECT * FROM providers;") as cur:
            row = await cur.fetchone()
        assert row is not None
        assert {"protocol", "capabilities"}.isdisjoint(row.keys()), "a column survived"

        stored = await ProviderRepository(database).get("dg1")
        assert stored == ProviderConfig(
            id="dg1",
            name="Deepgram Main",
            kind=ProviderKind.DEEPGRAM,
            base_url="wss://dg.example",
            auth_ref="provider:dg1",
            favorite_models=["nova-3"],
            sample_rate=16000,
            language="en",
            enabled=False,
        )


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
    await repo.upsert(ProviderConfig(id="dg1", name="Deepgram", kind=ProviderKind.DEEPGRAM))
    plain = await repo.get("dg1")
    assert plain is not None
    assert plain.routing is None


async def test_migration_backfills_has_speakers_from_the_rows_each_job_wrote(
    tmp_path: Path,
) -> None:
    """A job row that predates the flag says what its rows say.

    The version list reads ``has_speakers`` off the row to call a version
    diarized without loading it, so a column that defaulted to 0 for every
    existing job would have put "Not diarized" back on exactly the versions
    the flag was added for. The backfill reads each job's own rows: a
    re-transcription's are tagged with its id, a relabeling's with its target.
    """
    path = tmp_path / "legacy-speakers.db"
    async with Database(path) as database:
        conn = database.connection
        sessions = SessionRepository(database)
        transcripts = TranscriptRepository(database)
        reprocess = ReprocessRepository(database)
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

        def row(source: str, speaker: str | None) -> TranscriptEvent:
            return TranscriptEvent(
                session_id="s1",
                source=source,
                text="hello",
                speaker=speaker,
                start_ts=0.0,
                end_ts=1.0,
                is_final=True,
            )

        await reprocess.create(job("labelled"))
        await reprocess.create(job("plain"))
        await reprocess.create(job("empty"))
        await reprocess.create(job("pass", operation="diarize", target="plain"))
        await transcripts.add(row("reprocess:labelled", "Speaker A"))
        await transcripts.add(row("reprocess:plain", None))
        await transcripts.add(row("reprocess:plain", ""))
        await transcripts.add(row("diarize:plain", "Speaker B"))

        # Take the column away to stand in for a pre-v21 database, then run
        # only the migration under test (see the v16 test above for why).
        await conn.execute("ALTER TABLE reprocess_jobs DROP COLUMN has_speakers;")
        await conn.executescript(MIGRATIONS[_V_HAS_SPEAKERS])
        await conn.commit()

        flags = {j.id: j.has_speakers for j in await reprocess.for_session("s1")}
        assert flags == {
            "labelled": True,  # its own rows name a speaker
            "plain": False,  # rows, but none labelled; the diarize pass's rows are not its own
            "empty": False,  # no rows at all
            "pass": True,  # the relabeled copy it wrote, tagged with its target
        }


async def test_video_job_records_the_scene_its_prompt_was_converted_from(db: Database) -> None:
    """Both halves of a converted prompt survive the round trip, and a
    hand-written one stores neither - that absence is the only thing that tells
    a model's scene from a GM's own line afterwards."""
    videos = VideoRepository(db)
    await SessionRepository(db).create(Session(id="s1", started_at=0.0))

    await videos.create(
        VideoJob(
            id="converted",
            session_id="s1",
            provider_id="p1",
            model="alibaba/wan-3.0",
            prompt="A lone rider on a burning bridge, dusk, wide shot.",
            scene_model="gpt-5.6-luna",
            scene_source="The party crossed the bridge, at length.",
            created_at=1.0,
        )
    )
    await videos.create(
        VideoJob(
            id="by-hand",
            session_id="s1",
            provider_id="p1",
            model="alibaba/wan-3.0",
            prompt="a wizard walks into a tavern",
            created_at=2.0,
        )
    )

    converted = await videos.get("converted")
    assert converted is not None
    assert converted.scene_model == "gpt-5.6-luna"
    assert converted.scene_source == "The party crossed the bridge, at length."

    by_hand = await videos.get("by-hand")
    assert by_hand is not None
    assert by_hand.scene_model is None
    assert by_hand.scene_source is None


async def test_v25_adds_the_scene_columns_to_an_existing_database(db: Database) -> None:
    """An install that already has generated videos gains the columns without
    losing its rows, and every one of them reads as hand written - which is the
    honest answer for a prompt nobody recorded a conversion for."""
    conn = db.connection
    await SessionRepository(db).create(Session(id="s1", started_at=0.0))
    videos = VideoRepository(db)
    await videos.create(
        VideoJob(
            id="old",
            session_id="s1",
            provider_id="p1",
            model="alibaba/wan-3.0",
            prompt="a wizard walks into a tavern",
            created_at=1.0,
        )
    )

    # Take the columns away to stand in for a pre-v25 database, then run only
    # the migration under test (see the v16 test above for why).
    await conn.execute("ALTER TABLE video_jobs DROP COLUMN scene_model;")
    await conn.execute("ALTER TABLE video_jobs DROP COLUMN scene_source;")
    await conn.executescript(MIGRATIONS[_V_VIDEO_SCENE])
    await conn.commit()

    migrated = await videos.get("old")
    assert migrated is not None
    assert migrated.prompt == "a wizard walks into a tavern"
    assert migrated.scene_model is None
    assert migrated.scene_source is None
