"""Repositories: typed CRUD over the SQLite tables."""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING

import aiosqlite

from loreline.models import (
    DEFAULT_GLOSSARY_CAMPAIGN,
    DiarizationConfig,
    Glossary,
    JobStatus,
    OpenRouterRouting,
    Protocol,
    ProviderCaps,
    ProviderConfig,
    ProviderKind,
    ReprocessJob,
    Session,
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
                (id, name, kind, base_url, auth_ref, protocol, model, sample_rate,
                 language, capabilities, enabled, favorite_models, routing)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                name=excluded.name, kind=excluded.kind, base_url=excluded.base_url,
                auth_ref=excluded.auth_ref, protocol=excluded.protocol,
                model=excluded.model, sample_rate=excluded.sample_rate,
                language=excluded.language, capabilities=excluded.capabilities,
                enabled=excluded.enabled, favorite_models=excluded.favorite_models,
                routing=excluded.routing;
            """,
            (
                provider.id,
                provider.name,
                provider.kind.value,
                provider.base_url,
                provider.auth_ref,
                provider.protocol.value,
                provider.model,
                provider.sample_rate,
                provider.language,
                provider.capabilities.model_dump_json(),
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
                 primary_provider, fallback_provider, diarization, audio_path)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
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

    async def mark_interrupted(self) -> None:
        """Fail sessions left CAPTURING by a previous process (startup sweep).

        ``SessionManager`` only ever transitions a session out of CAPTURING
        from inside ``stop()`` - the in-memory runtime that would call it lives
        only as long as the process does. If the process dies uncleanly
        (crash, ``kill -9``, OOM, power loss) mid-capture, that row is stuck
        at CAPTURING forever with no ``ended_at``: nothing else revisits it,
        so it just sits in the history list looking like a session that's
        eternally still recording. Mirrors ``ReprocessRepository.mark_interrupted``.
        """
        await self._db.connection.execute(
            "UPDATE sessions SET status = ?, ended_at = ? WHERE status = ?;",
            (SessionStatus.ERROR.value, time.time(), SessionStatus.CAPTURING.value),
        )
        await self._db.connection.commit()

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
        self, session_id: str, summary: str, *, provider_id: str, model: str
    ) -> None:
        """Persist the LLM-generated session summary and what produced it."""
        await self._db.connection.execute(
            "UPDATE sessions SET summary = ?, summary_provider = ?, summary_model = ? "
            "WHERE id = ?;",
            (summary, provider_id, model, session_id),
        )
        await self._db.connection.commit()


class TranscriptRepository:
    """Append + read transcript segments."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def add(self, event: TranscriptEvent) -> None:
        words_json = json.dumps([w.model_dump() for w in event.words])
        await self._db.connection.execute(
            """
            INSERT INTO transcript_segments
                (session_id, source, text, speaker, start_ts, end_ts, is_final,
                 words, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
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
            ),
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
                 error)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
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
                segments_added = ?, error = ?
            WHERE id = ?;
            """,
            (
                job.status.value,
                job.started_at,
                job.finished_at,
                job.segments_added,
                job.error,
                job.id,
            ),
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
        protocol=Protocol(row["protocol"]),
        model=row["model"],
        sample_rate=row["sample_rate"],
        language=row["language"],
        capabilities=ProviderCaps.model_validate_json(row["capabilities"]),
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
