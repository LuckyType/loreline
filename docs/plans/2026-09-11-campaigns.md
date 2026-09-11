# Plan: the campaign layer

Goal: a session belongs to a campaign, and the campaign is where the value of
a transcript is collected: its glossary, its sessions in order, search across
all of them, a player-facing recap per session, structured extraction of the
names that appeared, and a "previously on" for the next session. A four hour
transcript nobody reads becomes a campaign memory people use.

What exists today: `sessions.campaign_id` (nullable, never set by the UI),
per-campaign glossaries in the `glossaries` table with routes under
`/api/glossary/{campaign_id}` and `GlossaryRepository.get_effective`, which
merges the `_default` list with a campaign's terms. No campaigns table, no UI,
the History page's Campaign column is always "-". Summaries live on the
session row (`summary`, `summary_provider`, `summary_model`,
`summary_version`) and are produced by `summarize_transcript` in
`src/loreline/llm.py` with `DEFAULT_SYSTEM_PROMPT` and an overridable
`summarize_prompt` in `ActionDefaults`.

## Design decisions (write them as ADR 0009)

1. **A campaign is a row, not a string.** `campaigns(id, name UNIQUE,
   created_at, notes, recap_prompt)`. Sessions keep `campaign_id`; unassigned
   sessions stay valid. Deleting a campaign unassigns its sessions and deletes
   its glossary; it never deletes sessions.
2. **Generated texts are documents.** A `session_documents` table
   (`session_id, kind, body, provider_id, model, version, created_at`) holds
   the new kinds `recap` and `extraction` (and `campaign_documents` holds the
   campaign level `previously_on`). The existing summary columns stay as they
   are in this pass; migrating them into documents is a noted follow-up. One
   table for every future generated text is the extensible seam.
3. **Search is SQLite FTS5 over the segment text**, external content table
   with triggers, so the transcript rows stay the single source of truth.

## Backend

1. **Migrations** in `src/loreline/persistence/database.py`: the import
   work adds v22 in parallel; write yours as the entries after it, and on
   merge make sure the list order equals the version order. One migration for
   `campaigns` and the documents tables, one for the FTS5 virtual table with
   insert, update and delete triggers on `transcript_segments` and a backfill
   of existing rows. At startup, detect whether the SQLite build has FTS5
   (`PRAGMA compile_options` or a try on `CREATE VIRTUAL TABLE`); if not, log
   it once and let search fall back to `LIKE`.
2. **Repositories**: `CampaignRepository` (create, get, list with session
   count and last session date, update, delete with unassign),
   `DocumentRepository` (put and get by session and kind, list for a
   campaign), `SearchRepository.search(q, campaign_id, limit)` returning
   session id, started at, source or version, speaker, `start_ts`, and a
   snippet with FTS5 `snippet()` highlights, ranked by `bm25`, restricted to
   final rows.
3. **Routes**: `GET/POST /api/campaigns`, `GET/PUT/DELETE
   /api/campaigns/{id}`, `PUT /api/sessions/{id}/campaign`, `GET
   /api/search?q=&campaign_id=&limit=`. The session start request (find the
   schema behind `api.startSession` in `frontend/src/lib/api.ts` and
   `src/loreline/web/schemas.py`) gains `campaign_id`; `ActionDefaults` gains
   `campaign_id` so the capture card remembers the last campaign; the import
   endpoint, if it has landed, already accepts `campaign_id`.
4. **Recap**: `POST /api/sessions/{id}/recap` with the same body shape as
   summarize (provider, model, version, reasoning effort). Reuses
   `summarize_transcript` with a recap system prompt: written for the
   players, past tense, in the transcript's language, covering what happened
   and what is open, no meta commentary, a length target; overridable per
   campaign via `campaigns.recap_prompt` and globally via a new
   `recap_prompt` in `ActionDefaults`. Stored as a document of kind `recap`.
5. **Extraction**: `POST /api/sessions/{id}/extract`, same body. A prompt that
   asks for JSON only, with the schema `{characters: [{name, kind: "pc" |
   "npc", notes}], places: [{name, notes}], items: [{name, notes}], factions:
   [{name, notes}], quests: [{title, status, notes}], decisions: [string]}`.
   Parse leniently (strip code fences, find the outermost braces), validate
   with a pydantic model, retry once with the validation error appended if it
   fails. Stored as a document of kind `extraction`. `GET
   /api/campaigns/{id}/entities` merges the extractions of all the campaign's
   sessions by normalised name (latest notes win, sessions listed per entity).
   `POST /api/campaigns/{id}/glossary/add` appends chosen names to the
   campaign glossary, deduplicated and trimmed, through the existing
   glossary repository.
6. **Previously on**: `POST /api/campaigns/{id}/previously-on` takes the last
   N (default 3) sessions' recaps, falling back to summaries, and produces a
   short text for the start of the next session. Stored as a campaign
   document. Lowest priority in this plan; do it last.

## Frontend

1. **Navigation**: add "Campaigns" to the sidebar between History and
   Settings in `frontend/src/routes/+layout.svelte`.
2. **Capture card** (`frontend/src/lib/CaptureControls.svelte`): a campaign
   picker (existing `Dropdown`) with an inline "New campaign" entry that
   creates one by name, remembered through `ActionDefaults.campaign_id`. Sent
   on start.
3. **History page**: the Campaign column shows the name as a link; a campaign
   filter above the table; unassigned sessions read "No campaign". If the
   import dialog exists on the branch when you merge, give it the same picker.
4. **Session page**: the header meta shows the campaign with a "Change"
   action (a small dialog with the picker). The summary section's menu gains
   "Write recap" and "Extract names"; generalise `SummarizeDialog.svelte` with
   a `kind` prop so all three share one dialog. Recap renders through
   `Markdown.svelte` with a Copy button. Extraction renders as grouped lists
   with checkboxes and an "Add to campaign glossary" button.
5. **Campaigns pages**: `/campaigns` lists campaigns (name, sessions, last
   session, a New button). `/campaigns/[id]` has: the session list in order
   with recap status; a search box whose results link to the session page
   with `?v=<version>&t=<start_ts>` (add that deep link handling to the
   session page: select the version and scroll the transcript to the row,
   highlighted); the glossary editor, extracted from
   `frontend/src/routes/settings/glossary/+page.svelte` into a shared
   `GlossaryEditor.svelte` that both the settings page (for `_default`) and
   the campaign page use, keeping the load gating and clear confirm that
   page already has; the merged entity list with "Add to glossary"; the
   previously-on text with a Generate button; rename, notes and recap prompt
   in a settings block; Delete with a confirm that says sessions are kept.

## Tests

- Migrations: fresh database and upgrade from the previous version, FTS
  backfill.
- Repositories: campaign CRUD, delete unassigns, documents put and get,
  search ranking and snippet, the LIKE fallback path.
- Routes: every new route, with the fake chat completion the existing
  `tests/integration/test_llm.py` patterns use for recap and extraction,
  including the invalid JSON retry.
- Session start with a campaign id, and the effective glossary reaching the
  provider for a campaign session (extend the existing glossary tests).

## Out of scope, note as follow-ups in the ADR

- Moving the summary columns into the documents table.
- Sharing a recap outside the app (Discord post, public link).
- Per-player views.
