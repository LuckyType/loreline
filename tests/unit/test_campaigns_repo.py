"""Campaigns, documents and search at the repository level."""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from pathlib import Path

import aiosqlite
import pytest
import pytest_asyncio

from loreline.capability_config import GlossarySupport
from loreline.models import (
    DEFAULT_GLOSSARY_CAMPAIGN,
    DOCUMENT_PREVIOUSLY_ON,
    DOCUMENT_RECAP,
    Campaign,
    CampaignDocument,
    CampaignPlayer,
    Glossary,
    Session,
    SessionDocument,
    SessionStatus,
    TranscriptEvent,
)
from loreline.persistence import (
    CampaignRepository,
    Database,
    DocumentRepository,
    GlossaryRepository,
    SearchRepository,
    SessionRepository,
    TranscriptRepository,
)
from loreline.persistence.database import FTS5_MARKER, MIGRATIONS
from loreline.stt.base import capped_terms

# Where the campaign layer starts, found by what its script does rather than
# counted from either end: migrations land at both ends of this list (the
# campaign layer itself landed in front of the search one), and any count here
# would silently point the upgrade test at somebody else's script the next time
# one does.
_BEFORE_CAMPAIGNS = next(
    index for index, script in enumerate(MIGRATIONS) if "CREATE TABLE campaigns" in script
)


@pytest_asyncio.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    database = Database(tmp_path / "campaigns.db")
    await database.connect()
    try:
        yield database
    finally:
        await database.close()


async def _table_exists(db: Database, name: str) -> bool:
    async with db.connection.execute("SELECT 1 FROM sqlite_master WHERE name = ?;", (name,)) as cur:
        return await cur.fetchone() is not None


def _campaign(name: str = "Curse of Strahd") -> Campaign:
    return Campaign(id=f"c-{name.lower().replace(' ', '-')}", name=name, created_at=time.time())


def _session(session_id: str, *, campaign_id: str | None = None, started_at: float = 1000.0):
    return Session(
        id=session_id,
        status=SessionStatus.COMPLETED,
        started_at=started_at,
        campaign_id=campaign_id,
    )


def _event(
    session_id: str,
    text: str,
    *,
    start_ts: float = 0.0,
    source: str = "prov",
    speaker: str | None = None,
    is_final: bool = True,
    turn_id: str | None = None,
) -> TranscriptEvent:
    return TranscriptEvent(
        session_id=session_id,
        source=source,
        text=text,
        start_ts=start_ts,
        end_ts=start_ts + 2.0,
        is_final=is_final,
        speaker=speaker,
        turn_id=turn_id,
    )


# --- migrations ---------------------------------------------------------


async def test_fresh_database_has_the_campaign_tables(db: Database) -> None:
    for table in ("campaigns", "session_documents", "campaign_documents"):
        assert await _table_exists(db, table), table
    assert db.fts5, "this SQLite build has no FTS5 - the fallback tests still apply"
    assert await _table_exists(db, "transcript_fts")


async def test_upgrading_backfills_the_search_index(tmp_path: Path) -> None:
    """A database that predates the index gets its existing rows indexed.

    The whole point of the backfill: a GM upgrading has a library of sessions
    already, and a search that only found what was recorded after the upgrade
    would look broken rather than new.
    """
    path = tmp_path / "upgrade.db"
    async with aiosqlite.connect(path) as conn:
        await conn.execute("CREATE TABLE schema_version (version INTEGER NOT NULL);")
        for index, script in enumerate(MIGRATIONS[:_BEFORE_CAMPAIGNS], start=1):
            await conn.executescript(script)
            await conn.execute("INSERT INTO schema_version (version) VALUES (?);", (index,))
        await conn.execute(
            "INSERT INTO sessions (id, status, started_at, diarization) VALUES (?, ?, ?, ?);",
            ("s1", "completed", 10.0, "{}"),
        )
        await conn.execute(
            "INSERT INTO transcript_segments "
            "(session_id, source, text, start_ts, end_ts, is_final, created_at) "
            "VALUES (?, ?, ?, ?, ?, 1, 0);",
            ("s1", "prov", "the lich guards the amulet", 1.0, 2.0),
        )
        await conn.commit()

    async with Database(path) as database:
        assert await _table_exists(database, "campaigns")
        hits = await SearchRepository(database).search("lich")
        assert [h.session_id for h in hits] == ["s1"]


async def test_the_fts_migration_is_marked_for_skipping() -> None:
    """The marker the migrator skips on is actually in the script.

    Matching on a string is what keeps the skip working when a migration lands
    in front of this one, and a typo in it would silently take the guard away:
    a build with no FTS5 would then fail every startup, which is the one
    outcome the guard exists to prevent.
    """
    assert sum(FTS5_MARKER in script for script in MIGRATIONS) == 1


# --- campaigns ----------------------------------------------------------


async def test_campaign_crud_and_listing(db: Database) -> None:
    campaigns = CampaignRepository(db)
    sessions = SessionRepository(db)
    first = _campaign("Curse of Strahd")
    second = _campaign("Alpha Complex")
    await campaigns.create(first)
    await campaigns.create(second)
    await sessions.create(_session("s1", campaign_id=first.id, started_at=100.0))
    await sessions.create(_session("s2", campaign_id=first.id, started_at=300.0))

    listed = await campaigns.list()
    # By name, case-insensitively: "Alpha Complex" before "Curse of Strahd".
    assert [row.campaign.name for row in listed] == ["Alpha Complex", "Curse of Strahd"]
    assert [(row.sessions, row.last_session_at) for row in listed] == [(0, None), (2, 300.0)]

    assert (await campaigns.by_name("Curse of Strahd")) is not None
    assert (await campaigns.by_name("curse of strahd")) is None  # exact, not fuzzy

    await campaigns.update(first.model_copy(update={"name": "Barovia", "notes": "gloomy"}))
    reread = await campaigns.get(first.id)
    assert reread is not None
    assert (reread.name, reread.notes) == ("Barovia", "gloomy")


async def test_deleting_a_campaign_keeps_its_sessions(db: Database) -> None:
    """Unassigned, not deleted - and its glossary goes, the default one stays."""
    campaigns = CampaignRepository(db)
    sessions = SessionRepository(db)
    glossaries = GlossaryRepository(db)
    campaign = _campaign()
    await campaigns.create(campaign)
    await sessions.create(_session("s1", campaign_id=campaign.id))
    await glossaries.put(Glossary(campaign_id=campaign.id, terms=["Strahd"]))
    await glossaries.put(Glossary(campaign_id=DEFAULT_GLOSSARY_CAMPAIGN, terms=["Aurora"]))

    await campaigns.delete(campaign.id)

    assert await campaigns.get(campaign.id) is None
    survivor = await sessions.get("s1")
    assert survivor is not None and survivor.campaign_id is None
    assert (await glossaries.get(campaign.id)).terms == []
    assert (await glossaries.get(DEFAULT_GLOSSARY_CAMPAIGN)).terms == ["Aurora"]


async def test_assign_puts_a_session_in_and_out_of_a_campaign(db: Database) -> None:
    campaigns = CampaignRepository(db)
    sessions = SessionRepository(db)
    campaign = _campaign()
    await campaigns.create(campaign)
    await sessions.create(_session("s1"))

    await campaigns.assign("s1", campaign.id)
    assigned = await sessions.get("s1")
    assert assigned is not None and assigned.campaign_id == campaign.id

    await campaigns.assign("s1", None)
    unassigned = await sessions.get("s1")
    assert unassigned is not None and unassigned.campaign_id is None


# --- documents ----------------------------------------------------------


async def test_documents_are_one_per_session_and_kind(db: Database) -> None:
    """Writing a recap replaces the recap rather than piling another one up."""
    sessions = SessionRepository(db)
    documents = DocumentRepository(db)
    await sessions.create(_session("s1"))
    await documents.put_session_document(
        SessionDocument(
            session_id="s1", kind=DOCUMENT_RECAP, body="first", model="m1", created_at=1.0
        )
    )
    await documents.put_session_document(
        SessionDocument(
            session_id="s1", kind=DOCUMENT_RECAP, body="second", model="m2", created_at=2.0
        )
    )

    stored = await documents.get_session_document("s1", DOCUMENT_RECAP)
    assert stored is not None
    assert (stored.body, stored.model) == ("second", "m2")
    assert len(await documents.for_session("s1")) == 1


async def test_campaign_documents_list_in_session_order(db: Database) -> None:
    campaigns = CampaignRepository(db)
    sessions = SessionRepository(db)
    documents = DocumentRepository(db)
    campaign = _campaign()
    await campaigns.create(campaign)
    await sessions.create(_session("late", campaign_id=campaign.id, started_at=900.0))
    await sessions.create(_session("early", campaign_id=campaign.id, started_at=100.0))
    await sessions.create(_session("other", started_at=50.0))
    for session_id in ("late", "early", "other"):
        await documents.put_session_document(
            SessionDocument(
                session_id=session_id, kind=DOCUMENT_RECAP, body=session_id, created_at=1.0
            )
        )

    rows = await documents.for_campaign(campaign.id, DOCUMENT_RECAP)
    # Oldest session first, and a session in no campaign is not in the list.
    assert [r.session_id for r in rows] == ["early", "late"]


async def test_campaign_document_round_trip(db: Database) -> None:
    campaigns = CampaignRepository(db)
    documents = DocumentRepository(db)
    campaign = _campaign()
    await campaigns.create(campaign)
    await documents.put_campaign_document(
        CampaignDocument(
            campaign_id=campaign.id,
            kind=DOCUMENT_PREVIOUSLY_ON,
            body="Last time...",
            created_at=1.0,
        )
    )
    stored = await documents.get_campaign_document(campaign.id, DOCUMENT_PREVIOUSLY_ON)
    assert stored is not None and stored.body == "Last time..."
    assert await documents.get_campaign_document(campaign.id, "nothing") is None


# --- search -------------------------------------------------------------


async def _seed_for_search(db: Database) -> str:
    campaigns = CampaignRepository(db)
    sessions = SessionRepository(db)
    transcripts = TranscriptRepository(db)
    campaign = _campaign()
    await campaigns.create(campaign)
    await sessions.create(_session("s1", campaign_id=campaign.id, started_at=100.0))
    await sessions.create(_session("s2", started_at=200.0))
    await transcripts.add(
        _event("s1", "The lich raises the amulet over Barovia", start_ts=12.5, speaker="Ireena")
    )
    await transcripts.add(_event("s1", "nothing of interest happens", start_ts=30.0))
    await transcripts.add(_event("s2", "another lich entirely", start_ts=5.0))
    return campaign.id


async def test_search_returns_a_marked_snippet_and_a_deep_link(db: Database) -> None:
    await _seed_for_search(db)
    hits = await SearchRepository(db).search("amulet")

    assert len(hits) == 1
    hit = hits[0]
    assert hit.session_id == "s1"
    assert hit.speaker == "Ireena"
    assert hit.start_ts == 12.5
    assert hit.version == "original"
    assert "[amulet]" in hit.snippet


async def test_search_can_be_scoped_to_one_campaign(db: Database) -> None:
    campaign_id = await _seed_for_search(db)
    everywhere = await SearchRepository(db).search("lich")
    scoped = await SearchRepository(db).search("lich", campaign_id)

    assert {h.session_id for h in everywhere} == {"s1", "s2"}
    assert {h.session_id for h in scoped} == {"s1"}


async def test_search_skips_interims_and_gap_markers(db: Database) -> None:
    """Neither is something somebody said, so neither is something to find."""
    sessions = SessionRepository(db)
    transcripts = TranscriptRepository(db)
    await sessions.create(_session("s1"))
    await transcripts.add(
        _event("s1", "the dragon is waking", start_ts=1.0, is_final=False, turn_id="t1")
    )
    await transcripts.add(_event("s1", "the dragon is lost", start_ts=5.0, source="gap"))

    assert await SearchRepository(db).search("dragon") == []


async def test_search_ranks_by_relevance(db: Database) -> None:
    sessions = SessionRepository(db)
    transcripts = TranscriptRepository(db)
    await sessions.create(_session("s1"))
    await transcripts.add(_event("s1", "we ride north past the tower", start_ts=1.0))
    await transcripts.add(_event("s1", "tower tower tower", start_ts=2.0))

    hits = await SearchRepository(db).search("tower")
    # bm25 puts the denser line first; the fallback below cannot, which is the
    # difference the search page tells the reader about.
    assert [h.start_ts for h in hits] == [2.0, 1.0]


async def test_a_query_of_syntax_is_a_search_not_a_crash(db: Database) -> None:
    """FTS5 reads "AND", "(" and "*" as query language; a search box does not."""
    await _seed_for_search(db)
    repo = SearchRepository(db)
    assert await repo.search("(amulet AND") == []  # quoted: no such phrase, no error
    assert await repo.search("   ") == []
    # Nothing but quote marks is dropped rather than quoted into an empty
    # phrase, which FTS5 refuses and which LIKE would read as "everything".
    assert await repo.search('"') == []


async def test_search_falls_back_to_like_without_fts5(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same hits, the same bracketed snippet, no ranking.

    Exercised by telling the database it has no FTS5 rather than by finding a
    SQLite without it: what the fallback has to get right is the SQL and the
    snippet, and both are the same whether the index exists and is ignored or
    never existed at all.
    """
    await _seed_for_search(db)
    monkeypatch.setattr(type(db), "fts5", property(lambda _self: False))

    hits = await SearchRepository(db).search("amulet")
    assert len(hits) == 1
    assert hits[0].session_id == "s1"
    assert "[amulet]" in hits[0].snippet

    # Newest session first, since there is nothing to rank by.
    both = await SearchRepository(db).search("lich")
    assert [h.session_id for h in both] == ["s2", "s1"]


async def test_the_like_fallback_escapes_wildcards(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A search for "50%" must not be a search for everything."""
    sessions = SessionRepository(db)
    transcripts = TranscriptRepository(db)
    await sessions.create(_session("s1"))
    await transcripts.add(_event("s1", "the potion heals 50% of your wounds", start_ts=1.0))
    await transcripts.add(_event("s1", "the door is locked", start_ts=2.0))
    monkeypatch.setattr(type(db), "fts5", property(lambda _self: False))

    hits = await SearchRepository(db).search("50%")
    assert [h.start_ts for h in hits] == [1.0]


async def test_deleting_a_session_takes_its_rows_out_of_the_index(db: Database) -> None:
    """Otherwise a search would keep offering lines nobody can open any more."""
    sessions = SessionRepository(db)
    transcripts = TranscriptRepository(db)
    await sessions.create(_session("s1"))
    await transcripts.add(_event("s1", "the lich raises the amulet", start_ts=1.0))
    assert await SearchRepository(db).search("amulet")

    await transcripts.delete_session("s1")
    await sessions.delete("s1")
    assert await SearchRepository(db).search("amulet") == []


async def test_a_replaced_turn_is_searched_as_its_final_text(db: Database) -> None:
    """The streaming path upserts a turn in place; the index has to follow it."""
    sessions = SessionRepository(db)
    transcripts = TranscriptRepository(db)
    await sessions.create(_session("s1"))
    await transcripts.add(
        _event("s1", "we ride to Vall", start_ts=1.0, is_final=False, turn_id="t1")
    )
    await transcripts.add(
        _event("s1", "we ride to Vallaki", start_ts=1.0, is_final=True, turn_id="t1")
    )

    assert [h.snippet for h in await SearchRepository(db).search("Vallaki")]
    assert await SearchRepository(db).search("Vall") == []


# --- the cast at the table ------------------------------------------------


async def test_a_campaign_stores_its_cast_in_order(db: Database) -> None:
    """Created with it, updated with it, read back in the order it was given."""
    campaigns = CampaignRepository(db)
    campaign = _campaign().model_copy(
        update={
            "players": [
                CampaignPlayer(player="Sara", character="Ireena"),
                CampaignPlayer(character="Ismark"),
                CampaignPlayer(player="Ben"),
            ]
        }
    )
    await campaigns.create(campaign)

    reread = await campaigns.get(campaign.id)
    assert reread is not None
    assert [(p.player, p.character) for p in reread.players] == [
        ("Sara", "Ireena"),
        ("", "Ismark"),
        ("Ben", ""),
    ]

    # Reordering is a save of the whole list, which is what makes the drag
    # handles on the campaign page mean anything: order is priority.
    flipped = list(reversed(reread.players))
    await campaigns.update(reread.model_copy(update={"players": flipped}))
    after = await campaigns.get(campaign.id)
    assert after is not None
    assert [p.character or p.player for p in after.players] == ["Ben", "Ismark", "Ireena"]

    # And it survives the list query, which reads the same rows a different way.
    listed = await campaigns.list()
    assert [p.character or p.player for p in listed[0].campaign.players] == [
        "Ben",
        "Ismark",
        "Ireena",
    ]


async def test_a_player_needs_a_name_on_one_side(db: Database) -> None:
    with pytest.raises(ValueError, match="name the player"):
        CampaignPlayer(player="  ", character="")
    # Whitespace is trimmed on the way in, so " Sara " and "Sara" are one name.
    assert CampaignPlayer(player=" Sara ").player == "Sara"


async def test_an_unreadable_cast_is_no_cast_rather_than_an_error(db: Database) -> None:
    """A body an older schema wrote must not be able to stop a capture starting.

    ``get_effective`` runs while a session is being started, so the campaigns
    row it reads is on the path that cannot fail: a column of nonsense means
    this campaign has no cast, and the next save from the editor rewrites it.
    """
    campaigns = CampaignRepository(db)
    campaign = _campaign()
    await campaigns.create(campaign)
    await db.connection.execute(
        "UPDATE campaigns SET players = ? WHERE id = ?;",
        ('[{"who": "Sara"}, "not an object", 7]', campaign.id),
    )
    await db.connection.commit()

    reread = await campaigns.get(campaign.id)
    assert reread is not None and reread.players == []
    assert await GlossaryRepository(db).get_effective(campaign.id) is None


async def test_the_cast_is_the_head_of_the_effective_glossary(db: Database) -> None:
    """Cast, then the campaign's terms, then the default list.

    Asserted through the ceiling rather than on the whole list, because the
    ceiling is the only reason the order matters: ``capped_terms`` keeps the
    head, so with room for three terms the three that survive have to be the
    people at this table.
    """
    campaigns = CampaignRepository(db)
    glossaries = GlossaryRepository(db)
    campaign = _campaign().model_copy(
        update={
            "players": [
                CampaignPlayer(player="Sara", character="Ireena"),
                CampaignPlayer(player="Tom", character="Ismark"),
            ]
        }
    )
    await campaigns.create(campaign)
    await glossaries.put(Glossary(campaign_id=campaign.id, terms=["Vallaki", "ireena"]))
    await glossaries.put(
        Glossary(campaign_id=DEFAULT_GLOSSARY_CAMPAIGN, terms=["Aurora", "Mistwood"])
    )

    effective = await glossaries.get_effective(campaign.id)
    assert effective is not None
    # Per row the character comes before the player: the character is the word
    # that gets said out loud. "ireena" is dropped as a duplicate of "Ireena",
    # and the higher-priority spelling is the one kept.
    assert effective.terms == ["Ireena", "Sara", "Ismark", "Tom", "Vallaki", "Aurora", "Mistwood"]

    support = GlossarySupport(supported=True, field="keyterm", max_terms=3, max_terms_realtime=3)
    assert capped_terms(effective.terms, support, realtime=True) == ["Ireena", "Sara", "Ismark"]

    # A campaign with no cast is the list it always was, campaign terms first.
    bare = _campaign("Alpha Complex")
    await campaigns.create(bare)
    await glossaries.put(Glossary(campaign_id=bare.id, terms=["Friend Computer"]))
    plain = await glossaries.get_effective(bare.id)
    assert plain is not None
    assert plain.terms == ["Friend Computer", "Aurora", "Mistwood"]
