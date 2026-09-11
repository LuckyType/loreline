"""Repositories: typed CRUD over the SQLite tables."""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING

import aiosqlite

from loreline.models import (
    DEFAULT_GLOSSARY_CAMPAIGN,
    DIARIZE_SOURCE_PREFIX,
    GAP_SOURCE,
    ORIGINAL_VERSION,
    REPROCESS_SOURCE_PREFIX,
    Campaign,
    CampaignDocument,
    CampaignSummary,
    DiarizationConfig,
    Glossary,
    JobStatus,
    OpenRouterRouting,
    ProviderConfig,
    ProviderKind,
    ReprocessJob,
    SearchHit,
    Session,
    SessionDocument,
    SessionStatus,
    TranscriptEvent,
    VideoJob,
    Word,
)

if TYPE_CHECKING:
    from loreline.persistence.database import Database


class ProviderRepository:
    """CRUD for configured STT providers."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def list(self) -> list[ProviderConfig]:
        async with self._db.connection.execute("SELECT * FROM providers ORDER BY name;") as cur:
            rows = await cur.fetchall()
        return [_row_to_provider(r) for r in rows]

    async def get(self, provider_id: str) -> ProviderConfig | None:
        async with self._db.connection.execute(
            "SELECT * FROM providers WHERE id = ?;", (provider_id,)
        ) as cur:
            row = await cur.fetchone()
        return _row_to_provider(row) if row is not None else None

    async def upsert(self, provider: ProviderConfig) -> None:
        await self._db.connection.execute(
            """
            INSERT INTO providers
                (id, name, kind, base_url, auth_ref, sample_rate,
                 language, enabled, favorite_models, routing)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                name=excluded.name, kind=excluded.kind, base_url=excluded.base_url,
                auth_ref=excluded.auth_ref, sample_rate=excluded.sample_rate,
                language=excluded.language, enabled=excluded.enabled,
                favorite_models=excluded.favorite_models, routing=excluded.routing;
            """,
            (
                provider.id,
                provider.name,
                provider.kind.value,
                provider.base_url,
                provider.auth_ref,
                provider.sample_rate,
                provider.language,
                int(provider.enabled),
                json.dumps(provider.favorite_models),
                provider.routing.model_dump_json() if provider.routing else None,
            ),
        )
        await self._db.connection.commit()

    async def delete(self, provider_id: str) -> None:
        await self._db.connection.execute("DELETE FROM providers WHERE id = ?;", (provider_id,))
        await self._db.connection.commit()


class GlossaryRepository:
    """CRUD for per-campaign glossaries."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def get(self, campaign_id: str) -> Glossary:
        async with self._db.connection.execute(
            "SELECT terms FROM glossaries WHERE campaign_id = ?;", (campaign_id,)
        ) as cur:
            row = await cur.fetchone()
        terms: list[str] = json.loads(row["terms"]) if row is not None else []
        return Glossary(campaign_id=campaign_id, terms=terms)

    async def put(self, glossary: Glossary) -> None:
        await self._db.connection.execute(
            """
            INSERT INTO glossaries (campaign_id, terms) VALUES (?, ?)
            ON CONFLICT(campaign_id) DO UPDATE SET terms=excluded.terms;
            """,
            (glossary.campaign_id, json.dumps(glossary.terms)),
        )
        await self._db.connection.commit()

    async def get_effective(self, campaign_id: str | None) -> Glossary | None:
        """Default word list merged with a campaign's terms; None if both empty.

        The always-on ``_default`` list applies to every session; a campaign's own
        terms are appended (deduped) when a campaign id is given.
        """
        terms: list[str] = list((await self.get(DEFAULT_GLOSSARY_CAMPAIGN)).terms)
        if campaign_id and campaign_id != DEFAULT_GLOSSARY_CAMPAIGN:
            for term in (await self.get(campaign_id)).terms:
                if term not in terms:
                    terms.append(term)
        if not terms:
            return None
        return Glossary(campaign_id=campaign_id or DEFAULT_GLOSSARY_CAMPAIGN, terms=terms)


class SessionRepository:
    """CRUD for capture sessions."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def create(self, session: Session) -> None:
        await self._db.connection.execute(
            """
            INSERT INTO sessions
                (id, status, started_at, started_mono, ended_at, campaign_id,
                 primary_provider, fallback_provider, diarization, audio_path,
                 merged_from)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """,
            (
                session.id,
                session.status.value,
                session.started_at,
                session.started_mono,
                session.ended_at,
                session.campaign_id,
                session.primary_provider,
                session.fallback_provider,
                session.diarization.model_dump_json(),
                session.audio_path,
                # Written at creation rather than by a setter, unlike the
                # speaker map and the summary: what a row was merged from is
                # true the instant it exists and never changes afterwards.
                json.dumps(session.merged_from),
            ),
        )
        await self._db.connection.commit()

    async def get(self, session_id: str) -> Session | None:
        async with self._db.connection.execute(
            "SELECT * FROM sessions WHERE id = ?;", (session_id,)
        ) as cur:
            row = await cur.fetchone()
        return _row_to_session(row) if row is not None else None

    async def list(self) -> list[Session]:
        async with self._db.connection.execute(
            "SELECT * FROM sessions ORDER BY started_at DESC;"
        ) as cur:
            rows = await cur.fetchall()
        return [_row_to_session(r) for r in rows]

    async def finish(self, session_id: str, status: SessionStatus) -> None:
        await self._db.connection.execute(
            "UPDATE sessions SET status = ?, ended_at = ? WHERE id = ?;",
            (status.value, time.time(), session_id),
        )
        await self._db.connection.commit()

    async def mark_interrupted(self, transcripts: TranscriptRepository) -> None:
        """Fail sessions left CAPTURING by a previous process (startup sweep).

        ``SessionManager`` only ever transitions a session out of CAPTURING
        from inside ``stop()`` - the in-memory runtime that would call it lives
        only as long as the process does. If the process dies uncleanly
        (crash, ``kill -9``, OOM, power loss) mid-capture, that row is stuck
        at CAPTURING forever with no ``ended_at``: nothing else revisits it,
        so it just sits in the history list looking like a session that's
        eternally still recording. Mirrors ``ReprocessRepository.mark_interrupted``.

        A capturing session that was streaming can also leave interim rows
        behind: the connector never got the chance to replace them with a
        final. ``TranscriptRepository.delete_interims`` otherwise only runs
        from ``SessionManager._finish``, which a killed process never reaches,
        so this sweeps it for every session about to be failed here - or a
        reloaded history would show a half-typed line as settled text forever.
        """
        interrupted = [s.id for s in await self.list() if s.status is SessionStatus.CAPTURING]

        await self._db.connection.execute(
            "UPDATE sessions SET status = ?, ended_at = ? WHERE status = ?;",
            (SessionStatus.ERROR.value, time.time(), SessionStatus.CAPTURING.value),
        )
        await self._db.connection.commit()

        for session_id in interrupted:
            await transcripts.delete_interims(session_id)

    async def delete(self, session_id: str) -> None:
        await self._db.connection.execute("DELETE FROM sessions WHERE id = ?;", (session_id,))
        await self._db.connection.commit()

    async def set_speaker_names(self, session_id: str, names: dict[str, str]) -> None:
        """Persist the per-session speaker rename map ({label: display name})."""
        await self._db.connection.execute(
            "UPDATE sessions SET speaker_names = ? WHERE id = ?;",
            (json.dumps(names), session_id),
        )
        await self._db.connection.commit()

    async def set_summary(
        self, session_id: str, summary: str, *, provider_id: str, model: str, version: str
    ) -> None:
        """Persist the LLM-generated session summary and what produced it.

        "What produced it" is three things, not two: the provider, the model,
        and the transcript version that was fed in. A session carries the live
        capture plus one version per re-transcription, and they differ by
        hundreds of segments, so a summary that cannot name its version cannot
        be judged - the same provider and model over two versions are two
        different summaries.
        """
        await self._db.connection.execute(
            "UPDATE sessions SET summary = ?, summary_provider = ?, summary_model = ?, "
            "summary_version = ? WHERE id = ?;",
            (summary, provider_id, model, version, session_id),
        )
        await self._db.connection.commit()


class TranscriptRepository:
    """Write + read transcript segments, one row per turn."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def add(self, event: TranscriptEvent) -> None:
        """Store one segment, replacing the turn's previous revision if any.

        The utterance path publishes each segment once, settled, with no
        ``turn_id``: SQLite counts NULLs as distinct, so the conflict clause
        below never fires for those and every call appends, exactly as it did
        before turns existed.

        A streaming connector publishes the same turn several times, as a
        growing interim and then as a final, all under one ``turn_id``. Those
        are revisions of one segment, not segments: appending them would store
        (and show, and export) a session once per word. The upsert is what
        makes "a final replaces its interims" true in the table rather than
        only in the browser, so a page reloaded mid-session, and a session the
        process died in, both read back one row per turn.
        """
        words_json = json.dumps([w.model_dump() for w in event.words])
        await self._db.connection.execute(
            """
            INSERT INTO transcript_segments
                (session_id, source, text, speaker, start_ts, end_ts, is_final,
                 words, created_at, turn_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(session_id, source, turn_id) DO UPDATE SET
                text=excluded.text, speaker=excluded.speaker,
                start_ts=excluded.start_ts, end_ts=excluded.end_ts,
                is_final=excluded.is_final, words=excluded.words,
                created_at=excluded.created_at;
            """,
            (
                event.session_id,
                event.source,
                event.text,
                event.speaker,
                event.start_ts,
                event.end_ts,
                int(event.is_final),
                words_json,
                time.time(),
                event.turn_id,
            ),
        )
        await self._db.connection.commit()

    async def delete_interims(self, session_id: str) -> None:
        """Drop any segment still marked interim (used when a session ends).

        A streaming stop settles every open turn, so this normally deletes
        nothing. It exists for the endings that cannot settle anything: the
        process killed mid-turn, a connector that died between an interim and
        its final. What it prevents is a stored transcript keeping a half-typed
        row forever, which no reader could tell from a real one, since the
        vendor was never going to send that final.
        """
        await self._db.connection.execute(
            "DELETE FROM transcript_segments WHERE session_id = ? AND is_final = 0;",
            (session_id,),
        )
        await self._db.connection.commit()

    async def for_session(self, session_id: str) -> list[TranscriptEvent]:
        async with self._db.connection.execute(
            "SELECT * FROM transcript_segments WHERE session_id = ? ORDER BY start_ts;",
            (session_id,),
        ) as cur:
            rows = await cur.fetchall()
        return [_row_to_event(r) for r in rows]

    async def delete_source(self, session_id: str, source: str) -> None:
        """Remove all segments for a (session, source) - supports replace-in-place."""
        await self._db.connection.execute(
            "DELETE FROM transcript_segments WHERE session_id = ? AND source = ?;",
            (session_id, source),
        )
        await self._db.connection.commit()

    async def delete_session(self, session_id: str) -> None:
        """Remove every transcript segment for a session (used on session delete)."""
        await self._db.connection.execute(
            "DELETE FROM transcript_segments WHERE session_id = ?;", (session_id,)
        )
        await self._db.connection.commit()


class ReprocessRepository:
    """CRUD for post-session re-processing jobs."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def create(self, job: ReprocessJob) -> None:
        await self._db.connection.execute(
            """
            INSERT INTO reprocess_jobs
                (id, session_id, provider_id, operation, model, target, use_glossary,
                 diarization, status, created_at, started_at, finished_at, segments_added,
                 has_speakers, error)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """,
            (
                job.id,
                job.session_id,
                job.provider_id,
                job.operation,
                job.model,
                job.target,
                int(job.use_glossary),
                job.diarization.model_dump_json(),
                job.status.value,
                job.created_at,
                job.started_at,
                job.finished_at,
                job.segments_added,
                int(job.has_speakers),
                job.error,
            ),
        )
        await self._db.connection.commit()

    async def get(self, job_id: str) -> ReprocessJob | None:
        async with self._db.connection.execute(
            "SELECT * FROM reprocess_jobs WHERE id = ?;", (job_id,)
        ) as cur:
            row = await cur.fetchone()
        return _row_to_job(row) if row is not None else None

    async def for_session(self, session_id: str) -> list[ReprocessJob]:
        async with self._db.connection.execute(
            "SELECT * FROM reprocess_jobs WHERE session_id = ? ORDER BY created_at DESC;",
            (session_id,),
        ) as cur:
            rows = await cur.fetchall()
        return [_row_to_job(r) for r in rows]

    async def update(self, job: ReprocessJob) -> None:
        await self._db.connection.execute(
            """
            UPDATE reprocess_jobs SET
                status = ?, started_at = ?, finished_at = ?,
                segments_added = ?, has_speakers = ?, error = ?
            WHERE id = ?;
            """,
            (
                job.status.value,
                job.started_at,
                job.finished_at,
                job.segments_added,
                int(job.has_speakers),
                job.error,
                job.id,
            ),
        )
        await self._db.connection.commit()

    async def delete_version(self, session_id: str, version: str) -> None:
        """Drop the job row for a transcript version, plus any diarize job aimed at it.

        The version's diarized copy is deleted with its base rows (see
        ``ReprocessManager.delete_version``), so the jobs that produced it would
        otherwise describe work on a transcript nobody can reach.
        """
        await self._db.connection.execute(
            "DELETE FROM reprocess_jobs WHERE session_id = ? "
            "AND (id = ? OR (operation = 'diarize' AND target = ?));",
            (session_id, version, version),
        )
        await self._db.connection.commit()

    async def mark_interrupted(self) -> None:
        """Fail jobs left QUEUED/RUNNING by a previous process (startup sweep)."""
        await self._db.connection.execute(
            "UPDATE reprocess_jobs SET status = ?, error = ? WHERE status IN (?, ?);",
            (
                JobStatus.ERROR.value,
                "interrupted by restart",
                JobStatus.QUEUED.value,
                JobStatus.RUNNING.value,
            ),
        )
        await self._db.connection.commit()


class SettingsRepository:
    """Small key/value store over ``kv_settings`` for persisted app config."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def get(self, key: str) -> str | None:
        async with self._db.connection.execute(
            "SELECT value FROM kv_settings WHERE key = ?;", (key,)
        ) as cur:
            row = await cur.fetchone()
        return row["value"] if row is not None else None

    async def set(self, key: str, value: str) -> None:
        await self._db.connection.execute(
            """
            INSERT INTO kv_settings (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value;
            """,
            (key, value),
        )
        await self._db.connection.commit()


def _row_to_provider(row: aiosqlite.Row) -> ProviderConfig:
    return ProviderConfig(
        id=row["id"],
        name=row["name"],
        kind=ProviderKind(row["kind"]),
        base_url=row["base_url"],
        auth_ref=row["auth_ref"],
        sample_rate=row["sample_rate"],
        language=row["language"],
        routing=(OpenRouterRouting.model_validate_json(row["routing"]) if row["routing"] else None),
        enabled=bool(row["enabled"]),
        favorite_models=json.loads(row["favorite_models"]),
    )


def _row_to_session(row: aiosqlite.Row) -> Session:
    return Session(
        id=row["id"],
        status=SessionStatus(row["status"]),
        started_at=row["started_at"],
        started_mono=row["started_mono"],
        ended_at=row["ended_at"],
        campaign_id=row["campaign_id"],
        primary_provider=row["primary_provider"],
        fallback_provider=row["fallback_provider"],
        diarization=DiarizationConfig.model_validate_json(row["diarization"]),
        audio_path=row["audio_path"],
        speaker_names=json.loads(row["speaker_names"]),
        summary=row["summary"],
        summary_provider=row["summary_provider"],
        summary_model=row["summary_model"],
        summary_version=row["summary_version"],
        merged_from=json.loads(row["merged_from"]),
    )


def _row_to_event(row: aiosqlite.Row) -> TranscriptEvent:
    words_raw: list[dict[str, object]] = json.loads(row["words"])
    return TranscriptEvent(
        session_id=row["session_id"],
        source=row["source"],
        text=row["text"],
        words=[Word.model_validate(w) for w in words_raw],
        speaker=row["speaker"],
        start_ts=row["start_ts"],
        end_ts=row["end_ts"],
        is_final=bool(row["is_final"]),
        turn_id=row["turn_id"],
    )


def _row_to_job(row: aiosqlite.Row) -> ReprocessJob:
    return ReprocessJob(
        id=row["id"],
        session_id=row["session_id"],
        provider_id=row["provider_id"],
        operation=row["operation"],
        model=row["model"],
        target=row["target"],
        use_glossary=bool(row["use_glossary"]),
        diarization=DiarizationConfig.model_validate_json(row["diarization"]),
        status=JobStatus(row["status"]),
        created_at=row["created_at"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        segments_added=row["segments_added"],
        has_speakers=bool(row["has_speakers"]),
        error=row["error"],
    )


class VideoRepository:
    """CRUD for video-generation jobs.

    Mirrors :class:`ReprocessRepository` - same enqueue/poll/update shape, for
    the same reason: a generation outlives the request that started it, so its
    state has to live in a row rather than in memory.
    """

    def __init__(self, db: Database) -> None:
        self._db = db

    async def create(self, job: VideoJob) -> None:
        await self._db.connection.execute(
            """
            INSERT INTO video_jobs
                (id, session_id, provider_id, model, prompt, duration, resolution,
                 aspect_ratio, generate_audio, seed, status, remote_id, video_path,
                 created_at, started_at, finished_at, error)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """,
            (
                job.id,
                job.session_id,
                job.provider_id,
                job.model,
                job.prompt,
                job.duration,
                job.resolution,
                job.aspect_ratio,
                int(job.generate_audio),
                job.seed,
                job.status.value,
                job.remote_id,
                job.video_path,
                job.created_at,
                job.started_at,
                job.finished_at,
                job.error,
            ),
        )
        await self._db.connection.commit()

    async def get(self, job_id: str) -> VideoJob | None:
        async with self._db.connection.execute(
            "SELECT * FROM video_jobs WHERE id = ?;", (job_id,)
        ) as cur:
            row = await cur.fetchone()
        return _row_to_video_job(row) if row is not None else None

    async def for_session(self, session_id: str) -> list[VideoJob]:
        async with self._db.connection.execute(
            "SELECT * FROM video_jobs WHERE session_id = ? ORDER BY created_at DESC;",
            (session_id,),
        ) as cur:
            rows = await cur.fetchall()
        return [_row_to_video_job(r) for r in rows]

    async def update(self, job: VideoJob) -> None:
        await self._db.connection.execute(
            """
            UPDATE video_jobs SET
                status = ?, remote_id = ?, video_path = ?,
                started_at = ?, finished_at = ?, error = ?
            WHERE id = ?;
            """,
            (
                job.status.value,
                job.remote_id,
                job.video_path,
                job.started_at,
                job.finished_at,
                job.error,
                job.id,
            ),
        )
        await self._db.connection.commit()

    async def delete(self, job_id: str) -> None:
        await self._db.connection.execute("DELETE FROM video_jobs WHERE id = ?;", (job_id,))
        await self._db.connection.commit()

    async def mark_interrupted(self) -> None:
        """Fail jobs left queued/running by a previous process (startup sweep).

        The polling loop that would advance them lives only as long as the
        process; without this a job killed mid-generation shows as "running"
        forever. Mirrors ``ReprocessRepository.mark_interrupted``.
        """
        await self._db.connection.execute(
            "UPDATE video_jobs SET status = ?, finished_at = ?, error = ? WHERE status IN (?, ?);",
            (
                JobStatus.ERROR.value,
                time.time(),
                "interrupted by a restart",
                JobStatus.QUEUED.value,
                JobStatus.RUNNING.value,
            ),
        )
        await self._db.connection.commit()


def _row_to_video_job(row: aiosqlite.Row) -> VideoJob:
    return VideoJob(
        id=row["id"],
        session_id=row["session_id"],
        provider_id=row["provider_id"],
        model=row["model"],
        prompt=row["prompt"],
        duration=row["duration"],
        resolution=row["resolution"],
        aspect_ratio=row["aspect_ratio"],
        generate_audio=bool(row["generate_audio"]),
        seed=row["seed"],
        status=JobStatus(row["status"]),
        remote_id=row["remote_id"],
        video_path=row["video_path"],
        created_at=row["created_at"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        error=row["error"],
    )


class CampaignRepository:
    """CRUD for campaigns, the thing a session belongs to.

    Deleting one is the only operation with anything to decide, and it decides
    in favour of the recordings: a campaign row, its glossary and its documents
    go, and its sessions are unassigned rather than deleted. A campaign is a
    label on work that already happened; removing the label must not be able to
    destroy four hours of audio nobody can record again.
    """

    def __init__(self, db: Database) -> None:
        self._db = db

    async def create(self, campaign: Campaign) -> None:
        await self._db.connection.execute(
            """
            INSERT INTO campaigns (id, name, created_at, notes, recap_prompt)
            VALUES (?, ?, ?, ?, ?);
            """,
            (
                campaign.id,
                campaign.name,
                campaign.created_at,
                campaign.notes,
                campaign.recap_prompt,
            ),
        )
        await self._db.connection.commit()

    async def get(self, campaign_id: str) -> Campaign | None:
        async with self._db.connection.execute(
            "SELECT * FROM campaigns WHERE id = ?;", (campaign_id,)
        ) as cur:
            row = await cur.fetchone()
        return _row_to_campaign(row) if row is not None else None

    async def by_name(self, name: str) -> Campaign | None:
        """The campaign with this exact name, or None.

        The name is unique in the table, so this is what turns "create it if it
        is not there" into one question rather than a caught constraint error.
        """
        async with self._db.connection.execute(
            "SELECT * FROM campaigns WHERE name = ?;", (name,)
        ) as cur:
            row = await cur.fetchone()
        return _row_to_campaign(row) if row is not None else None

    async def list(self) -> list[CampaignSummary]:
        """Every campaign with its session count and its most recent session.

        Counted in the query rather than by listing the sessions and grouping
        them in Python: the list page asks this on every visit, and a table
        with a few hundred sessions is not worth loading to answer "how many".
        A campaign with no sessions yet comes back with a zero and a null,
        which is what the LEFT JOIN is for - it is a campaign somebody just
        made, and it has to appear in the list they made it from.
        """
        async with self._db.connection.execute(
            """
            SELECT c.*, COUNT(s.id) AS sessions, MAX(s.started_at) AS last_session_at
            FROM campaigns c
            LEFT JOIN sessions s ON s.campaign_id = c.id
            GROUP BY c.id
            ORDER BY c.name COLLATE NOCASE;
            """
        ) as cur:
            rows = await cur.fetchall()
        return [
            CampaignSummary(
                campaign=_row_to_campaign(r),
                sessions=r["sessions"],
                last_session_at=r["last_session_at"],
            )
            for r in rows
        ]

    async def update(self, campaign: Campaign) -> None:
        await self._db.connection.execute(
            "UPDATE campaigns SET name = ?, notes = ?, recap_prompt = ? WHERE id = ?;",
            (campaign.name, campaign.notes, campaign.recap_prompt, campaign.id),
        )
        await self._db.connection.commit()

    async def delete(self, campaign_id: str) -> None:
        """Remove a campaign, unassigning its sessions and dropping its glossary.

        The sessions survive with ``campaign_id`` NULL, which is a state the
        table has always allowed and the History page has always rendered.
        The glossary goes because it is the campaign's - the always-on
        ``_default`` list is a different row and is never touched here - and
        the campaign's documents go with the row through the foreign key.
        """
        conn = self._db.connection
        await conn.execute(
            "UPDATE sessions SET campaign_id = NULL WHERE campaign_id = ?;", (campaign_id,)
        )
        await conn.execute("DELETE FROM glossaries WHERE campaign_id = ?;", (campaign_id,))
        await conn.execute("DELETE FROM campaigns WHERE id = ?;", (campaign_id,))
        await conn.commit()

    async def assign(self, session_id: str, campaign_id: str | None) -> None:
        """Put a session in a campaign, or take it out of one (None)."""
        await self._db.connection.execute(
            "UPDATE sessions SET campaign_id = ? WHERE id = ?;", (campaign_id, session_id)
        )
        await self._db.connection.commit()


class DocumentRepository:
    """Generated texts about a session or a campaign, one row per kind.

    A recap, an extraction and a "previously on" are the same thing three
    times: a body a model wrote, the provider and model that wrote it, and
    when. They share a table (see migration v22) so the fourth kind is a new
    string rather than a new table, a new repository and a new route.

    One document per (subject, kind): writing a recap replaces the recap. That
    is the same rule the summary column has always had, and it is what the UI
    shows - a session has *a* recap, not a pile of them, and the one worth
    keeping is the one written last.
    """

    def __init__(self, db: Database) -> None:
        self._db = db

    async def put_session_document(self, document: SessionDocument) -> None:
        await self._db.connection.execute(
            """
            INSERT INTO session_documents
                (session_id, kind, body, provider_id, model, version, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(session_id, kind) DO UPDATE SET
                body=excluded.body, provider_id=excluded.provider_id,
                model=excluded.model, version=excluded.version,
                created_at=excluded.created_at;
            """,
            (
                document.session_id,
                document.kind,
                document.body,
                document.provider_id,
                document.model,
                document.version,
                document.created_at,
            ),
        )
        await self._db.connection.commit()

    async def get_session_document(self, session_id: str, kind: str) -> SessionDocument | None:
        async with self._db.connection.execute(
            "SELECT * FROM session_documents WHERE session_id = ? AND kind = ?;",
            (session_id, kind),
        ) as cur:
            row = await cur.fetchone()
        return _row_to_session_document(row) if row is not None else None

    async def for_session(self, session_id: str) -> list[SessionDocument]:
        async with self._db.connection.execute(
            "SELECT * FROM session_documents WHERE session_id = ? ORDER BY kind;",
            (session_id,),
        ) as cur:
            rows = await cur.fetchall()
        return [_row_to_session_document(r) for r in rows]

    async def for_campaign(
        self, campaign_id: str, kind: str | None = None
    ) -> list[SessionDocument]:
        """Every document of the campaign's sessions, oldest session first.

        Ordered by the session's start rather than by when the document was
        written, because that is the order a campaign is read in: the recaps of
        sessions one through nine are a story, and the order they happened to
        be generated in is not.
        """
        sql = """
            SELECT d.* FROM session_documents d
            JOIN sessions s ON s.id = d.session_id
            WHERE s.campaign_id = ?
        """
        params: list[object] = [campaign_id]
        if kind is not None:
            sql += " AND d.kind = ?"
            params.append(kind)
        sql += " ORDER BY s.started_at;"
        async with self._db.connection.execute(sql, tuple(params)) as cur:
            rows = await cur.fetchall()
        return [_row_to_session_document(r) for r in rows]

    async def put_campaign_document(self, document: CampaignDocument) -> None:
        await self._db.connection.execute(
            """
            INSERT INTO campaign_documents
                (campaign_id, kind, body, provider_id, model, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(campaign_id, kind) DO UPDATE SET
                body=excluded.body, provider_id=excluded.provider_id,
                model=excluded.model, created_at=excluded.created_at;
            """,
            (
                document.campaign_id,
                document.kind,
                document.body,
                document.provider_id,
                document.model,
                document.created_at,
            ),
        )
        await self._db.connection.commit()

    async def get_campaign_document(self, campaign_id: str, kind: str) -> CampaignDocument | None:
        async with self._db.connection.execute(
            "SELECT * FROM campaign_documents WHERE campaign_id = ? AND kind = ?;",
            (campaign_id, kind),
        ) as cur:
            row = await cur.fetchone()
        return _row_to_campaign_document(row) if row is not None else None


# How many characters of context the snippet carries around a hit, per side,
# in the LIKE fallback. FTS5's own snippet() is told in tokens instead, below.
_SNIPPET_CONTEXT_CHARS = 60
_SNIPPET_TOKENS = 12


class SearchRepository:
    """Find a line somebody said, across one campaign or across all of them.

    Two implementations of one question, chosen by whether this SQLite build
    has FTS5: the index, which ranks by ``bm25`` and marks the hits with
    ``snippet()``, and a ``LIKE`` scan, which cannot rank and returns the
    newest sessions first instead. The caller cannot tell them apart from the
    rows: both come back as :class:`~loreline.models.SearchHit` with the
    matched words wrapped in brackets. What differs is speed on a large
    library and the quality of the ordering, which is a worse search rather
    than a broken page - see :attr:`Database.fts5`.

    Both read only settled, non-gap rows. An interim is a guess the vendor was
    still revising and a gap marker is the app saying it lost the audio, and
    searching a transcript should never land a reader on either.
    """

    def __init__(self, db: Database) -> None:
        self._db = db

    async def search(
        self, query: str, campaign_id: str | None = None, limit: int = 50
    ) -> list[SearchHit]:
        terms = _search_terms(query)
        if not terms:
            return []
        if self._db.fts5:
            return await self._search_fts(terms, campaign_id, limit)
        return await self._search_like(terms, campaign_id, limit)

    async def _search_fts(
        self, terms: list[str], campaign_id: str | None, limit: int
    ) -> list[SearchHit]:
        # Every term quoted and ANDed. The box takes what a person types, and
        # FTS5's query language reads "AND", "OR", "NOT", ":", "*", "(" and "-"
        # as syntax: an unbalanced bracket in a search for a spell name would
        # come back as a 500 rather than as no results.
        match = " ".join(terms)
        sql = f"""
            SELECT
                seg.session_id AS session_id,
                seg.source AS source,
                seg.speaker AS speaker,
                seg.start_ts AS start_ts,
                s.started_at AS started_at,
                s.campaign_id AS campaign_id,
                snippet(transcript_fts, 0, '[', ']', '…', {_SNIPPET_TOKENS}) AS snippet
            FROM transcript_fts
            JOIN transcript_segments seg ON seg.id = transcript_fts.rowid
            JOIN sessions s ON s.id = seg.session_id
            WHERE transcript_fts MATCH ?
              AND seg.is_final = 1
              AND seg.source != ?
              {"AND s.campaign_id = ?" if campaign_id else ""}
            ORDER BY bm25(transcript_fts)
            LIMIT ?;
        """
        params: list[object] = [match, GAP_SOURCE]
        if campaign_id:
            params.append(campaign_id)
        params.append(limit)
        async with self._db.connection.execute(sql, tuple(params)) as cur:
            rows = await cur.fetchall()
        return [_row_to_hit(r, r["snippet"]) for r in rows]

    async def _search_like(
        self, terms: list[str], campaign_id: str | None, limit: int
    ) -> list[SearchHit]:
        # One LIKE per term, ANDed, so a two-word search means both words
        # somewhere in the line - the same thing the indexed path means by it.
        # Newest first, because there is no relevance to sort by and the
        # session you are looking for is usually the last one you played.
        needles = [t.strip('"').replace('""', '"') for t in terms]
        conditions = " AND ".join("seg.text LIKE ? ESCAPE '\\'" for _ in needles)
        sql = f"""
            SELECT
                seg.session_id AS session_id,
                seg.source AS source,
                seg.speaker AS speaker,
                seg.start_ts AS start_ts,
                seg.text AS text,
                s.started_at AS started_at,
                s.campaign_id AS campaign_id
            FROM transcript_segments seg
            JOIN sessions s ON s.id = seg.session_id
            WHERE {conditions}
              AND seg.is_final = 1
              AND seg.source != ?
              {"AND s.campaign_id = ?" if campaign_id else ""}
            ORDER BY s.started_at DESC, seg.start_ts
            LIMIT ?;
        """
        params: list[object] = [f"%{_like_escape(n)}%" for n in needles]
        params.append(GAP_SOURCE)
        if campaign_id:
            params.append(campaign_id)
        params.append(limit)
        async with self._db.connection.execute(sql, tuple(params)) as cur:
            rows = await cur.fetchall()
        return [_row_to_hit(r, _snippet(r["text"], needles)) for r in rows]


def _search_terms(query: str) -> list[str]:
    """The query as FTS5 string literals: one quoted token per word.

    Quoting is what keeps a search box a search box. Unquoted, ``NOT`` is an
    operator, ``d&d`` is a syntax error and ``(the`` is an unbalanced bracket,
    and each of those is a 500 on a page where the user only mistyped.
    """
    return [f'"{word.replace(chr(34), chr(34) * 2)}"' for word in query.split() if word.strip()]


def _like_escape(value: str) -> str:
    """Escape the wildcards, so a search for "50%" is not a search for anything."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _snippet(text: str, needles: list[str]) -> str:
    """The matched text with the hits bracketed, trimmed around the first one.

    The fallback's answer to FTS5's ``snippet()``. It has to agree with it on
    the shape - brackets around the hit, an ellipsis where the line was cut -
    because the browser renders one thing and cannot be told which path
    produced it.
    """
    marked = text
    for needle in needles:
        lowered = marked.lower()
        target = needle.lower()
        start = 0
        rebuilt: list[str] = []
        while True:
            found = lowered.find(target, start)
            if found < 0:
                rebuilt.append(marked[start:])
                break
            rebuilt.append(marked[start:found])
            rebuilt.append(f"[{marked[found : found + len(needle)]}]")
            start = found + len(needle)
        marked = "".join(rebuilt)
        lowered = marked.lower()
    first = marked.find("[")
    if first < 0:
        return marked
    begin = max(0, first - _SNIPPET_CONTEXT_CHARS)
    end = min(len(marked), first + _SNIPPET_CONTEXT_CHARS * 2)
    return ("…" if begin else "") + marked[begin:end] + ("…" if end < len(marked) else "")


def _hit_version(source: str) -> str:
    """Which transcript version a matched row belongs to, as ``?v=`` spells it.

    A row is tagged with the job that produced it, and the session page takes
    the bare version id. The diarized copy of a version answers to the version
    it relabelled, which is what makes a hit in a diarized transcript open the
    same selection the reader would have made by hand.
    """
    if source.startswith(REPROCESS_SOURCE_PREFIX):
        return source[len(REPROCESS_SOURCE_PREFIX) :]
    if source.startswith(DIARIZE_SOURCE_PREFIX):
        return source[len(DIARIZE_SOURCE_PREFIX) :]
    return ORIGINAL_VERSION


def _row_to_hit(row: aiosqlite.Row, snippet: str) -> SearchHit:
    return SearchHit(
        session_id=row["session_id"],
        started_at=row["started_at"],
        campaign_id=row["campaign_id"],
        version=_hit_version(row["source"]),
        speaker=row["speaker"],
        start_ts=row["start_ts"],
        snippet=snippet,
    )


def _row_to_campaign(row: aiosqlite.Row) -> Campaign:
    return Campaign(
        id=row["id"],
        name=row["name"],
        created_at=row["created_at"],
        notes=row["notes"],
        recap_prompt=row["recap_prompt"],
    )


def _row_to_session_document(row: aiosqlite.Row) -> SessionDocument:
    return SessionDocument(
        session_id=row["session_id"],
        kind=row["kind"],
        body=row["body"],
        provider_id=row["provider_id"],
        model=row["model"],
        version=row["version"],
        created_at=row["created_at"],
    )


def _row_to_campaign_document(row: aiosqlite.Row) -> CampaignDocument:
    return CampaignDocument(
        campaign_id=row["campaign_id"],
        kind=row["kind"],
        body=row["body"],
        provider_id=row["provider_id"],
        model=row["model"],
        created_at=row["created_at"],
    )
