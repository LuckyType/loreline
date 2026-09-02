"""SQLite database connection + schema migrations (aiosqlite)."""

from __future__ import annotations

from pathlib import Path
from types import TracebackType
from typing import Self

import aiosqlite

from loreline.logging import get_logger

log = get_logger(__name__)

# Ordered, append-only migration list. Each entry is a full SQL script applied
# once; the applied version is tracked in ``schema_version``. Never edit a
# migration that has shipped - add a new one.
MIGRATIONS: list[str] = [
    # v1 - initial schema
    """
    CREATE TABLE providers (
        id            TEXT PRIMARY KEY,
        name          TEXT NOT NULL,
        kind          TEXT NOT NULL,
        base_url      TEXT,
        auth_ref      TEXT,
        protocol      TEXT NOT NULL,
        model         TEXT,
        sample_rate   INTEGER NOT NULL DEFAULT 16000,
        capabilities  TEXT NOT NULL DEFAULT '{}',
        enabled       INTEGER NOT NULL DEFAULT 1
    );

    CREATE TABLE glossaries (
        campaign_id   TEXT PRIMARY KEY,
        terms         TEXT NOT NULL DEFAULT '[]'
    );

    CREATE TABLE sessions (
        id                TEXT PRIMARY KEY,
        status            TEXT NOT NULL,
        started_at        REAL NOT NULL,
        ended_at          REAL,
        campaign_id       TEXT,
        primary_provider  TEXT,
        fallback_provider TEXT,
        diarization       TEXT NOT NULL DEFAULT '{}',
        audio_path        TEXT
    );

    CREATE TABLE transcript_segments (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id  TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
        source      TEXT NOT NULL,
        text        TEXT NOT NULL,
        speaker     TEXT,
        start_ts    REAL NOT NULL,
        end_ts      REAL NOT NULL,
        is_final    INTEGER NOT NULL DEFAULT 0,
        words       TEXT NOT NULL DEFAULT '[]',
        created_at  REAL NOT NULL
    );
    CREATE INDEX idx_segments_session ON transcript_segments(session_id, start_ts);

    CREATE TABLE kv_settings (
        key    TEXT PRIMARY KEY,
        value  TEXT NOT NULL
    );
    """,
    # v2 - re-processing jobs
    """
    CREATE TABLE reprocess_jobs (
        id              TEXT PRIMARY KEY,
        session_id      TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
        provider_id     TEXT NOT NULL,
        diarization     TEXT NOT NULL DEFAULT '{}',
        status          TEXT NOT NULL,
        created_at      REAL NOT NULL,
        started_at      REAL,
        finished_at     REAL,
        segments_added  INTEGER NOT NULL DEFAULT 0,
        error           TEXT
    );
    CREATE INDEX idx_reprocess_session ON reprocess_jobs(session_id, created_at);
    """,
    # v3 - per-provider transcription language
    """
    ALTER TABLE providers ADD COLUMN language TEXT NOT NULL DEFAULT 'de';
    """,
    # v4 - monotonic anchor for the session clock (wall-clock derivation)
    """
    ALTER TABLE sessions ADD COLUMN started_mono REAL NOT NULL DEFAULT 0;
    """,
    # v5 - re-processing operation kind (transcribe | diarize)
    """
    ALTER TABLE reprocess_jobs ADD COLUMN operation TEXT NOT NULL DEFAULT 'transcribe';
    """,
    # v6 - per-session speaker rename map ({original_label: display_name}, JSON)
    """
    ALTER TABLE sessions ADD COLUMN speaker_names TEXT NOT NULL DEFAULT '{}';
    """,
    # v7 - per-provider favorite models (JSON list), chosen from the live model list
    """
    ALTER TABLE providers ADD COLUMN favorite_models TEXT NOT NULL DEFAULT '[]';
    """,
    # v8 - on-demand LLM session summary
    """
    ALTER TABLE sessions ADD COLUMN summary TEXT;
    """,
    # v9 - transcript versions: record the model a re-transcription job ran
    # with and the version a diarize job targets; store the summary's
    # provider/model; move segment tags to the per-version scheme
    # (reprocess:<job_id>, diarize:<version>) and relink legacy
    # provider-tagged reprocess rows to the newest done job that made them.
    """
    ALTER TABLE reprocess_jobs ADD COLUMN model TEXT;
    ALTER TABLE reprocess_jobs ADD COLUMN target TEXT NOT NULL DEFAULT 'original';
    ALTER TABLE sessions ADD COLUMN summary_provider TEXT;
    ALTER TABLE sessions ADD COLUMN summary_model TEXT;
    UPDATE transcript_segments SET source = 'diarize:original' WHERE source = 'diarize';
    UPDATE transcript_segments SET source = 'reprocess:' || (
        SELECT j.id FROM reprocess_jobs j
        WHERE j.session_id = transcript_segments.session_id
          AND j.operation = 'transcribe'
          AND j.status = 'done'
          AND 'reprocess:' || j.provider_id = transcript_segments.source
        ORDER BY j.created_at DESC LIMIT 1
    )
    WHERE source LIKE 'reprocess:%'
      AND EXISTS (
        SELECT 1 FROM reprocess_jobs j
        WHERE j.session_id = transcript_segments.session_id
          AND j.operation = 'transcribe'
          AND j.status = 'done'
          AND 'reprocess:' || j.provider_id = transcript_segments.source
      );
    """,
    # v10 - on-demand video generation jobs (OpenRouter /videos), one row per
    # generation. `remote_id` is the upstream job handle we poll; `video_path`
    # is set once the finished bytes are stored locally.
    """
    CREATE TABLE video_jobs (
        id              TEXT PRIMARY KEY,
        session_id      TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
        provider_id     TEXT NOT NULL,
        model           TEXT NOT NULL,
        prompt          TEXT NOT NULL,
        duration        INTEGER,
        resolution      TEXT,
        aspect_ratio    TEXT,
        generate_audio  INTEGER NOT NULL DEFAULT 0,
        seed            INTEGER,
        status          TEXT NOT NULL,
        remote_id       TEXT,
        video_path      TEXT,
        created_at      REAL NOT NULL,
        started_at      REAL,
        finished_at     REAL,
        error           TEXT
    );
    """,
    # v11 - drop the Google Cloud STT v2 (gRPC) provider kind. Its rows must go
    # with it: ProviderKind no longer has a "google" member, so a surviving row
    # would raise on every provider read. The Gemini kind covers Google
    # transcription with a plain API key and no service-account setup.
    # (An orphaned `provider:<id>` entry may remain in secrets.json; harmless,
    # and the DB migration has no reach into that file.)
    """
    DELETE FROM providers WHERE kind = 'google';
    """,
]


class Database:
    """Async SQLite wrapper with migration support."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._conn: aiosqlite.Connection | None = None

    @property
    def connection(self) -> aiosqlite.Connection:
        if self._conn is None:  # pragma: no cover - misuse guard
            raise RuntimeError("Database not connected. Call connect() first.")
        return self._conn

    async def connect(self) -> None:
        """Open the connection, enable pragmas, and run migrations."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self._path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL;")
        await self._conn.execute("PRAGMA foreign_keys=ON;")
        await self._migrate()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def _migrate(self) -> None:
        conn = self.connection
        await conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);")
        async with conn.execute("SELECT MAX(version) FROM schema_version;") as cur:
            row = await cur.fetchone()
        current: int = row[0] if row is not None and row[0] is not None else 0

        for version in range(current + 1, len(MIGRATIONS) + 1):
            log.info("db.migrate", version=version)
            await conn.executescript(MIGRATIONS[version - 1])
            await conn.execute("INSERT INTO schema_version (version) VALUES (?);", (version,))
            await conn.commit()

    async def __aenter__(self) -> Self:
        await self.connect()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()
