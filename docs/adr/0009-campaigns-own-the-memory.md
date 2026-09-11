---
status: accepted
date: 2026-09-11
---

# Campaigns own the memory a transcript becomes

## Context

`sessions.campaign_id` has existed since the first schema and nothing ever set
it. No table listed the campaigns, so the id was a free string: the History
page's Campaign column printed it raw and, in practice, printed a dash for
every row; the per-campaign glossary under `/api/glossary/{campaign_id}` was
reachable only by typing an id into the URL; and `GlossaryRepository.get_effective`
merged a campaign's terms for a campaign no UI could name.

What that cost is not tidiness. A four-hour transcript is a file nobody reads.
The value in it is a recap the players get during the week, the names that came
up, an answer to "who was the burgomaster of Vallaki again" that does not mean
scrubbing through audio, and something to read aloud before the next session
starts. All four of those are questions about a campaign, and there was no
campaign to ask.

## Decision

* **A campaign is a row, not a string.** `campaigns(id, name UNIQUE, created_at,
  notes, recap_prompt)`. Sessions keep the nullable `campaign_id` they always
  had and it is deliberately not made a foreign key: rows written before this
  carry whatever string was in them. Deleting a campaign unassigns its sessions
  and drops its glossary, and never deletes a session. A campaign is a label on
  recordings that already exist, and removing the label must not be able to
  destroy audio nobody can capture again.
* **Generated texts are documents.** `session_documents(session_id, kind, body,
  provider_id, model, version, created_at)` holds the kinds `recap` and
  `extraction`; `campaign_documents` holds the campaign-level `previously_on`.
  One row per subject and kind, so writing a recap replaces the recap. The
  three are one table because they are one shape - a body, what wrote it, when -
  and the fourth kind should be a new string rather than a new table with its
  own repository and its own routes.
* **A recap is not a summary.** They are two texts about one session because
  they are for two readers: a summary is the GM's index of what happened, a
  recap is what the table is told a week later. Which instructions run is the
  campaign's `recap_prompt`, else the stored `action_defaults.recap_prompt`,
  else the built-in text, each blank level deferring to the one above it.
* **Extraction is asked for as JSON and read leniently.** The prompt asks for a
  single object; the parser strips a code fence, takes the outermost braces,
  validates with pydantic, and retries once with the complaint appended. A
  model told exactly what it got wrong usually fixes it, and failing outright
  would charge for a run that produced nothing.
* **Search is SQLite FTS5 over the segment text**, an external content table
  over `transcript_segments` with insert, update and delete triggers and a
  backfill, so a transcript row stays the single place a segment's words live.
  Every row is indexed and the reader filters interims and gap markers out: a
  streaming turn crosses `is_final` mid-session, so indexing selectively would
  fire the delete trigger for rows that were never in the index.
* **No FTS5 is a worse search, not a broken page.** The build is probed at
  startup by creating a temp virtual table; without it the migration is skipped
  (and still recorded, because the list is index-based and a version left
  unapplied would block every one after it), and `SearchRepository` answers with
  a `LIKE` scan, newest first, marking the hits the same way so the browser
  cannot tell which path ran. `SearchResults.indexed` says which it was, and the
  page says so under the box rather than presenting an unranked list as a ranked
  one. If FTS5 later turns up, `Database._ensure_search_index` builds the index
  that the skipped migration would have.

## Consequences

* The glossary editor is one component now (`GlossaryEditor.svelte`), used by
  Settings for the always-on `_default` list and by a campaign page for its own
  terms, keeping the load gating and the clear confirm the settings page had.
  Extracted names go into it through `POST /api/campaigns/{id}/glossary/add`,
  which appends and deduplicates case-insensitively rather than replacing: the
  gesture on that list is "and these too", and a request that could drop terms
  typed by hand would make the list dangerous to use.
* Search results are deep links (`?v=<version>&t=<start_ts>`), so the session
  page now selects a version and scrolls to a row from its query string. A hit
  in a diarized copy resolves to the version it relabelled, which is the
  selection a reader would have made by hand.
* A session's summary, recap and extraction can each name a different transcript
  version. That is the honest state of a session with five versions, and every
  one of them says which it read.
* The stored answer of an extraction is a JSON body rather than columns, so a
  change to the schema leaves old documents unreadable. The campaign merge skips
  a body that no longer parses instead of failing the page: it is one session's
  names, and the other eight are still worth reading.

## Follow-ups, deliberately not in this change

* Moving `sessions.summary` and its three companion columns into
  `session_documents`. Nothing else reads them, so the migration buys clarity
  and no capability, and it is better done when a second reader appears.
* Sharing a recap outside the app: a Discord post, a public link.
* Per-player views, where a recap is written for one character's knowledge
  rather than for the table's.
