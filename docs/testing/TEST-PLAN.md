# Loreline UI test plan

Built by reading the source file by file. Each section below is appended after
one file is analysed, and lists the browser level steps that file implies.
Target under test: http://10.10.50.55/ (same version as this checkout).

Legend: `[neg]` negative or error path, `[api-only]` not reachable from the UI,
`[obs]` observation only, no click.

---

## F01 `frontend/src/routes/+layout.ts` and `+layout.svelte` (app shell)

What it does: SPA only (`ssr = false`), no prerender. Shell renders header,
sidebar nav (Dashboard `/`, History `/sessions`, Settings `/settings`), a health
dot polled every 5s via `api.health()`, a hover health popover, a Logout button,
and a global ConfirmDialog. `/login` renders bare with no shell. A 401 from the
health poll flips `authed` false and redirects to `/login`. Sidebar collapse is
persisted in `localStorage` under `loreline.nav-collapsed`; below the `sm`
breakpoint the sidebar becomes an overlay dropdown closed by Escape or by
clicking the scrim. Capability load failure shows an amber banner with Retry.

Steps:
1. Load `/` while logged out, expect redirect to `/login`, no shell chrome.
2. Log in, land on Dashboard, confirm header shows `Loreline` plus a version
   string next to it.
3. Click the health dot, confirm it refetches (dot stays green, no error).
4. Hover the health dot, read the popover: Client (Service, Version, Capture,
   Uptime, Disk free as `x.x GiB / y.y GiB`, Alerts on/off), Transcription
   (Transcript stream dot + label), Logs (Log stream dot + label).
5. Confirm Uptime renders as `Hh Mm Ss` and increases across two hovers.
6. Click each nav item, confirm the active item is highlighted and that
   `/settings/...` subpages keep Settings highlighted (prefix match).
7. Toggle the sidebar with the PanelLeft button, reload the page, confirm the
   collapsed state survived (localStorage).
8. Resize to a phone width, confirm the sidebar becomes a hamburger overlay;
   open it, press Escape to close; open it, click the scrim to close; open it,
   click a nav item and confirm it navigates and closes.
9. `[obs]` Start a capture, confirm the header shows a `Capturing…` badge with a
   green dot, and that it disappears when the capture stops.
10. Click Logout, confirm redirect to `/login` and that going back to `/` does
    not show data.
11. `[neg]` Break the capabilities fetch (offline the box or block
    `/api/capabilities`), confirm the amber banner appears with a working Retry.

---

## F02 `frontend/src/routes/login/+page.svelte`

What it does: one password field (`type=password`, `autocomplete=current-password`),
Sign in button that shows `Signing in…` and disables while busy, an inline
destructive error paragraph from the server `detail`, and `goto('/')` on success.

Steps:
1. Submit with an empty password `[neg]`, note the exact server message.
2. Submit a wrong password `[neg]`, confirm the inline error text and that the
   button re-enables afterwards.
3. Submit the correct password, confirm redirect to `/`.
4. Press Enter in the field rather than clicking, confirm it submits (form
   `onsubmit`).
5. `[obs]` Confirm the button label switches to `Signing in…` and is disabled
   during the request.
6. After logging in, navigate to `/login` directly, confirm what happens (no
   redirect guard exists in the code, so the form should render again).

---

## F03 `frontend/src/lib/api.ts`, `stores.ts`, `ws.ts` (client contracts)

What they do: `request()` sends `credentials: same-origin`, and on any 401 that
is not the login call it clears `authed` and bounces to `/login`. Errors surface
the server's `detail` field. `ws.connect()` reconnects forever with equal-jitter
exponential backoff from 1s to 30s and reports connected / reconnecting /
offline. `stores.ts` holds the shared helpers the panes render with:
`speakerColor` (deterministic HSL from the label), `fmtWhen`, `formatTime`
(`m:ss`), `audioIsEmpty` (duration <= 0.05s, message
"This session has no captured audio - the recording is empty."),
`sourceLabel` (`gap` renders as "Lost audio", `diarize*` as "Diarization",
`reprocess:<id>` as "<name> (re-run)" or "re-run <8 chars>", otherwise the
provider name, falling back to the first 8 chars of a deleted provider's id),
and `turnKey` (streaming interims collapse onto one row per `turn_id`).

Endpoint inventory to cover from the UI (each must be exercised at least once):
auth login/logout; system healthz, diarizer probe, revision, update, rollback,
defaults get/put, autostart get/put, alert channels list/create/update/delete/test,
services list/toggle/logs; audio devices list, device get/put; capabilities;
providers list/models/create/update/delete/secret/test; glossary default and
per-campaign get/put; session start/stop/list/get, transcript version get and
delete, version logs, speakers, summarize, export, audio, delete, merge;
video models/enqueue/get/list/delete/content; reprocess enqueue/get/list.

Steps:
1. `[obs]` With DevTools open, watch that every action below issues exactly the
   endpoint named in the inventory, and that no call fires twice per click.
2. Let the JWT cookie expire or delete it in DevTools, then click anything that
   fetches, confirm the app bounces to `/login` rather than rendering blank.
3. Stop the backend or pull the network, confirm the transcript and log stream
   dots go amber ("reconnecting…") and not straight to red, and that they
   recover to green on their own once the backend is back.
4. `[obs]` Confirm the same speaker label always gets the same colour across the
   dashboard, the session page and re-runs.
5. `[obs]` Confirm a transcript line whose source is a deleted provider renders
   an 8-char id prefix and not a raw 32-char hex string.

---

## F04 `frontend/src/routes/+page.svelte` (Dashboard layout)

What it does: fixed-height dock, `100vh - 104px`. CaptureControls card on top,
then the live Transcript pane (takes the leftover height) and the live Logs pane
under it at a user-dragged height, capped so the transcript keeps at least 180px.

Steps:
1. Drag the Logs pane's resize handle upward as far as it goes, confirm the
   Transcript pane never shrinks below roughly three lines plus its title bar.
2. Drag it down to the minimum, confirm the Logs pane collapses cleanly.
3. Resize the browser window while a pane is dragged tall, confirm the ceiling
   recomputes and nothing overflows the viewport.
4. `[obs]` Confirm the page itself never scrolls; only the panes scroll.

---

## F05 `frontend/src/lib/CaptureControls.svelte` (capture card)

Idle state controls: Transcription provider dropdown (list gated to providers
that can drive a live capture), Model picker, Start session button, a dashed
summary line (Fallback / Diarization / Glossary with a status dot) and an
Edit/Done toggle opening the advanced panel (Fallback provider, Fallback model,
Diarization mode, Glossary checkbox, Diarization endpoint).

Rules encoded in the file, each of which is a check:
- Provider list is `providersFor('capture')`: LLM-only vendors and OpenRouter
  transcription (batch only) must not appear.
- The model seed follows the stored default only while its provider is selected;
  switching provider resets the model.
- Start is disabled unless a provider and a model are set and nothing is blocked.
- `remote` diarization with an empty endpoint blocks Start and shows
  "Required for remote diarization - the service's base URL." under the field.
- Picking `remote` when no endpoint is stored prefills `http://diarization:8001`.
- The endpoint is probed 500ms after typing settles; an unreachable one shows
  "No diarization service answered at ..." in amber, a degraded one shows
  "... answered but cannot serve right now."; the verdict must never be shown
  against an endpoint no longer in the box.
- Inline diarization only appears for provider+model pairs that return speakers;
  otherwise the note "This model returns no speaker labels..." shows.
- With the glossary on and a model that cannot combine them, Inline stays listed
  but disabled with a reason plus "Turn the glossary off to use it."
- Switching to a model that cannot take a glossary at all unticks and disables
  the checkbox.
- A glossary that costs word timestamps shows an amber warning triangle both in
  the panel and in the collapsed summary line.
- Selecting a fallback provider without a fallback model blocks Start and the
  summary reads "<name> - model missing" in red.

Running state: green dot + `Recording` + `m:ss` elapsed + "<n> providers" +
"<m:ss> recorded" or "no audio for Ns", a live level meter fed by
`/ws/audio/live-level`, and a red Stop session button. Stop shows `Finalizing…`
with an amber dot and the note about draining the queue.

Steps:
1. Open the provider dropdown, list every entry, compare against Settings >
   Providers, confirm chat-only and batch-only vendors are absent.
2. Pick each provider in turn, confirm the model picker repopulates and the
   previously picked model does not survive the switch.
3. With no model picked, confirm the hint "Pick a model to start ..." shows and
   Start is disabled `[neg]`.
4. Open Edit, set Fallback provider without a model, confirm the summary reads
   "<name> - model missing" in red, the dot is red, and Start is disabled `[neg]`.
5. Set the fallback model, confirm the summary becomes "<name> · <model>" and
   the dot returns to green.
6. Set Diarization to Remote, clear the endpoint, confirm the inline error, the
   red dot, the summary "Remote - endpoint missing" and a disabled Start `[neg]`.
7. Re-pick Remote with the box empty, confirm `http://diarization:8001` prefills.
8. Type a bogus endpoint such as `http://10.10.50.55:9999`, wait ~1s, confirm the
   amber "No diarization service answered" line and that the collapsed summary
   also says "Remote - service not answering" `[neg]`.
9. Type quickly through several endpoints, confirm no stale verdict is ever
   painted against the value on screen.
10. Pick a model with no speaker labels, confirm Inline vanishes from the
    Diarization dropdown and the explanatory note appears; if Inline was
    selected, confirm it silently falls back to None.
11. Pick a model that conflicts glossary vs inline, confirm Inline is listed but
    greyed with a tooltip and the "Turn the glossary off" note; untick Glossary
    and confirm Inline becomes selectable.
12. Pick a model that cannot take a glossary, confirm the checkbox disables, the
    tick clears, the summary reads "Unsupported" and a reason is shown.
13. Pick a model whose glossary drops word timestamps, confirm the amber
    triangle in both the panel and the collapsed summary, with a hover label.
14. Press Start with a valid setup, confirm the card flips to Recording, the
    header shows the Capturing badge, and the elapsed clock ticks from about 0.
15. `[obs]` Watch the level meter move when there is sound, and confirm
    "<m:ss> recorded" climbs.
16. `[neg]` If the box has no working mic, confirm "no audio for Ns" appears
    within a few seconds, the dot turns red, the meter reads zero, and the long
    "No audio has reached the recorder ..." message is shown.
17. Press Stop, confirm the amber `Finalizing…` state, the draining note, and
    that the card returns to the idle form afterwards.
18. `[neg]` Press Stop when nothing is running (via a second tab that already
    stopped it), confirm the 409 path reports the ended session rather than a
    raw error.
19. `[neg]` Force a session to end in error (kill the mic mid-capture if
    possible), confirm the red "The recording stopped before it was finished..."
    banner with a working "Open the session" link and a working Dismiss.
20. `[obs]` If the STT provider fails during capture, confirm the amber
    "Live transcription has been failing since HH:MM" line, and the red
    "Live transcription stopped: <vendor message>" line for a hard failure.
21. Start a second capture from another tab while one runs `[neg]`, confirm the
    error surfaces in the card rather than silently doing nothing.

---

## F06 `LiveTranscriptPane.svelte` + `liveFeed.svelte.ts`

What they do: the pane seeds itself from the active session's stored transcript
(so returning to the Dashboard mid-session does not look like lost history),
then follows `/ws/transcript`. Buffer cap 500, oldest fall off. Items are keyed
by `turnKey`, so a streaming connector's interim revisions land on one row
instead of one row per word; a revision whose key already fell off the cap is
dropped rather than re-appended at the tail. Header controls: Filter (toggle,
Escape closes and clears), Auto-scroll (toggle, green border when on), Clear
(disabled when empty, asks for confirmation). Empty text is
"Waiting for transcript events…", or "No segments match the search." when
filtering.

Steps:
1. Start a capture, speak or play audio, confirm lines appear.
2. `[obs]` With a streaming model, confirm an interim line is dimmed and grows
   in place, then settles as final, without spawning a new row per revision.
3. Navigate to History and back to the Dashboard mid-session, confirm the pane
   re-seeds with everything said so far, not an empty list.
4. Click Filter, type a word present in the transcript, confirm only matching
   lines show and matches are highlighted; type nonsense, confirm
   "No segments match the search."
5. Press Escape in the filter box, confirm it closes and the filter clears.
6. Toggle Auto-scroll off, let new lines arrive, confirm the view stays put;
   toggle it on, confirm it jumps to the tail on the next line.
7. Click Clear with content, confirm the confirmation dialog, cancel it and
   confirm nothing was cleared, then accept it and confirm the pane empties and
   keeps filling afterwards.
8. `[obs]` Confirm Clear is disabled while the pane is empty.
9. `[obs]` If a session ever exceeds 500 segments, confirm the pane keeps the
   newest and does not reorder.

---

## F07 `LiveLogsPane.svelte` + `LogLine.svelte` + `LevelMeter.svelte`

Logs pane: `/ws/logs`, cap 1000, line count shown next to the title so a folded
pane still shows the feed climbing. The whole title bar is a drag handle
(pointer events, 6px threshold) and a tap on it folds the pane to the bar.
Height and fold state persist in `localStorage` under `loreline.logs-dock`.
Buttons: Filter, Wrap lines, Follow, Clear (disabled when empty, confirms), and
a chevron that folds. Controls on the bar must not trigger a fold, including
the disabled Clear button. Empty text differs by capture state: "No log lines
yet." while capturing, otherwise the "Logs appear here while a session is
recording..." sentence.

LogLine parses `ts [LEVEL] event k=v k=v`, colours the level (debug grey, info
sky, warning amber, error red, critical bold red), renders `k=` grey with the
value green, and falls back to the raw line when the pattern does not match.

LevelMeter: green below 0.6, amber to 0.9, red above; width is `peak * 100%`.

Steps:
1. Drag the Logs title bar up and down, confirm the pane resizes 1:1 with the
   pointer and stops at the ceiling the Transcript's minimum leaves.
2. Drag it below the minimum, confirm it folds to the bar.
3. Tap the bar without moving, confirm it folds; tap again, confirm it returns
   to the height it had.
4. Reload the page, confirm both the height and the fold state survived.
5. Click each bar control (Filter, Wrap, Follow, Clear, chevron) and confirm
   none of them folds the pane as a side effect.
6. Click the disabled Clear button while the pane is empty, confirm the pane
   does NOT fold (this was a real bug once).
7. Filter for a token such as `error`, confirm only matching lines show and the
   count next to the title reflects the filtered number.
8. Press Escape in the filter box, confirm it closes and clears.
9. Toggle Wrap lines with a very long line present, confirm it wraps and then
   goes back to a single nowrap line.
10. Toggle Follow off, confirm new lines do not scroll the view; on, confirm it
    sticks to the tail.
11. Fold the pane while following, unfold it, confirm it is parked on the newest
    line and not the oldest.
12. Clear the logs, confirm the confirm dialog, then that the count resets and
    the feed keeps filling.
13. `[obs]` Between sessions, confirm the pane shows the "Logs appear here while
    a session is recording..." sentence, not a broken-feed look.
14. `[obs]` Confirm log lines render with a coloured level column and green
    `k=v` values, and that a non-conforming line still renders verbatim.
15. `[obs]` Confirm the level meter is green at speech volume and turns amber or
    red only when loud.

---

## F08 `TranscriptList.svelte` + `transcriptSearch.ts`

Shared by the Dashboard pane and the session page. Per row: `m:ss` start time
(a button that seeks when the caller passes `onseek`, otherwise plain text), a
speaker label coloured deterministically (a button that renames in place when
the caller passes `onrename`), an optional source chip, and the text. Query
matches are wrapped in `<mark>`. Segments with source `gap` render as an amber
italic line with a dashed rule, never as speech, are always kept by the filter
and never counted as a hit. Interim segments are dimmed and italic when
`dimInterim` is on. The active (playing) row gets a tinted background and a left
bar and is scrolled into view with `block: nearest`.

In-place rename: click a speaker, an input appears prefilled with the current
name, Enter commits, Escape cancels, blur commits, and the field closes before
saving so a blur cannot double-send.

Steps:
1. On the session page, click a timestamp, confirm the player seeks there.
2. On the Dashboard pane, confirm timestamps are plain text and not clickable.
3. Click a speaker on the session page, type a new name, press Enter, confirm
   every row with that label updates.
4. Click a speaker, press Escape, confirm nothing was saved.
5. Click a speaker, type, click elsewhere (blur), confirm it saves once, not
   twice.
6. Rename to an empty string, confirm the name clears back to the raw label.
7. Search for a speaker's display name and for the raw label, confirm both
   match.
8. `[obs]` Confirm search hits are highlighted, all of them in a line, not just
   the first.
9. `[obs]` With a filter active, confirm gap markers still show.
10. `[obs]` Play the audio, confirm the current row is highlighted and follows
    along, and that it does not fight a manual scroll when paused.

---

## F09 `frontend/src/lib/Dropdown.svelte` (custom listbox, used everywhere)

Behaviour to verify once, then trusted for every dropdown in the app: opens on
click or ArrowUp/ArrowDown; popover is portalled to the body, positioned under
the trigger, min width 160px, and repositions on scroll and resize; a click
outside closes it and that click is swallowed by a shield so it does not also
activate what is underneath; Escape closes the list only, not a dialog behind
it; Tab closes and moves focus on; Enter picks the active row; Space picks when
the list is not filterable; Home/End jump to the first/last enabled row; arrow
keys do not wrap around at the ends; disabled rows are skipped by the keyboard
and unclickable but still visible with a `title` explaining why; type-ahead
matches a label prefix with a 700ms reset when the list has no filter box; a
filter box narrows the list and puts the active row on the first hit; favourites
and the default value are grouped under a "Favorites" heading with an "All"
group below, and the default row carries a `default` tag; the empty list reads
"Loading…" or "No options."

Steps:
1. Open the Dashboard's provider dropdown by clicking it, close it with Escape.
2. Focus the trigger, press ArrowDown, confirm it opens on the current value.
3. Walk with ArrowDown to the last row, press ArrowDown again, confirm it does
   not wrap to the top.
4. Press Home and End, confirm they jump to the first and last pickable row.
5. Type the first letters of an option (no filter box), confirm type-ahead
   selects it; pause a second, type a different letter, confirm the prefix
   resets.
6. Press Enter, confirm the value is picked and the popup closes with focus
   back on the trigger.
7. Open a model picker (filterable), type into the filter, confirm the list
   narrows and Enter picks the first hit.
8. Open a dropdown, scroll the page, confirm the popover follows the trigger.
9. Open a dropdown, click a button behind the shield, confirm that click only
   closes the list and does not press the button.
10. Open a dropdown inside a dialog, press Escape once, confirm the list closes
    and the dialog stays open.
11. `[obs]` Confirm the stored default row shows a `default` tag and appears
    under Favorites.
12. `[obs]` Confirm a disabled row is greyed, unclickable, skipped by arrows and
    shows a reason on hover.
13. `[obs]` Confirm a narrow trigger still shows a readable popover, not one
    wrapping every label.

---

## F10 `ModelPicker.svelte`, `modelCatalog.svelte.ts`, `modelInfo.ts`

What they do: the model list is fetched lazily on first open of the dropdown,
deduped per (provider row, interaction, refresh token) and shared across
pickers. Hidden models are never listed. The list is: stored default (if it
belongs to this provider), then favourites, then the fetched catalogue, deduped.
The right-hand hint is `"$in / $out · <context> · realtime|batch · reasoning"`
where the vendor publishes it (OpenRouter today), `free` when both prices are
zero, and empty otherwise. A tiered price shows the ladder as a tooltip. A model
with an announced sunset shows `retiring <date>` in the row and an amber
"The vendor is retiring this model on <date>." under the trigger once selected.

Steps:
1. `[obs]` Open a model picker for the first time, confirm it shows "Loading…"
   and then a populated list, and that a second open does not refetch.
2. Switch provider and back, confirm each provider gets its own list and the
   previous provider's models never appear.
3. `[obs]` For OpenRouter, confirm price and context hints render, that a
   `:free` model reads `free`, and that a tiered model shows the ladder on hover.
4. `[obs]` For a transcription picker, confirm models are hinted `realtime` or
   `batch`.
5. `[obs]` Confirm a model marked hidden in the capability config never appears
   in any picker.
6. `[obs]` Pick a deprecated model, confirm the amber retirement note under the
   picker and the `retiring <date>` hint in the row.
7. Flip Settings > Providers "Only show compatible models" and reopen a picker,
   confirm the list changes (the refresh token invalidates the cache) rather
   than staying stale.
8. `[neg]` Point a self-hosted provider at a dead base URL, open its model
   picker, confirm it shows "No options." rather than hanging on "Loading…".

---

## F11 `actionSetup.svelte.ts` + `capabilities.svelte.ts` (gating rules)

Rules worth verifying through the UI:
- Provider rows, action defaults and the capability config load together, so a
  picker never seeds to a row it then refuses to list.
- `capture` = transcribe-capable AND the kind is not marked `live_capture:
  false` (OpenRouter transcription is stored-audio only).
- The preferred provider is the stored default while it is still offerable,
  else a caller fallback, else the first row; a default naming a deleted
  provider is skipped rather than selected into a dead id.
- The model half of a stored default only applies while its provider half is
  selected.
- If the capability config fails to load, everything is offered and the amber
  banner explains why (fail soft), except `hidden`, which needs a loaded config.
- `glossary.supported: false` disables the glossary checkbox with
  "This model has no way to receive a glossary, so the terms would be ignored."
- `inline_diarization: false` gives "This model returns no speaker labels."
- A declared conflict greys the second feature with
  "This model rejects requests that combine ... with ...".
- Reasoning effort dropdown appears only for models that expose levels, and
  `none` is dropped for a model that requires reasoning.

Steps:
1. Delete the provider named in a stored default, reload, confirm the picker
   falls back to another row rather than showing a blank or dead selection.
2. Set a stored default of provider A + model M, then select provider B in the
   Dashboard, confirm M is not carried over and B's own favourite is chosen.
3. Confirm an OpenRouter row appears in the re-transcribe picker on the session
   page but not in the Dashboard's capture picker.
4. Confirm a chat-only provider (for example an Ollama row) appears in the
   Summarize dialog but not in any transcription picker.
5. On the Summarize dialog, pick a reasoning model and confirm the effort
   dropdown appears with the config's levels; pick a non-reasoning model and
   confirm the dropdown disappears entirely.

---

## F12 `routes/sessions/+page.svelte` (History)

Table: select-all checkbox, per-row checkbox, Started (locale string), Status
badge (destructive for error, secondary for completed, outline otherwise),
Primary (provider name, or an 8-char id for a deleted provider), Campaign (or
`-`), Open link. Toolbar: "<n> selected", Merge selected (disabled below 2),
Delete selected (disabled at 0, confirms with
"Delete N session(s)? This also removes their audio.", destructive). Merge
confirms with the parts named oldest to newest and then navigates to the new
session. Empty state: "No sessions recorded."

Steps:
1. Open History, confirm the list renders and the counts start at "0 selected".
2. Tick one row, confirm the count updates and Delete enables while Merge stays
   disabled `[neg]`.
3. Tick a second row, confirm Merge enables.
4. Click the header checkbox, confirm every row selects; click again, confirm
   all clear.
5. Click Merge selected, read the confirm text, confirm the sessions are named
   oldest to newest and worded "a and b" for two, "a, b, and c" for three.
6. Cancel the merge, confirm nothing happened.
7. Accept the merge, confirm it navigates to a new session page whose transcript
   contains both parts in order.
8. Click Delete selected, cancel, confirm nothing was deleted; accept, confirm
   the rows disappear and the audio is gone (the session page 404s or the
   player is absent).
9. `[obs]` Confirm a session captured with a since-deleted provider shows a
   short id, not a blank cell.
10. `[obs]` Confirm the empty state text when no sessions exist.

---

## F13 `routes/sessions/[id]/+page.svelte` (session page shell)

Owns: the session detail, the job list, the selected version, the playhead.
Sections (Transcriptions, Transcript, Summary) fold and their state persists in
`localStorage` under `loreline.session-sections`; first visit has Transcriptions
open and the other two folded. A running job is polled every 1.5s and the detail
is refetched once the queue drains. Selecting a version cancels a slower earlier
fetch (request token), so switching quickly can never show the wrong version's
segments. Clicking a transcript timestamp seeks the docked player and reveals
the row.

Steps:
1. Open a session, confirm Transcriptions is open and Transcript and Summary are
   folded on a first visit.
2. Fold and unfold each section, reload, confirm the state survived.
3. Switch rapidly between the original and a re-transcription version, confirm
   the transcript always matches the highlighted row and never flashes another
   version's content.
4. Queue a re-transcription, confirm the row count ticks up about every 1.5s
   while it runs and that the page refetches once when it finishes.
5. Click a timestamp, confirm the player jumps there and the row is revealed and
   highlighted.
6. Click a timestamp inside the row already highlighted, confirm it still seeks
   and reveals (the nonce path).
7. `[neg]` Open `/sessions/does-not-exist`, confirm a readable error banner
   rather than a blank page.

---

## F14 `SessionHeader.svelte` + `Foldable.svelte`

Header: "Session", a status badge, the start time and duration (`h:mm:ss` or
`m:ss`), an Export menu and a "← Back" link. Export lists Text (.txt),
Markdown (.md), Subtitles (.srt), Subtitles (.vtt), JSON (.json), plus
"Audio (.wav)" when the session has a recording, which becomes a disabled
"Audio (empty)" with the EMPTY_AUDIO_NOTE tooltip when the WAV holds nothing.

Steps:
1. Open the Export menu, confirm all five formats plus the audio entry.
2. Download each format in turn, confirm the file downloads, its extension and
   that the content is right for the format (srt/vtt have timecodes, json is
   valid JSON, md has structure).
3. Click outside the export menu, confirm it closes.
4. On a session that captured no audio, confirm the entry reads "Audio (empty)",
   is disabled, and shows the "no captured audio" tooltip `[neg]`.
5. On a session with no `audio_path` at all, confirm the audio entry is absent.
6. Click "← Back", confirm it returns to History.
7. `[obs]` Confirm the duration is blank on a session that never ended and
   correct for one that did.

---

## F15 `TranscriptVersions.svelte` (version table + New transcription)

Columns: Transcript (`original` or the job's 8-char id), Provider, Model,
Diarization, Segments, Created, Status, actions. The `original` row's badge is
`live` only while the session is capturing or stopping, `error` for an errored
session, otherwise `complete`. A running transcribe job shows "<n> so far…" with
an explanatory tooltip. A row is clickable only if the job is in flight or done
with segments; a done-but-empty row carries the title "This pass produced no
segments, so there is nothing to show for it." The Diarization cell shows
"diarizing… <n>" while running, else the diarizer label
("OpenAI · gpt-4o-transcribe-diarize", "sherpa-onnx · <endpoint>", or the mode).
Every row has "Show logs"; only re-transcriptions have Delete, disabled while in
flight, confirming with "Delete version <8 chars> and its N segments? ...".
When the original streamed and a re-transcription exists, a note explains the
segmentation differs. The most recent job error is shown under the table.

Steps:
1. Confirm the version count in the section header reads "1 version" with no
   re-runs and "N versions" after re-runs.
2. Click the `original` row, confirm the transcript below switches to it and the
   row gets the selected bar.
3. Queue a re-transcription, click its row while it runs, confirm segments
   stream into the transcript live.
4. `[obs]` Confirm the Segments cell counts up with "so far…" and has the
   tooltip.
5. `[neg]` Produce a job that writes no segments (a provider that errors on
   every utterance), confirm the row is not clickable and the title explains why.
6. Click "Show logs" on the original, confirm the dialog opens with lines from
   the capture and that the row is NOT selected by that click.
7. Click "Show logs" on a re-transcription, confirm it shows that job's log only.
8. `[neg]` Open logs for a version with no stored log, confirm
   "No logs were stored for this version."
9. Click Delete on a finished re-transcription, read the confirm text, cancel,
   confirm nothing changed; accept, confirm the row goes and the page falls back
   to `original` if that version was selected.
10. `[obs]` Confirm Delete is disabled while a job is in flight, with the
    "Wait for the job to finish" tooltip.
11. `[obs]` Confirm the original row has no Delete button at all.
12. `[obs]` Confirm the segmentation-difference note appears only when the
    original has turn ids and at least one re-transcription exists.
13. `[obs]` Force a job failure, confirm "Last failed job (transcribe): ..."
    under the table and the error as the badge's tooltip.

---

## F16 `ReprocessPanel.svelte` (New transcription row)

Shows only when the session has stored audio; otherwise the sentence "No stored
audio for this session - re-processing and diarization are unavailable." The
provider list is `transcribe`-capable (wider than capture: OpenRouter belongs
here). Provider seed is the stored default, else the capturing provider, else
the first row. A model is required. Glossary checkbox on by default, disabled
with a reason on models that cannot take one, and carrying the amber warning
plus a full sentence under the row when the glossary would drop word timestamps.
Button reads "Queuing…" while busy.

Steps:
1. Confirm the provider dropdown here lists providers the Dashboard's does not
   (OpenRouter).
2. Confirm the seeded provider matches the stored transcription default, not the
   session's capture provider, when both exist.
3. Clear the model `[neg]`, confirm the button is disabled with the title "Pick a
   model to re-process with."
4. Queue a run, confirm the button shows "Queuing…" and that a new row appears in
   the table above without a manual refresh.
5. Pick a model that cannot take a glossary, confirm the checkbox disables and
   unticks with a reason in its title.
6. Pick a model that drops word timestamps with a glossary, confirm the amber
   triangle and the full amber sentence under the row.
7. `[neg]` On a session with no audio, confirm the panel is replaced by the
   "No stored audio" sentence and that diarization is unavailable too.
8. `[neg]` Queue a run against a provider with a bad key, confirm the job row
   ends `error` and the message surfaces under the table.

---

## F17 `TranscriptPanel.svelte` (transcript section + diarize row)

Section header meta: `<version> · <n> segments`, or `<shown>/<total>` while a
search is on; gap markers are excluded from both counts. Info bar: Transcript
`<version>`, Provider, Model, "Diarized with <label>" or "Not diarized",
Segments. Controls: search toggle, a diarizer dropdown (`sherpa-onnx` /
`gpt-4o-transcribe-diarize`), an endpoint box when remote, min and max speaker
number inputs, a Diarize button, and "Rename speakers" (disabled when the shown
version has no speaker labels). The diarize controls only render when the
session has stored audio. `showSource` chips appear on the `original` version
only.

Steps:
1. Confirm the meta count matches the visible rows, and that a session with gap
   markers is not counted higher because of them.
2. Search, confirm the header reads `<hits>/<total>` and both places agree.
3. Press Escape in the search box, confirm it closes and clears.
4. Confirm "Rename speakers" is disabled on an undiarized version `[neg]` and
   enabled after diarization.
5. Open Rename speakers, confirm one field per label prefilled with any stored
   name, save, confirm the transcript updates and the dialog closes only after.
6. Rename to blank, save, confirm the raw label comes back.
7. Cancel the dialog, confirm nothing was saved.
8. Pick `sherpa-onnx`, leave the endpoint blank, press Diarize, confirm it falls
   back to the session's stored endpoint and either runs or errors readably.
9. Enter min 2 and max 3, run a diarization, confirm the job row appears with
   "diarizing… <n>" and finishes with the diarizer label in the table.
10. `[neg]` Enter min 5 max 2 (inconsistent), confirm what the server does and
    whether the UI explains it.
11. `[neg]` Enter a negative or zero speaker count, confirm the `min="1"`
    attribute and what the server says if it is bypassed.
12. Pick `gpt-4o-transcribe-diarize`, confirm the endpoint box disappears and the
    run uses OpenAI.
13. Re-run diarization on the same version, confirm the tooltip's promise holds:
    the previous diarization is replaced, not duplicated.
14. `[obs]` Confirm source chips show on `original` and not on re-runs.

---

## F18 `SessionPlayer.svelte`

Renders only when the session has an `audio_path`. An empty recording shows the
EMPTY_AUDIO_NOTE instead of controls. Otherwise: a play/pause circle, a current
clock, a seek bar with one dot per non-gap segment (active dot enlarged and
darkened), a total clock (from the element's metadata, falling back to the API's
duration), and a range input covering the whole bar.

Steps:
1. Press play, confirm audio plays, the button becomes Pause, the clock advances
   and the transcript row highlight follows.
2. Press pause, confirm playback stops and the clock freezes.
3. Drag the seek bar, confirm playback moves and the transcript reveals the new
   segment.
4. Click a dot, confirm it seeks to that segment.
5. `[obs]` Confirm the right-hand clock shows the real total immediately, before
   the WAV has fully loaded.
6. `[obs]` Confirm gap markers get no dot.
7. `[neg]` On a session with an empty recording, confirm the note replaces the
   player and the transcript timestamps are not clickable.
8. `[neg]` On a session with no recording at all, confirm no player and no stray
   separator line.

---

## F19 `SessionSummary.svelte` + `SummarizeDialog.svelte`

Summary section: shows the stored summary as pre-wrapped text with meta
"<provider> · <model>"; with no LLM provider configured it says to add one; else
"Not summarized yet." Buttons: "Generate video" (disabled with a reason when no
video provider or no summary) and "Summarize"/"Re-summarize" (disabled with no
LLM provider). Summarizing with no diarized speakers asks
"This session has no diarized speakers. Summarize anyway?" first, and the dialog
then repeats the warning in red. Video jobs poll every 5s while queued or
running, render a `<video>` when done, an error message when failed, and each
has a Delete that confirms "Delete this video and its file?".

Steps:
1. On a session with no summary, confirm "Not summarized yet." and that Generate
   video is disabled with the title "Summarize the session first".
2. Click Summarize on an undiarized session, confirm the pre-confirm dialog and
   that cancelling stops there.
3. Accept, confirm the dialog's red "No diarized speakers" description.
4. Confirm the provider list holds only summarize-capable rows and that the
   stored default is preselected and tagged.
5. Pick a reasoning model, confirm the effort dropdown with "Model's default"
   plus the config's levels; switch to a model with different levels, confirm an
   unsupported carried-over level is cleared.
6. Pick a non-reasoning model, confirm the effort dropdown disappears entirely.
7. Clear the model `[neg]`, confirm Summarize is disabled with the title
   "Pick a model to summarize with."
8. Run a summary, confirm "Summarizing…", then the dialog closes and the text is
   on the page with the right provider and model in the section meta.
9. `[neg]` Run a summary against a provider with a bad key, confirm the error
   stays in the dialog and the dialog does not close.
10. Reopen the dialog after a failure, confirm the old error is gone.
11. Click Re-summarize, confirm it overwrites.

---

## F20 `GenerateVideoDialog.svelte`

Provider list = video-capable rows (OpenRouter). Model list from the video
catalogue, loaded when the dialog opens. The prompt is seeded from the summary
on first open and never overwritten while open. Length, Resolution and Aspect
ratio dropdowns appear only when the model offers those, and switching model
drops any value the new model does not support. "Generate audio" appears only
when the model offers it. Generate is disabled without provider, model and a
non-blank prompt.

Steps:
1. Open the dialog, confirm the prompt is prefilled with the summary.
2. Edit the prompt, close and reopen, confirm the edit survives (it re-seeds
   only when the prompt is empty).
3. Blank the prompt `[neg]`, confirm Generate is disabled.
4. Switch models, confirm the parameter dropdowns change and that a value the
   new model does not offer is replaced by its first supported one.
5. `[obs]` Confirm the model list shows "Loading models…" and then options, and
   that a provider with a bad key shows
   "No video models available - check the provider's API key."
6. `[obs]` Confirm a deprecated video model shows the amber retirement note.
7. Press Generate, confirm the dialog closes, a job row appears as "Generating…"
   and the list polls about every 5s.
8. Let it finish, confirm a `<video>` element with working controls appears.
9. `[neg]` Force a failure (bad key or an impossible prompt), confirm the red
   error text on the job row.
10. Delete a video, confirm the "Delete this video and its file?" dialog and that
    the row and its file go.
11. `[neg]` With no OpenRouter provider configured, confirm the dialog says to
    add one and the button was disabled in the first place.

---

## F21 `confirm.svelte.ts` + `ConfirmDialog.svelte`

One dialog mounted in the layout, promise-based. Title defaults to "Confirm",
labels default to Cancel/Confirm, destructive actions get a red confirm button.
Dismissing (Escape or the overlay) resolves false.

Steps:
1. Trigger any confirm, press Escape, confirm the action does not happen.
2. Trigger one, click the overlay, confirm the same.
3. Confirm a destructive dialog (session delete, version delete) has a red
   confirm button and the custom label where one is set ("Delete").
4. `[obs]` Confirm no browser-native `window.confirm` ever appears.

---

## F22 `routes/settings/+layout.svelte` and `+page.svelte`

Tabs: Client, Providers, Glossary, Alerting, Services. The active tab is matched
by exact pathname. `/settings` immediately redirects to `/settings/client` with
`replaceState`.

Steps:
1. Visit `/settings`, confirm the URL becomes `/settings/client` and that the
   browser Back button goes to the previous page, not back to `/settings`.
2. Click each tab, confirm the underline moves and the content changes.
3. Deep-link each tab URL directly, confirm the right tab is highlighted.

---

## F23 `routes/settings/client/+page.svelte`

Microphone dropdown ("System default" plus each device; a stored device that is
no longer present is appended as "<name> (not found)", disabled, with the title
"This device is no longer available - pick another one."). Picking saves
immediately and shows "Saved", and restarts a running meter. "Test"/"Stop"
button opens `/ws/audio/level?device=...` and drives the level meter; a frame
carrying `error` shows it in red and stops the meter. Below: Revision (first 10
chars of the commit) with an "Update now" button, and an Autostart switch that
reads "unavailable" when the endpoint fails. Update output: a single-line result
is shown verbatim; multi-line output goes into a scrollable `<pre>`.

Steps:
1. Open the Microphone dropdown, confirm "System default" plus the real devices.
2. Pick a device, confirm "Saved" appears and that the choice survives a reload.
3. Press Test, confirm the meter animates with sound and that the button becomes
   Stop; press Stop, confirm the meter returns to zero.
4. Change the device while metering, confirm the meter restarts on the new one.
5. `[neg]` Select a device that cannot be opened (or run Test with no mic),
   confirm the red error text and that metering stops rather than hanging.
6. `[neg]` Confirm a stored-but-missing device shows "(not found)", is disabled
   and cannot be re-picked.
7. `[obs]` Confirm the Revision shows a 10-char commit or `-`.
8. Press "Update now" on this Docker deployment, confirm the button shows
   "Updating…" and then the verbatim single-line answer (expected: it cannot
   update a Docker deployment by itself unless the updater service runs).
9. Toggle Autostart, confirm it settles to the server's answer and that a failed
   toggle snaps the switch back rather than leaving it lying.
10. `[obs]` Confirm no rollback control exists here even though the README says
    the same page rolls back to any earlier commit, and the API client has a
    `rollback` call. Record as a finding.
11. `[obs]` Confirm Test is usable while a session is capturing, or that it
    fails cleanly if the device is already open.

---

## F24 `routes/settings/providers/+page.svelte` (largest surface)

### Provider table
Columns: Name, Supports (capability badges), Endpoint (`base_url` or "default"),
API key (`secret_hint` or "- none -"), Status, actions. Status badges from the
Test result: testing… (amber), healthy (green), degraded (amber), auth failed
(red), unreachable (red), unknown (grey), not tested (grey outline); the
vendor's own message is the badge tooltip. "Test all" runs every row in
parallel and is disabled with no providers. Empty state:
"No providers yet - click + to add one." A failed provider fetch shows a red
banner with Retry.

### Wizard (+ button)
Step 1 hosting: Cloud provider / Self-hosted. Step 2 lists the kinds for that
hosting (a kind the config did not describe appears under both). Step 3 form:
Name (placeholder is the real default, deduplicated as "OpenRouter 2"), Base URL
(only for kinds that need one), Language, Favorite models (Load models button,
disabled with the title "Add an API key first" when a key is required and none
is on file; a filter box; a checkbox list with price, context and retirement
hints), API key (password field, labelled "API key (optional)" for optional-auth
kinds and suffixed "- blank = keep current" when editing, with a "Get an API key
↗" link), and for OpenRouter only a Provider routing box (Prefer: Balanced /
Cheapest / Highest throughput / Lowest latency; "No data collection";
"Zero Data Retention only"; plus a note when either is on). Saving with a
required key missing asks "No API key ... Save anyway / Go back".

### Defaults card
Four boxes: Transcription (provider + model), Diarization (mode + endpoint when
remote), Summary (provider + model + reasoning effort, or a note to add an LLM
provider), Video (provider + model, or a note to add OpenRouter). Then a Summary
system prompt textarea ("Clear it and save to restore the built-in default") and
the "Only show compatible models" switch. "Save defaults" shows "Saved" for
2.5s.

Steps (table):
1. Click "Test all" with several providers, confirm every row goes to "testing…"
   and then to a graded badge.
2. Test one row with a good key, confirm "healthy".
3. `[neg]` Edit a row to a wrong key, Test, confirm "auth failed" in red and the
   vendor's message in the tooltip.
4. `[neg]` Point a self-hosted row at a dead URL, Test, confirm "unreachable".
5. `[obs]` Confirm the Supports badges match what that vendor can do.
6. `[obs]` Confirm the API key column shows a masked hint, never the key.
7. Delete a provider, confirm the "Delete this provider? This also removes its
   stored key." destructive dialog, cancel, then accept and confirm the row goes
   and every picker stops offering it.
8. `[neg]` Delete the provider a stored default names, confirm the defaults card
   recovers rather than showing a dead selection.

Steps (wizard):
9. Click +, confirm step 1; pick Cloud, confirm step 2 lists cloud kinds only;
   press ← Back, confirm step 1 again.
10. Pick Self-hosted, confirm the self-hosted kinds (OpenAI-compatible).
11. Pick a kind, confirm step 3 and that the Name placeholder is the vendor
    label; save with the name left blank, confirm the row is named after the
    vendor.
12. Add a second row of the same kind with a blank name, confirm it becomes
    "<label> 2".
13. `[neg]` Press "Load models" before entering a key on a key-required kind,
    confirm the button is disabled with the title "Add an API key first".
14. Enter a key, press "Load models", confirm the list loads, the filter box
    appears, and that hints (price, context, retiring) render.
15. Tick two favourites, save, reopen the row, confirm they persist and show as
    badges before models are loaded.
16. Filter the model list to nothing, confirm "No models match."
17. `[neg]` Save a key-required kind with no key, confirm the "No API key"
    dialog, choose "Go back", confirm nothing was saved; repeat and choose
    "Save anyway", confirm the row saves with "- none -" in the API key column.
18. Edit a row, leave the key blank, save, confirm the stored key is kept (the
    label says "blank = keep current").
19. Edit a row, enter a new key, save, Test, confirm the new key is in use.
20. For OpenRouter, confirm the routing box appears; for every other kind,
    confirm it does not.
21. Set Prefer = Cheapest, tick "No data collection", confirm the note about a
    model with no matching provider appears, save, reopen, confirm the settings
    persisted.
22. Tick "Zero Data Retention only", save, Test, confirm what the vendor says.
23. `[obs]` Confirm the "Get an API key ↗" link opens the vendor's key page in a
    new tab.
24. Cancel the wizard mid-way, confirm nothing was saved and reopening starts at
    step 1.

Steps (defaults):
25. Set a transcription provider and model, save, confirm "Saved" and that the
    Dashboard preselects them.
26. Switch the transcription provider, confirm the model resets to the new
    provider's favourite rather than carrying the old id over.
27. Set Diarization to Remote, confirm the endpoint box appears, save, confirm
    the Dashboard prefills it.
28. Set a diarization default of Inline, then change the STT model to one with
    no speaker labels, confirm the mode falls back to None.
29. Set a summary provider, model and reasoning effort, save, confirm the
    Summarize dialog preselects all three.
30. Switch to a summary model with different effort levels, confirm an
    unsupported saved level is cleared rather than sent.
31. Edit the Summary system prompt, save, run a summary, confirm the tone
    changes; clear it, save, confirm the built-in default is restored.
32. Turn "Only show compatible models" off, save, open a transcription model
    picker, confirm non-transcription models now appear; turn it back on and
    confirm they vanish.
33. `[obs]` Confirm the "default" tag in each picker follows what is saved, not
    what is currently selected in the draft.
34. `[neg]` With no LLM provider, confirm the Summary box shows the "Add an LLM
    provider" note; same for Video and OpenRouter.
35. `[obs]` Note that the provider form carries `sample_rate` and `enabled`
    fields with no control anywhere in the UI. Record as a finding.

---

## F25 `routes/settings/glossary/+page.svelte`

One Textarea, one term per line, saved on blur (there is no Save button), with
"Saved" shown for 2.5s. Loads the default glossary on mount.

Steps:
1. Type three terms on three lines, click outside, confirm "Saved" appears.
2. Reload the page, confirm the terms came back in the same order.
3. Add blank lines and leading/trailing spaces, blur, reload, confirm they were
   trimmed and the blanks dropped.
4. Clear everything, blur, reload, confirm the glossary is empty.
5. `[obs]` Confirm the terms actually reach the provider: start a capture with
   "Use glossary" on and check the logs for keyterms/prompt, then repeat with it
   off.
6. `[obs]` Note that the API supports per-campaign glossaries
   (`/api/glossary/{campaign}`) and sessions carry a `campaign_id`, but the UI
   offers neither a campaign selector nor a way to set a session's campaign.
   Record as a finding.
7. `[neg]` Blur with the backend down, confirm the failure message appears
   instead of a false "Saved".

---

## F26 `routes/settings/services/+page.svelte`

Polls `/api/system/services` every 5s. Splits into Core services (not
controllable) and Additional services (controllable). Each row: name, State
badge (green "running", grey otherwise), Status text, Image, a Logs button and,
for controllable rows, a Start/Stop button. With no services at all, a card
explains the page needs the Docker API. With no optional services, the copy
gives the `docker compose --profile ...` command. The logs card has Refresh and
Close and shows "(no output)" for an empty log.

Steps:
1. Open the page, confirm core rows (app, caddy, docker proxy) appear with no
   Start/Stop control.
2. Confirm the state badges match reality and that they refresh on their own
   within ~5s after a container changes state.
3. Click Logs on a core service, confirm output, press Refresh, confirm it
   refetches, press Close, confirm the card goes.
4. If an optional service exists, press Start, confirm the button disables while
   busy and the row flips to running; press Stop and confirm the reverse.
5. `[neg]` Confirm the updater container, if present, is listed but not
   controllable.
6. `[neg]` If the Docker API is unavailable, confirm the explanatory card rather
   than an empty table.
7. `[obs]` Confirm the "None installed" copy shows the compose command when no
   optional services exist.

---

## F27 `routes/settings/alerts/+page.svelte`

Table: Type, Target (chat id for Telegram, url for webhook, topic for ntfy),
Min level, On (checkbox that toggles immediately), and Send test / Edit / Delete
buttons. Wizard: step 1 picks ntfy / Telegram / Webhook, step 2 shows only that
type's fields (ntfy: Server, Topic, optional Auth token; Telegram: Chat id, Bot
token; Webhook: URL), plus Min level (info/warning/error) and an Enabled
checkbox. Save is disabled until the type's key field is filled. Editing shows
"blank = keep" on the token. Test reports "Test sent" or "Test failed". Delete
confirms "Delete this alert channel?".

Steps:
1. Add an ntfy channel with topic only, confirm it saves and lists with the
   topic as Target.
2. `[neg]` Try to save with the topic blank, confirm Add is disabled.
3. Send a test, confirm "Test sent" and that the notification actually arrives.
4. `[neg]` Point ntfy at a dead server, send a test, confirm "Test failed".
5. Add a Telegram channel, confirm Save is disabled without a chat id, then save
   with a bot token and send a test.
6. Add a Webhook channel, confirm Save is disabled without a URL, save, test
   against a request bin and confirm the JSON payload shape.
7. Edit a channel, leave the token blank, save, then Send test, and confirm the
   stored token still works (label promises "blank = keep").
8. Toggle the On checkbox in the table, reload, confirm it persisted.
9. `[neg]` IMPORTANT: after toggling On from the table, send a test again. The
   toggle sends an update body with no `token` field at all, so if the backend
   treats a missing token as "clear it", the stored credential is silently lost.
   Verify explicitly.
10. Change Min level to error, confirm warnings no longer arrive but errors do.
11. Delete a channel, confirm the destructive dialog and the removal.
12. `[obs]` Confirm the header health popover's "Alerts on/off" line agrees with
    whether any channel is enabled.

---

## F28 Cross-cutting: `magicBento.ts`, `portal.ts`, `elapsed.svelte.ts`, `app.html`

- The app is hard-set to dark (`<html class="dark">`); there is no theme toggle,
  so `prefers-color-scheme: light` must not break contrast anywhere.
- Magic Bento adds a cursor glow, a proximity spotlight and hover particles to
  cards; it is disabled entirely under `prefers-reduced-motion: reduce` and
  below a 768px viewport.
- Dropdown popovers are portalled to `document.body`, so they must not be
  clipped by a Card's `overflow: hidden` nor left behind on unmount.
- The recording clock counts locally once a second and stops when there is
  nothing to count.

Steps:
1. Move the mouse across the cards, confirm the glow follows and the particles
   appear on hover without lag.
2. Enable `prefers-reduced-motion: reduce`, reload, confirm no glow and no
   particles.
3. Resize below 768px, confirm the effect is off and nothing is left painted.
4. Open a dropdown inside a Card near the bottom of the viewport, confirm the
   popover is fully visible and not clipped.
5. Open a dropdown, then navigate away with the keyboard, confirm no orphaned
   popover is left in the DOM.
6. Emulate `prefers-color-scheme: light`, confirm the UI stays legible (it is
   dark-only by design; record any unreadable area as a finding).
7. Walk the whole app with Tab only, confirm every control is reachable, the
   focus ring is visible, and no focus trap exists outside dialogs.
8. `[obs]` Confirm the recording clock ticks once a second and stops at 0 when
   no session runs.

---
---

# Part 2: the merged, ordered run

Sections F01-F28 above were written one per source file, so they repeat each
other: the Dropdown behaviour appears in six places, "open Settings > Providers"
is a precondition for a dozen steps, and several checks are only possible once
something else has been created. This part is that plan deduplicated and put in
dependency order, and it is the order the run actually followed.

Rules applied while merging:

- **Component behaviour is proved once.** Dropdown keyboard handling, the
  Foldable, the ConfirmDialog and the LiveFeed are exercised on their first
  appearance and trusted afterwards. Their per-file steps are folded into
  stage 2.
- **Group by screen, not by file.** Every step that needs Settings > Providers
  open runs together, so the page is loaded once.
- **Read before write.** Everything that only observes runs before anything that
  creates, edits or deletes, so the box's real state is recorded first.
- **Create before delete.** Anything destructive acts only on what the run
  created, and runs last on that object.
- **State-changing settings are bracketed.** Anything that must be flipped to be
  tested (the compatibility toggle, the microphone, the glossary) is captured
  first and restored immediately after.

## Stage 0 - orientation, read only

1. Reach the app logged out, confirm the redirect to `/login`.
2. Record the starting state: providers, action defaults, sessions, glossary,
   alert channels, services, audio devices, health.

Merged from: F01.1, F03 inventory, F12.1, F22, F23.7, F24 table, F25, F26, F27.

## Stage 1 - authentication

3. Empty password, wrong password, Enter to submit, correct password.
4. Deep-link a protected page while logged out.
5. Logout, then confirm the back navigation shows nothing.
6. `[neg]` Five wrong passwords, then a correct one from a second client, to see
   whether the lockout is per client or shared.

Merged from: F02 (all), F01.1, F01.10, F03.2.

## Stage 2 - shell and shared components, proved once

7. Health dot: click to refresh, hover for the popover, focus for the popover.
8. Sidebar collapse, reload, mobile overlay (hamburger, Escape, scrim, item).
9. Dropdown, on the Dashboard's provider picker: open by click and by ArrowDown,
   type-ahead, Home/End, no wrap at the ends, Escape closes and refocuses, Tab
   closes, filter box narrows and re-anchors the active row, empty state text,
   favourites and `default` tag, disabled rows.
10. Dropdown inside a dialog: one Escape closes the list, not the dialog.
11. ConfirmDialog: Escape and overlay click both resolve to "no"; destructive
    styling and custom labels.

Merged from: F01.3-F01.8, F09 (all), F21 (all), and every "open the dropdown"
step in F05, F10, F11, F17, F19, F20, F24, F27.

## Stage 3 - Dashboard, idle, read and validate

12. Provider list is gated to capture-capable rows; compare against Settings.
13. Model picker: lazy load, per-provider lists, hints, deprecation note.
14. Model with no inline diarization: option withdrawn, mode falls back, note.
15. Model that cannot take a glossary: checkbox disabled with the reason,
    summary reads "Unsupported".
16. Model whose glossary costs word timestamps: amber warning in both places.
17. Fallback provider with no model: red summary, Start disabled.
18. Remote diarization with an empty endpoint: inline error, Start disabled.
19. Remote diarization prefill; bogus endpoint probe; stale-verdict check.
20. Whether the glossary re-arms after leaving a model that cannot take one.

Merged from: F05.1-F05.13, F10.2-F10.8, F11.2-F11.5.

## Stage 4 - Dashboard, capture

21. Start with the stored microphone; if it fails, read the message.
22. Fix the device in Settings > Client, return, start again.
23. While recording: elapsed clock, recorded seconds, level meter, header badge.
24. Logs pane while there are lines: drag, fold, tap, persistence, each control,
    filter, wrap, follow, clear.
25. Transcript pane: seeding across a navigation, filter, autoscroll, clear.
26. `[neg]` A second start while one runs.
27. Stop: the finalizing state, then what the pane shows during the drain.
28. `[neg]` A stop when nothing is running.

Merged from: F04 (all), F05.14-F05.21, F06 (all), F07 (all).

## Stage 5 - History

29. Empty and populated states, badges, provider names, campaign column.
30. Selection: one row, select-all, counts, button enablement.
31. Merge: confirm text and ordering, cancel, then confirm.

Merged from: F12 (all).

## Stage 6 - session page, on a session that already has several versions

32. Section folds and their persistence.
33. Version table: columns, original row badge, done-but-empty row,
    "Show logs" not selecting the row, the segmentation-difference note, the
    last-failed-job line.
34. Select a non-original version; confirm the transcript, the info bar and the
    counts all follow it.
35. **With that version selected, export and check which version comes out.**
36. Transcript: search and its counts, highlight, gap markers, Escape.
37. Player: play, pause, seek by drag, seek by dot, seek by timestamp, the
    active-row highlight.
38. Export: every format's content type, filename and body; audio entry; the
    empty-audio variant on a session that captured nothing.
39. Summary section: existing summary, meta line, button enablement and reasons.
40. Generate video dialog: seeding, model switch re-deriving the parameters,
    validation, the whitespace-prompt trap.
41. Version logs dialog for each version, including one with none.

Merged from: F13, F14, F15, F17, F18, F19, F20 (all), plus F08's list steps.

## Stage 7 - jobs, on a session the run can afford to change

42. Re-process with a local provider: row appears, count updates, job completes.
43. Diarize with the endpoint box left empty.
44. Diarize with the endpoint filled in; watch progress; watch what happens on
    failure; check the diarizer's health afterwards.
45. Rename speakers, in the dialog and in place, including blank and Escape.
46. Delete a version created by the run: confirm text, cancel, then confirm.

Merged from: F15.3-F15.13, F16 (all), F17.4-F17.14.

## Stage 8 - Settings, in one pass per tab

47. Client: device list including a missing device, save, meter start/stop,
    revision, update button, autostart.
48. Providers: table and badges, Test all, per-row Test with a good and a bad
    key, the wizard end to end for a self-hosted and a cloud kind, the no-key
    confirm, the whitespace-key case, OpenRouter routing, delete.
49. Providers > Defaults: each picker, the pairing rule, the reasoning-effort
    list, the system prompt, and the compatibility toggle measured on a
    transcription picker **and** a summary picker.
50. Glossary: save on blur, trimming, blanks, duplicates, restore.
51. Alerting: each channel type's fields and validation, create, test, toggle,
    edit with a blank token, delete.
52. Services: core versus optional, logs, refresh, stop and start.

Merged from: F22, F23, F24, F25, F26, F27 (all).

## Stage 9 - cross-cutting, last

53. Narrow-viewport sweep at 390 and 320 on every page, looking only for
    overflow with no scrollable ancestor.
54. Tab order and accessible names on the Dashboard.
55. Magic Bento presence; reduced motion if the harness can emulate it.
56. Restore everything the run changed and record what it could not.

Merged from: F28 (all), F01.7-F01.8.

## Steps dropped as duplicates

- "Open a dropdown and pick an option" appears in F05, F10, F11, F16, F17, F19,
  F20, F24 and F27; kept once in stage 2 and then used as a means, not a test.
- "Confirm the dialog cancels" appears in F12, F15, F19, F21, F24 and F27; kept
  once in stage 2 and then only where the wording itself is the thing under test.
- "Section folds and persists" appears in F13 and F14; kept once in stage 6.
- "Model picker lazy-loads" appears in F05, F10 and F16; kept once in stage 3.
- Every `[api-only]` branch noted in the backend analysis is excluded from this
  plan by definition and is listed in `analysis-backend-api.md` instead.
