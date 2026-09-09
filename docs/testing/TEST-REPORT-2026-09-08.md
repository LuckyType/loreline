# Loreline UI test report, 2026-09-08

Target: http://10.10.50.55/ (Loreline 0.1.0, same revision as this checkout).
Method: source read first (see `TEST-PLAN.md`), then every step driven through
the browser. Findings are numbered and rated.

Severity: **high** = data loss, a broken core flow, or a security problem.
**medium** = a flow that misleads or blocks a user with a workaround.
**low** = cosmetic, or an edge case. **ux** = works as coded, reads badly.

Environment as found: 6 providers (AssemblyAI, Deepgram, Google Gemini, OpenAI,
OpenRouter, Speaches local), 5 stored sessions (1 errored), diarizer healthy at
`http://diarization:8001`, no alert channels, services app/caddy/docker-proxy
(core) and diarization/speaches (optional, both running), stored defaults with
`strict_model_filtering: false`.

## Fix status, added 2026-09-08 after the run

Everything below was found by the test run and is written as it was observed on
the deployed 0.1.0 image. It is left in the past tense on purpose: it is the
record of what the audit saw, not a description of the code as it stands now.
This table is the only part that tracks what was done about it.

All 29 are fixed on branch `t3code/7587409d`. The checks are green: ruff,
pyright, 1091 pytest tests (49 of them new), the capability check, the OpenAPI
document, and the frontend lint, type check and build.

| # | Fixed by |
| --- | --- |
| F-01 | The health dot is a real disclosure button: click, Enter, Space and touch all open it, Escape and an outside click close it, `aria-expanded` is honest, and hover still works for a mouse. |
| F-02 | The capture row is `grid-cols-1 sm:grid-cols-[1fr_1fr_auto]` with a full-width Start below `sm`. |
| F-03 | The glossary stores the GM's choice, not the flag: `glossaryPick` plus a derived `useGlossary`, so a model that cannot take one no longer erases the default. Same fix in `ReprocessPanel`. |
| F-04 | New `client_address()` keys the limiter on the rightmost `X-Forwarded-For` entry when the peer is a trusted proxy, validated as an IP, falling back to the peer. Expired entries are now pruned. |
| F-05 | `healthz` requires auth; new unauthenticated `GET /api/system/livez` returns only `{"status":"ok"}`. `deploy/install.sh`, README and CONTRIBUTING follow it. |
| F-06 | The capture card fetches the device list once on mount and flags a stored device that is gone, in the summary line, linking to Settings. It warns rather than blocks, deliberately. |
| F-07 | Two bugs, both fixed: the manager grew `live_view_session_id()` that survives teardown, and `EventBus.subscribe()` now takes a predicate applied at publish time, so the filter is decided where the record is written rather than whenever the socket task happens to run. |
| F-08 | `LogLine` splits on ` key=` boundaries, so a value keeps its spaces. |
| F-09 | `subtitle_cues()` sorts, clamps each end to the next start, and splits long rows on real word timings where the provider returned them. |
| F-10 | Merge sets `ended_at` from the sum of the parts' spans. |
| F-11 | `GET /export?version=` and a `version` field on the summarize body, both 404 on an unknown version; merge takes each source's newest completed re-transcription; new `summary_version` and `merged_from` columns. The UI passes the selected version and names it in the Export menu, the Summarize dialog and the Summary meta line. |
| F-12 | The seeding guard tests `!prompt.trim()`, plus an explicit "Reset to summary" button. |
| F-13 | New `Markdown.svelte` renders headings, lists and inline emphasis by building typed spans, never an HTML string, so there is nothing to sanitise and no dependency was added. |
| F-14 | A character count under the prompt, with an over-limit warning where the capability config publishes a limit and an amber nudge past 1200 characters where it does not. |
| F-15 | Every re-processing run now writes `reprocess.start`, `reprocess.finished` with segments and elapsed time, and on failure the message plus the traceback, to its own version log. |
| F-16 | The logs dialog remembers its trigger and restores focus to it on close. |
| F-17 | `filter_models` narrows every interaction. Summarize and video are narrowed negatively by a new `incompatible_name_markers` list in the YAML plus a model's own declared interactions; unknown models stay offered. |
| F-18 | `ModelPicker` filters favourites against the interaction's own catalogue, and `preferredModel` now takes an interaction so seeding cannot pick one either. |
| F-19 | The key is trimmed client-side, `ProviderCreate` normalises a blank-after-trim key to none and `SecretWrite` refuses one, `secrets.hint()` stops rendering an already-stored blank as credentialed, and a malformed header grades `unauthorized` rather than `unreachable`. |
| F-20 | `list_catalog` carries the reason a list is empty; the wizard shows it beside the button, red when nothing loaded, amber when one catalogue of several failed, and a plain note when the vendor genuinely returned none. |
| F-21 | The timeout is `60s + 4x audio seconds` instead of a flat 120s, and a failed job never stores an empty message. |
| F-22 | The banner selects on `status === 'error'` rather than on having text, prefers the newest failure, carries a timestamp, and a failed diarize pass is also marked in its own row. |
| F-23 | The log viewer hides health probes by default, says how many it hid, and offers 200 / 1000 / 5000 lines. |
| F-24 | An ntfy server and a webhook URL must be an absolute http(s) URL, validated field-wise on the server (so the 422 body cannot echo the token) and mirrored in the form. |
| F-25 | `AlertTestResult` gained `detail`, populated with the transport error or the vendor's sentence, scrubbed of the channel's token before it is logged or returned. This also closed a pre-existing token leak into the logs. |
| F-26 | The diarization service takes an explicit one-at-a-time semaphore that `/healthz` never waits on, queues a second caller for 30s then answers 429, drops a `.tolist()` that built ~35 million Python floats per long session, and shuts down within its grace period. The app also refuses to queue a diarize job against a diarizer it knows is unreachable. |
| F-27 | `merged_from` on the wire drives a "merged" badge, and History gained a Duration column. |
| F-28 | Both redirect paths carry a sanitised `next`, rejecting anything that is not a single-slash same-origin path. |
| F-29 | `txt` and `md` drop the speaker entirely when nothing in the transcript has one, and keep "Unknown" in the mixed case. |

Two notes on the three analysis documents beside this one
(`analysis-backend-api.md`, `analysis-capabilities.md`,
`analysis-session-lifecycle.md`): they describe the code as it was before these
fixes, so a few passages are now superseded, most visibly the ones about
`healthz` being open and about export following `canonical_transcript`.

None of this is deployed. The box at 10.10.50.55 runs
`ghcr.io/luckytype/loreline:latest` and still has every behaviour described
below.

---

## Summary

29 findings. Four are high severity and would each cost a user real work.

| # | Sev | Area | Finding |
| --- | --- | --- | --- |
| F-11 | high | sessions | Export, Summarize and Merge always use the original capture, ignoring the version you selected |
| F-21 | high | diarization | Self-hosted diarization times out at a fixed 120s and fails with an empty error, so the UI reports nothing |
| F-26 | high | diarization | A timed-out diarization leaves the diarizer wedged for every later request |
| F-04 | high | auth | One client's failed logins lock every other client out (rate limit keys on the proxy IP) |
| F-05 | medium | auth | `/api/system/healthz` is unauthenticated and returns the full operational snapshot |
| F-01 | medium | a11y | The header health details open on hover only; the keyboard fallback class does not compile |
| F-02 | medium | layout | The Start session button is clipped on a phone, with no way to scroll to it |
| F-03 | medium | capture | The glossary silently stays off after passing through a model that cannot take one |
| F-06 | medium | capture | A missing microphone is only reported when you press Start |
| F-07 | medium | logs | The live log pane never shows a session's teardown lines |
| F-09 | medium | export | SRT and VTT exports contain overlapping cues |
| F-15 | medium | logs | Per-version logs are nearly empty, and some versions have none at all |
| F-17 | medium | providers | "Only show compatible models" filters transcription pickers only |
| F-18 | medium | providers | A provider's favourite models leak into every interaction's picker |
| F-19 | medium | providers | A whitespace-only API key is stored as real, then reported as "unreachable" |
| F-22 | medium | sessions | "Last failed job" can show a stale error while a newer failure is hidden |
| F-24 | medium | alerting | A webhook channel accepts any string as its URL, with no validation |
| F-08 | low | logs | Log values containing spaces are broken into separate tokens |
| F-10 | low | sessions | A merged session has no `ended_at`, so its header shows no duration |
| F-12 | low | video | A whitespace-only video prompt gets stuck and cannot be re-seeded |
| F-16 | low | a11y | Closing the version-logs dialog puts focus on the section header |
| F-23 | low | services | The Services log viewer is drowned by health checks, tail not adjustable |
| F-25 | low | alerting | "Test failed" says nothing about why |
| F-27 | low | sessions | A merged session is indistinguishable from its oldest source in History |
| F-28 | low | auth | Logging in after a redirect always lands on the Dashboard |
| F-29 | low | export | Undiarized transcripts export every line as "Unknown" |
| F-13 | ux | summaries | The summary is markdown but is rendered as plain text |
| F-14 | ux | video | The video prompt is seeded with the whole summary, with no length guidance |
| F-20 | ux | providers | "Load models" against an unreachable endpoint gives no feedback at all |

The two diarization findings compound: F-21 makes every diarization fail
silently, and F-26 means each failure also takes the service down for everything
that follows. On this box, self-hosted diarization did not complete once, for
audio as short as two minutes, and at no point did the UI say so.


---

## Findings

### F-01 (medium, a11y) The header health details are mouse-hover only

The health popover in the header opens on `group-hover/health`, and the intended
keyboard fallback is written as `focus-within/health:visible
focus-within/health:block` in `frontend/src/routes/+layout.svelte:171`. That is
not a valid Tailwind variant: a named group needs `group-focus-within/health:`.
The class never compiles, so the rule does not exist.

Verified in the browser: focusing the health button leaves the popover at
`display: none; visibility: hidden`. Service status, version, capture state,
uptime, disk free, alert state and both socket states are therefore unreachable
by keyboard, and unreachable on any touch device, where there is no hover at
all. The app is explicitly built for a box you put on the table and drive from a
phone, so this is the whole diagnostic panel missing on the likely client.

Fix: `group-focus-within/health:visible group-focus-within/health:block`, or
make the dot a real toggle button.

### F-02 (medium, layout) The Start session button is clipped on a phone

At a 390x844 viewport (iPhone 12 Pro), the capture card's top row is still the
desktop grid `grid-cols-[1fr_1fr_auto]`
(`frontend/src/lib/CaptureControls.svelte`, the idle branch). Measured in the
page: the card's right edge sits at x=401 in a 390px viewport, and the primary
button's own right edge is at 401.16, so its label renders as "Start sess" with
the rest cut off. `main` reports `scrollWidth === clientWidth === 390`, so there
is no horizontal scroll to reveal it either: the overflow is clipped, not
scrollable.

The button is still clickable (about 98 of its 109px are on screen), so this is
not a blocker, but the primary action of the whole app is visibly truncated on
the device class the README expects people to use ("put it in the middle of the
table"). The two dropdowns are squeezed to 91px and 137px at the same time, so
a model id longer than a few characters truncates as well.

Fix: let the row wrap below `sm`, for example
`grid-cols-1 sm:grid-cols-[1fr_1fr_auto]`, or give the button its own full-width
row on small screens.

### F-03 (medium) The glossary silently stays off after passing through a model that cannot take one

Reproduced on the Dashboard:

1. Provider OpenAI, model `gpt-transcribe`. Summary line reads `Glossary On`.
2. Switch the model to `gpt-4o-transcribe-diarize`, which declares
   `glossary.supported: false`. The checkbox correctly unticks and disables, and
   the summary reads `Glossary Unsupported` with the reason
   "This model has no way to receive a glossary, so the terms would be ignored."
3. Switch the model again to `whisper-1`, which does take a glossary. The
   checkbox is enabled again but stays **unticked**, and the summary reads
   `Glossary Off`.

`CaptureControls.svelte` turns the toggle off in an effect
(`if (useGlossary && glossaryBlocked) useGlossary = false`) and nothing ever
turns it back on, because `useGlossary` is plain `$state` rather than a derived
that can be overridden. The user never unticked it, so the card now claims a
deliberate choice the user did not make, and the campaign glossary (147 terms of
Dark Eye spell and character names on this box) is silently not sent for the
session that follows.

The same pattern exists in `ReprocessPanel.svelte`.

Fix: remember whether the user turned it off, or re-arm the toggle when the new
model supports a glossary, for example by making the state a
`$derived`-with-override the way `model` and `diarMode` already are.

### F-04 (high) One client's failed logins lock every other client out

`LoginRateLimiter` in `src/loreline/web/auth.py:174` documents its own intent:
"keyed by client IP rather than globally, so one bad actor probing the password
can't lock the real users at the table out of their own device." Behind the
bundled Caddy that promise does not hold, because `routes/auth.py:26` keys on
`request.client.host`, which is the proxy container for every browser on the
LAN. `X-Forwarded-For` is never consulted, even though the trusted-proxy
machinery for exactly that already exists at `auth.py:66-89` and is used to
decide the cookie's Secure flag.

Reproduced against the live box:

1. Five wrong passwords posted from this machine, through Caddy on port 80:
   `401, 401, 401, 401, 401`.
2. The correct password from the same client: `429 too many attempts, try again
   shortly`, as designed.
3. The correct password from **the browser**, a different machine entirely, in
   the same window: also `429`. A client that had done nothing wrong was locked
   out by someone else's mistakes.

Two consequences. Someone mistyping the shared password on their phone locks the
GM's laptop out for 30s, which is the opposite of what the code intends. And
anyone who can reach the box can hold every login shut indefinitely for the cost
of five requests every 30 seconds, which is a trivial denial of service against
the device the whole table depends on.

Fix: key on the forwarded client address when the peer is a trusted proxy, using
the same helper `client_uses_https` already relies on. Also worth noting:
`_attempts` is never pruned, so the dict grows for the process's lifetime.

### F-05 (medium, disclosure) `/api/system/healthz` is unauthenticated

Verified with no cookie at all:

```
$ curl -s http://10.10.50.55/api/system/healthz
{"status":"ok","version":"0.1.0","uptime_seconds":1772.88,"capture_status":"idle",
 "active_session_id":null,"disk_free_bytes":22794665984,"disk_total_bytes":105089261568,
 "alerts_enabled":false,"diarizer_endpoint":"http://diarization:8001", ...}
```

Anything that can reach the box learns the exact version (so which published
CVEs apply), the disk totals, the uptime, whether a session is recording right
now and its id, the operator's internal diarizer address, and, per
`routes/system.py:116-150`, `stt_error`, which carries the vendor's raw error
text. `/api/capabilities` is also open (200 with no cookie); that one is static
config and much less interesting.

A health endpoint that a monitor can reach without a credential is a reasonable
design choice, but it should then be the liveness subset, not the full
operational snapshot. Suggest splitting: a bare `{"status":"ok"}` unauthenticated,
everything else behind the cookie.

### F-06 (medium, ux) A missing microphone is only reported when you press Start

Found on the live box: the stored input device was
`Jabra SPEAK 410 USB: Audio (hw:2,0)`, while `/api/audio/devices` actually
offered `Jabra SPEAK 410 USB: Audio (hw:0,0)`. The ALSA card index had moved,
which is ordinary on a box that reboots with a different USB enumeration order,
and the stored setting is the device's full name with the index baked into it.

Settings > Client handles this well: the dropdown appends
"Jabra SPEAK 410 USB: Audio (hw:2,0) (not found)", disabled, with the title
"This device is no longer available - pick another one."

The Dashboard does not. It shows a green status dot, a ready Start button and no
hint of a problem. Pressing Start returns, after the attempt:

> input device 'Jabra SPEAK 410 USB: Audio (hw:2,0)' could not be opened for
> recording (No input device matching 'Jabra SPEAK 410 USB: Audio (hw:2,0)').
> Pick another microphone in Settings, or check that nothing else is using it.

That message is excellent, and the failure is clean. The problem is when it
arrives: the moment the table starts playing is the worst time to discover the
mic setting is stale. The Dashboard already fetches the device list nowhere, but
the capture card could check the stored device against `/api/audio/devices` on
mount and warn in the summary line the way it warns about a missing fallback
model.

Related: consider storing a stable identifier rather than the ALSA name. The
same physical Jabra also appears as
`alsa_input.usb-0b0e_Jabra_SPEAK_410_USB_50C971F34D5Dx010900-00.mono-fallback`,
which carries the serial number and does not move.

### F-07 (medium) The live log pane never shows a session's teardown lines

Recorded a real 2-minute session on the box (id `8da95aa0…`, OpenAI /
`gpt-transcribe`, remote diarization). The Dashboard's Logs pane showed exactly
two lines for the whole session and stayed at two after Stop:

```
12:39:09 [info] session.start   session_id=8da95aa0… primary=67afc994…
12:39:09 [info] audio.capture.start device=… rate=16000 device_rate=16000
```

The log the session itself stored has four:

```
12:41:15 [info] audio.capture.stop session_id=8da95aa0…
12:41:15 [info] session.stop      session_id=8da95aa0… status=completed
```

So everything written during teardown is dropped from the live feed. The cause
is that the log bus filters on the manager's current session id
(`src/loreline/logbus.py:40-44`), and Stop has already cleared it before the
teardown lines are written (`src/loreline/session/manager.py:750-759`). The same
filter is on the transcript socket (`web/routes/transcript_ws.py:30-32`), which
means the finals that settle open turns during the drain are dropped too: on a
streaming provider a dimmed interim row stays dimmed on the Dashboard forever,
even though the stored transcript has the settled text.

This matters most in exactly the case the "Finalizing…" copy is written for: the
card says "Transcribing what is still queued", and the pane that would show that
happening goes silent instead.

### F-08 (low, cosmetic) Log values containing spaces are broken into separate tokens

`LogLine.svelte` parses a line as `ts [LEVEL] event k=v k=v` by splitting the
remainder on whitespace, so any value with a space in it is torn apart. The very
first line of every session on this box shows it: the server writes

```
audio.capture.start device=Jabra SPEAK 410 USB: Audio (hw:0,0) rate=16000 …
```

and the pane renders `device=` + green `Jabra`, then `SPEAK`, `410`, `USB:`,
`Audio`, `(hw:0,0)` as five separate unkeyed grey tokens. The device name is the
one thing on that line a person reads, and it is the one thing the renderer
mangles. The stored log dialog shows the same, since it uses the same component.

Fix: split on ` <key>=` boundaries rather than on whitespace, or have the server
quote values that contain spaces.

### F-09 (medium) The SRT and VTT exports contain overlapping cues

Exported the 156-segment merged session as `.srt` and checked the cue table:

- **28 of 156 cues start before the previous cue has ended**, by 9 to 20ms each
  (cue 2 starts at 00:00:03,356 while cue 1 runs to 00:00:03,365). Overlapping
  cues are invalid SRT; players variously drop one, show both stacked, or
  desync.
- **84 of 156 cues are longer than 10 seconds**, the longest 30.1s, carrying
  several hundred characters. As a transcript that is fine. As a subtitle file,
  which is what `.srt` and `.vtt` are for, it is unusable: no player can show a
  30-second paragraph.

The overlap is a bug worth fixing outright (clamp each cue's end to the next
cue's start). The cue length is a design question: if the subtitle formats are
meant to be played against the recording rather than just re-imported, the
exporter should split a long utterance across cues on word timings where the
provider returned them.

Both formats otherwise carry correct headers: `application/x-subrip` and
`text/vtt`, with `Content-Disposition: attachment; filename="<id>.srt"`. `txt`,
`md` and `json` are all correct, and an unknown `fmt` returns 404.

### F-10 (low) A merged session has no `ended_at`, so its header shows no duration

`POST /api/session/merge` produced session `95f12eed…` with
`status: "completed"` and `ended_at: null`, while every other session on the box
carries a real `ended_at`. `SessionHeader.svelte` computes its duration only
`if (session.ended_at)`, so the merged session's header shows the start time and
nothing else, while the player docked at the foot of the same card shows a
37:34 recording. The JSON export repeats the null.

Fix: set `ended_at` on the merged row to the last part's end, which is what the
merged timeline actually runs to.

### F-11 (high) Export, Summarize and Merge always use the original capture, ignoring the selected transcript version

The clearest defect found. Reproduced on session `36583e39…`, which has five
versions:

| Version | Provider / model | Segments |
| --- | --- | --- |
| `original` | OpenAI live capture, session ended in **error** | 683 |
| `f1c47b63` | Deepgram nova-3 | 954 |
| `14306ca7` | OpenRouter google/chirp-3 | 1322 |
| `37d2e05c` | OpenAI gpt-transcribe | 1178 |
| `2680abb4` | OpenRouter microsoft/mai-transcribe-2 | 1346 |

Steps: open the session, click the `2680abb4` row. The transcript panel switches
to it and says "Transcript 2680abb4 · 1346 segments", the row is highlighted,
and the info bar names OpenRouter and `microsoft/mai-transcribe-2`. Now use the
Export button in the header of that same card.

The downloaded `.txt` has **683 lines**, which is `original`. It is not the
version on screen, and nothing in the UI says so. Every version happens to open
with the same sentence here, so a quick glance at the file looks right; the
difference only shows up in the length and in everything after the first minute.

Cause: `web/routes/sessions.py:262` (export), `:223` (summarize) and `:353`
(merge) all call `canonical_transcript(...)`, which is
`variant_view(events, ORIGINAL_VERSION)` with the version hard-coded
(`src/loreline/export.py:56`). The export route has no `version` query parameter
at all, so the frontend could not ask for the right one even if it wanted to.
The machinery is already there: `variant_view` takes a version and is what the
per-version transcript endpoint uses.

Consequences, in order of how much they cost:

1. **Summarize** feeds the LLM the original capture. On this session that is the
   partial transcript of a run that ended in error, when three complete
   re-transcriptions are sitting next to it. The summary is paid for and worse
   than it needs to be, and nothing indicates which text it was built from.
2. **Export** hands back the original in all five formats. The entire point of
   re-transcribing with a better model is to get that text out.
3. **Merge** copies each source's `original`, so a merged session silently
   discards every re-transcription of its parts.

Fix: give the export route a `version` query parameter and the summarize request
body a `version` field, defaulting to `original` for compatibility, and have the
session page pass whatever the version table has selected. For merge, either
take each source's newest complete version or ask which one.

Until then, the honest short-term fix is to label it: the Export button and the
Summarize dialog should say "original" so nobody assumes otherwise.

### F-12 (low) A whitespace-only video prompt gets stuck and cannot be re-seeded

`GenerateVideoDialog.svelte` re-seeds the prompt with `if (open && !prompt)`.
That guard reads an empty string as "not seeded yet" but reads a single space as
real content.

Reproduced: open Generate video (prompt seeded with the 6672-character summary),
select all and type a space, note Generate correctly disables on
`!prompt.trim()`, press Cancel, reopen. The prompt still holds `" "`, Generate is
still disabled, and there is no control that puts the summary back. Only a page
reload recovers it. Clearing the box completely instead does re-seed (verified:
6672 characters return).

Fix: test `!prompt.trim()` in the seeding guard too, or add an explicit
"Reset to summary" button, which is worth having regardless.

### F-13 (ux) The summary is markdown but is rendered as plain text

The stored summary on session `36583e39…` (OpenAI, `gpt-5.6-terra`) begins:

```
## Zusammenfassung der Sitzung

### F-14 (ux) The video prompt is seeded with the entire summary and nothing warns about its length

The same dialog seeds 6672 characters into the prompt box. Its own source
comment says "a summary is a recap, not a shot description - it almost always
wants trimming before it is a good prompt", and the helper text under the box
says "edit freely". But there is no character count, no length guidance and no
warning, and video models generally cap prompts one to two orders of magnitude
below this. A first-time user presses Generate on the seeded value, waits for a
background job, and finds out from the vendor's error.

Suggest a character counter with the selected model's documented limit, or seed
with the summary's first paragraph and offer "use the whole summary".

### F-15 (medium) The per-version logs are nearly empty, and some versions have none at all

The README promises: "Every transcript version, the live capture and each
re-processing run, keeps its own log file, readable from the session page." The
UI repeats it in the dialog: "What this version was produced by, kept per
version". What is actually stored, checked across every version on the box:

| Version | Result | Stored log |
| --- | --- | --- |
| `f1c47b63` Deepgram nova-3 | done, 954 segments | **404, no logs stored** |
| `14306ca7` OpenRouter chirp-3 | done, 1322 segments | **404, no logs stored** |
| `37d2e05c` OpenAI gpt-transcribe | done, 1178 segments | **2 lines** |
| `2680abb4` OpenRouter mai-transcribe-2 | done, 1346 segments | **1 line** |
| `b69b5434` Speaches, run during this test | done, 0 segments | **1 line** |

The two lines under `37d2e05c` are `reprocess.enqueue` and one
`stt.openai_compat.verbose_json_unsupported`. Nothing records that the job
started, how far it got, that it finished, or with what. A run that produced 1178
segments over several minutes leaves no trace of having run.

The `original` version of the errored session `36583e39…` is worse: its log holds
four `reprocess.version.deleted` lines and nothing from the capture itself, so
the log of the one session that failed says nothing about the failure. (Sessions
captured on 9/6 and later do store proper start/stop lines, so part of this is
older sessions predating the feature; the thin re-processing logs are current,
reproduced with a run made today.)

The failure is only visible when you go looking, which is exactly when it costs
most: something about a version looks wrong, you press "Show logs", and get
either one line or "No logs were stored for this version."

Suggest at minimum a start line, a finish line with the segment count and
elapsed time, and the error on the failure path, all tagged with the job id that
already scopes the file.

### F-16 (low, a11y) Closing the version-logs dialog puts focus on the section header, not the button that opened it

Reproduced twice: expand Transcriptions, press "Show logs" on any row, press
Escape. The dialog closes and `document.activeElement` is the
"Transcriptions 2 versions" foldable header button, not the "Show logs" button
that opened the dialog.

For a keyboard user that is a trap with consequences: the next Space or Enter
collapses the whole Transcriptions section rather than doing anything to do with
the row they were looking at, and the focus ring is now somewhere they did not
put it. (I saw the section collapse unexpectedly once during this run and could
not reproduce it deliberately; the misplaced focus is the plausible mechanism
and is itself reproducible.)

### F-17 (medium) "Only show compatible models" filters transcription pickers only

Turned the switch on under Settings > Providers and pressed Save defaults
(confirmed stored: `strict_model_filtering: true`). Then opened both model
pickers on the same page, both pointed at the OpenAI row:

- **Transcription model**: 131 models before, **9 after**. Exactly the
  transcription-capable ones (`gpt-transcribe`, `gpt-realtime-whisper`, the four
  `gpt-4o*-transcribe*`, `gpt-live-transcribe`, `whisper-1`). Correct.
- **Summary model**: **131 models, unchanged**. Still lists `tts-1`, `tts-1-hd`,
  `gpt-image-1`, `gpt-image-2`, `text-embedding-3-small`,
  `text-embedding-ada-002`, `omni-moderation-latest`, `sora-2`.

The switch's own help text is "Hides models that can't do the job you're picking
for", and the copy right next to it names the case ("an OpenAI endpoint lists
image and speech models alongside the transcription ones"). For summaries it
does nothing at all, in either position. Picking `text-embedding-3-small` as the
summary default is possible, savable, and fails only when a summary is actually
run.

Cause: `src/loreline/capabilities.py:428` returns early for any interaction that
is not `transcribe`, so the server applies no filter to the summarize catalogue.

Two smaller notes from the same test:

- The `refreshToken` that busts the client-side model cache is wired to the
  *unsaved* draft (`settings/providers/+page.svelte:697`) while the server
  applies the last *saved* value, so flipping the switch refetches the list and
  gets the same answer back until you press Save defaults.
- The other pickers (`ModelPicker` on the Dashboard, in the Summarize dialog and
  in the re-process row) pass no `refreshToken` at all, so their cached lists
  survive a change to this setting until a full page reload.

### F-18 (medium) A provider's favourite models leak into every interaction's picker

The OpenRouter row's favourites are four transcription models
(`deepgram/nova-3`, `google/chirp-3`, `x-ai/grok-stt-1.0`,
`nvidia/nemotron-3.5-asr-streaming-multilingual-0.6b`). Opening the **Video**
default model picker for that row shows:

```
FAVORITES
  minimax/hailuo-3-max        <- the only real video model
  deepgram/nova-3
  google/chirp-3
  x-ai/grok-stt-1.0
  nvidia/nemotron-3.5-asr-streaming-multilingual-0.6b
ALL
  alibaba/happyhorse-1.0 ... (27 genuine video models)
```

Four speech-to-text models sit at the top of the video picker, under a heading
that says they are the preferred choices. `ModelPicker.svelte` composes its list
as `[stored default, ...provider.favorite_models, ...fetched catalogue]` and the
favourites are one flat per-row list, while a row now serves several
interactions at once. The provider wizard already knows this: its "Load models"
button deliberately merges every interaction's catalogue so a favourite can be
picked "for any of its roles". Nothing narrows them again on the way out.

Selecting one and saving it as the video default produces a video job that fails
upstream. The same leak applies to the summarize pickers.

Fix: filter favourites against the interaction's catalogue in `ModelPicker`
(they are already fetched there), or store favourites per interaction.

### F-19 (medium) A whitespace-only API key is stored as a real key, and then reported as "unreachable"

Full chain reproduced through the wizard:

1. Add provider > Cloud > OpenAI. Type three spaces into the API key field. The
   inline amber warning "No key saved - you won't be able to test or transcribe
   with this provider until you add one." correctly stays up and Load models
   stays disabled, because `hasUsableKey` trims.
2. Press Add provider. The "No API key" dialog appears; choose "Save anyway".
3. The row saves. The API key column now shows `•••`, exactly like a row with a
   real key. The whitespace was stored verbatim, not discarded.
4. Press Edit on that row: the warning is **gone**, the label reads
   "API key - blank = keep current", the field shows the placeholder
   "•••• unchanged", and Load models is now **enabled**. Every signal says the
   row is credentialed.
5. Press Test. Result: **`unreachable`**, red, tooltip
   `could not connect: Illegal header value b'Bearer    '`.

Two problems. The row lies about being configured from step 3 onward, and the
one diagnostic that would set the user straight points at the wrong thing: a
malformed credential is graded `unreachable`, which the page's own design notes
distinguish from `unauthorized` precisely because "a rejected key is not a wrong
base URL". The user is sent to check the network.

Fix: `save()` should send `api_key: form.api_key.trim() || null`, which makes the
whitespace case identical to the empty case that already works correctly. The
provider secret write should reject a blank-after-trim value too, and a header
construction error is worth grading as `unauthorized` rather than `unreachable`.

### F-20 (ux) "Load models" against an unreachable endpoint gives no feedback at all

In the wizard for a self-hosted provider, set Base URL to something that is not
listening (`http://10.10.50.55:9911/v1`) and press Load models. The button reads
"Loading…", then returns to "Load models". No list appears, no message appears,
nothing is disabled or highlighted. The user cannot tell whether the URL is
wrong, the port is wrong, the service is down, or the vendor simply offers no
models.

`loadModels` catches every failure into `availableModels = []`
(`settings/providers/+page.svelte`, the `.catch(() => [])` per interaction plus
the outer `catch`), and nothing renders for an empty list unless favourites
already exist. A self-hosted endpoint is exactly the case where a typo'd URL is
the most likely outcome, and this is the screen where it should be caught.

Suggest an inline message on an empty result: "No models returned from
<base URL>", and a distinct one when the request failed outright, carrying the
error the way the provider Test badge already does.

### F-21 (high) Self-hosted diarization times out on a real session length and fails completely silently

The headline case. Steps, all through the UI:

1. Open a 37-minute session (156 segments), Transcript section, diarizer
   `sherpa-onnx`, endpoint `http://diarization:8001` (the bundled service,
   reported `healthy` by the header health popover and by
   `/api/system/healthz`). Press Diarize.
2. The Diarization column shows `diarizing…`. Correct.
3. About two minutes later the column silently returns to `-`. No message, no
   badge, no banner. The only red text on the page is
   "Last failed job (diarize): remote diarization requires an endpoint", which
   is a **different, earlier, already-fixed** failure (see F-22).
4. "Show logs" on the version returns "No logs were stored for this version."

So a user gets: no diarization, no error, and a stale error about something
else. The reasonable conclusion is that diarization "did nothing", and there is
nothing in the product to contradict that.

What actually happened, from the app container's log (reachable only via
Settings > Services > app > Logs, at a `tail` the UI cannot request):

```
{"job_id":"585df46f…","operation":"diarize","event":"reprocess.failed",
 "session_id":"95f12eed…","level":"error", "exception":"Traceback ...
   File ".../httpcore/_async/http11.py", line 217, in _receive_event
 httpcore.ReadTimeout
 ...
   File "/app/src/loreline/reprocess/jobs.py", line 364, in _run
     job.segments_added = await self._diarize_session(job)"}
```

Three separate defects stack up here:

1. **The timeout is fixed at 120s regardless of audio length.**
   `src/loreline/diarization/remote.py:56` builds the client with
   `timeout=120.0`. Diarizing 37 minutes of audio on a CPU sherpa-onnx service
   cannot finish in two minutes. The job failed at 12:59:45 having started at
   about 12:57:45, exactly on the limit. A tabletop session is hours, so the
   self-hosted diarizer this repo ships is unusable for its own primary use case
   at anything past a few minutes of audio.
2. **A `ReadTimeout` stores an empty error message.**
   `reprocess/jobs.py:531 _job_error_message` passes `UNREACHABLE` verdicts
   through as `str(exc)` on the documented reasoning that such an error "already
   reads fine". `str(httpx.ReadTimeout())` is `''`. So `job.error` is the empty
   string.
3. **An empty error is invisible in the UI.** `TranscriptVersions.svelte`
   computes `lastJobError` as `jobs.filter((j) => j.error)`, and `''` is falsy,
   so the failed job is skipped entirely and the previous failure is shown in its
   place.

The same function's docstring offers a fallback that is also not there: "The
vendor's own words are not lost, they are still in the traceback
`log.exception` writes right after this is called, which lands in the version's
own log file, exactly what 'Show logs' reads." For this session
`GET /api/session/95f12eed…/logs?version=original` returns
`404 no logs stored for this version` (see F-15).

Suggested fixes, in order of value:

- Scale the diarization timeout with the audio duration, or make the diarizer
  endpoint asynchronous (submit, poll) the way video generation already is.
  Two minutes cannot be a constant here.
- Never store an empty error: fall back to `type(exc).__name__` or a written
  sentence ("The diarization service did not answer within 120 seconds").
- Treat a job with `status === 'error'` as reportable regardless of whether its
  message is non-empty, and prefer the *most recent* job over the most recent
  job that happens to carry text.

### F-22 (medium) "Last failed job" can show a stale error while a newer failure is hidden

Same page, same run. `TranscriptVersions.svelte` picks the banner as
`jobs.filter((j) => j.error).sort((a, b) => b.created_at - a.created_at)[0]`,
which is the newest job that *has an error string*, not the newest failure and
not a current one. During the test the page showed

> Last failed job (diarize): remote diarization requires an endpoint

for several minutes after that exact problem had been fixed (endpoint typed in)
and a *newer* job had been queued, run, and failed for an entirely different
reason. There is nothing on screen tying the message to a time or a job, so it
reads as current.

Suggest showing the failure next to the run it belongs to (the Diarization cell
already has the space) with its timestamp, and clearing or greying it once a
newer job of the same operation has been queued.

### F-23 (low) The Services log viewer is drowned by health checks and its tail is not adjustable

Settings > Services > Logs on the `diarization` container returns 200 lines, and
because the app probes `/healthz` on the diarizer every few seconds, all 200 are

```
INFO: 172.18.0.6:53470 - "GET /healthz HTTP/1.1" 200 OK
```

covering roughly the last 17 minutes and nothing else. The container's entire
non-health output is 13 lines (two startup sequences and one shutdown), and none
of it is reachable from the UI: `api.serviceLogs` defaults to `tail = 200` and
the page exposes no control to change it.

This is the screen a user reaches for when a self-hosted service misbehaves, and
in the shipped configuration it can only show them the health probe.

Suggest a tail selector (200 / 1000 / 5000), a "hide health checks" filter, or
simply moving the diarizer probe to a quieter log level in the service itself.

**F-21, follow-up: it is not only long sessions.** Repeated the same test on a
125.9-second recording (the 2-minute session captured during this run). Polled
the job: `running` at 12s, 24s, 36s, 48s, 60s, 72s, 84s, 96s, 108s, then
`error` with `error: ''` at about 120s. Two minutes of audio does not diarize
inside the two-minute timeout on this hardware, so on this box the self-hosted
diarizer effectively never succeeds.

And the page after that failure shows **nothing at all**: the Diarization column
reads `-`, there is no red text anywhere on the page, and no "Last failed job"
line (this session had no earlier errored job to borrow a message from). A
two-minute job ran and failed and the UI is indistinguishable from never having
pressed the button.

I could not verify what the diarizer would do given more time: its port is not
reachable from outside the compose network, so "would a longer timeout succeed"
is untested. What is certain is the timeout, the empty message and the silence.

### F-24 (medium) A webhook alert channel accepts any string as its URL, with no validation anywhere

Settings > Alerting > + > Webhook, typed `not-a-url` into the URL field. The
"Add channel" button enabled (the client only checks `!!chanForm.url`) and the
channel **saved successfully**. It now sits in the table with Target
`not-a-url`, `warning` and above, enabled.

Nothing on the page suggests it is broken. The table has no status column, so
unlike a provider (which shows healthy / degraded / auth failed / unreachable)
an alert channel's health is invisible unless you press Send test, and there is
no periodic check. An operator who typed the URL wrong sets up alerting, sees a
row that looks configured, and never receives an alert. Alerting is precisely
the feature whose silent failure is indistinguishable from "nothing has gone
wrong".

Suggest an `http(s)://` check on save (client and server), and a last-delivery
status column fed by the real send path, not only by the manual test.

### F-25 (low) "Test failed" says nothing about why

Both failing tests during this run, an ntfy channel pointed at a closed port and
the `not-a-url` webhook, reported the same three words: **Test failed**. No
status code, no error, no URL, no tooltip. The provider table right next door
does this properly: it grades the failure and carries the vendor's own sentence
in the badge's tooltip, and the code comment there explains why ("it is what
turns 'something is wrong' into 'fix this field'").

`AlertTestResult` already comes back from the server; surface whatever detail it
carries the same way, and distinguish "could not connect" from "the server said
no".

### F-26 (high) A timed-out diarization leaves the diarizer wedged for every later request

Direct consequence of F-21, and worse than it. After the two diarization jobs
above hit the client's 120s timeout, the bundled sherpa-onnx service stopped
answering anything at all:

```
/api/system/healthz -> diarizer_reachable: false, diarizer_status: "unreachable",
                       diarizer_detail: "no answer within 2s"
```

while `docker ps` (through Settings > Services) still reported the container
`running`, `Up About an hour`. It stayed that way for at least four minutes of
polling at 20s intervals, with no sign of recovering.

The visible effect: the Dashboard's capture card summary flipped from
`Remote - http://diarization:8001` to **`Remote - service not answering`** with a
red dot, and would have kept the operator from trusting a diarization setting
that had been fine ten minutes earlier. Nothing anywhere connects that state to
the job that caused it.

What is happening: the app gives up on the HTTP request at 120s, but the service
keeps working on the audio. It is a single blocking worker, so it answers
nothing else, including the 2-second health probe. Every subsequent diarize
press queues more work behind it. The container's own log shows an earlier
shutdown/startup pair, so this had most likely already happened once before this
test run.

Recovery took a container restart. I did that through Settings > Services, which
worked correctly and is worth recording as a passing test: Stop showed the row
go to `exited`, `Exited (137) 15 seconds ago` (SIGKILL, because it never
answered SIGTERM either), the button flipped to Start, and starting it brought
the health probe back to `healthy` within about 12 seconds.

Fixes worth considering: make the diarize endpoint asynchronous (submit and
poll, as video generation already does) so a long job never holds a connection;
have the service run the model off its request thread so `/healthz` keeps
answering; and have the app refuse to queue a diarize job while the diarizer is
`unreachable`, rather than adding to the queue that is causing it.

**F-02, follow-up: measured at 320px, and it is the only such overflow in the app.**
Repeated the sweep at a 320x720 viewport on a fresh load of each page, looking
for elements that extend past the viewport *and* have no horizontally scrollable
ancestor:

- Dashboard: **1 element**, the Start session button. Card width 272px, button
  spans x=292 to x=401, so it begins 20px past the card's own right edge and only
  **28 of its 109px** are on screen. `document.scrollWidth` is 320, so there is
  no way to scroll to it.
- Session page: 0. The version table is 747px wide but sits in a
  `overflow-x-auto` wrapper that scrolls correctly.
- Settings > Providers: 0.

So the app is otherwise well behaved on a narrow screen and this is one grid.
Two more details worth having:

- Opening the advanced panel (Edit) and closing it again forces a reflow after
  which the button fits (right edge 313). So the state is reachable only on a
  fresh load, which is exactly how a phone at the table would meet it.
- At 390px the two dropdowns are squeezed to 91px and 137px, so any model id
  longer than about 12 characters truncates in the trigger.

### F-27 (low) A merged session is indistinguishable from its oldest source in History

The merge created session `95f12eed…` from `1eac9795…` and `e323e5bc…`. The
History table now holds two rows with an identical Started value
(`8/28/2026, 5:15:45 PM`), identical status (`completed`), identical Primary
(`OpenAI`) and identical empty Campaign. Nothing marks one as a merge or names
its parts. The only way to tell them apart is to open each and count segments.

Suggest a "merged" badge, or a Segments / Duration column, or naming the merged
row after its span rather than its first part's start.

### F-28 (low) Logging in after a redirect always lands on the Dashboard

Logged out, then deep-linked to `/sessions`. The app correctly bounced to
`/login`. After signing in it went to `/`, not to `/sessions`. The intended
destination is discarded (`login/+page.svelte` hard-codes `goto('/')`).

For a device people bookmark a session page on, carrying the target through as a
redirect parameter is a small change with a real payoff.

### F-29 (low) Undiarized transcripts export every line as "Unknown"

`txt` and `md` exports of a session with no speaker labels render every line as
`[00:01] Unknown: ...` and `**Unknown** (00:01): ...`. "Unknown" reads as a
failed identification rather than "this session was never diarized", and it is
repeated on every line of a 683-line file. Omitting the speaker entirely when
the whole transcript has none would read better, and is what the on-screen
transcript already does.


---

## Verified working

### Chaos in der Taverne und Tod des Hünen
- Nach Rotbarts Tod durchsucht die Gruppe ...
- Als Xenon versucht, das Geld mit **Motoricus** unter eine Decke zu bewegen ...
```

`SessionSummary.svelte` renders it in a `whitespace-pre-wrap` paragraph, so the
reader sees the literal `##`, `###` and `**` markers. Every current chat model
answers a "write a summary" instruction in markdown, and the built-in system
prompt (visible under Settings > Providers) does not ask it not to, so this is
the normal case rather than an edge one.

Either render the markdown, or tell the model to answer in plain text. Rendering
it is the better trade: the headings are what make an eight-hour session's recap
skimmable, which is the whole point of the feature.

### Verified, not a bug: the table's On toggle keeps the stored token

Worth recording because it looked like a defect on reading the code:
`toggleChannel` in `settings/alerts/+page.svelte` sends an update body with no
`token` field at all, which would wipe the credential if the server read a
missing token as "clear it". Tested directly: created an ntfy channel with token
`tk_selftest_token_12345` (`token_set: true`), toggled On off from the table,
re-read the channel: `enabled: false, token_set: true`. Toggled back on:
`token_set: true`. Editing the channel and saving with the token box left blank
also keeps it, as the "blank = keep" label promises. The backend treats a null
token as "unchanged" and the flow is safe.

### Verified working: glossary handling

Terms are trimmed and blank lines dropped on save (added `"   Zzz-Test-Term   "`
surrounded by blank lines; stored as `Zzz-Test-Term`, no empty entries). Save on
blur reports "Saved". The original 154-term list was captured before the test
and restored byte for byte afterwards.

One rough edge: duplicates are stored verbatim. Pasting `Xenon` twice into a
list that already contained it produced three copies with no warning and no
dedupe. In a 154-term list shown in a 12-row textarea with no search and no
count, a duplicate is impossible to notice.

### Verified working (no defect found)

Recorded so the coverage is legible:

- Login: empty password and a wrong password both return the inline "invalid
  password"; Enter submits; the correct password lands on the Dashboard.
- Auth guard: deep-linking `/sessions` while logged out redirects to `/login`
  with no shell chrome and no data flash. Logout redirects and the back
  navigation stays out.
- Sidebar collapse persists in `localStorage` (`loreline.nav-collapsed`) across a
  reload; the mobile overlay opens from the hamburger and closes on Escape, on a
  scrim click, and on picking an item.
- Dropdown component: type-ahead ("d" jumps to Deepgram), End jumps to the last
  row, ArrowDown at the end does not wrap, Escape closes the list and returns
  focus to the trigger, and inside a dialog Escape closes only the list. The
  filter box narrows the list and puts the active row on the first hit; an empty
  result reads "No options.".
- Capture gating: the Dashboard's provider list correctly excludes OpenRouter
  (batch only), while the re-process list includes it. Picking
  `gpt-4o-transcribe-diarize` shows its retirement note and disables the glossary
  with the right reason; switching to `whisper-1` withdraws the Inline option,
  auto-falls back to None, and shows "This model returns no speaker labels...".
  A fallback provider with no model blocks Start and reads "AssemblyAI - model
  missing" in red.
- Diarization endpoint validation: emptying it blocks Start with the inline
  message and `aria-invalid`; re-picking Remote prefills
  `http://diarization:8001`; a bogus endpoint produces the amber "No diarization
  service answered at ... (could not connect: All connection attempts failed)"
  after the debounce, and typing a good one clears the verdict.
- Capture lifecycle: Start with a stale device fails with an actionable sentence
  and no session; with a valid device it records (elapsed clock, "0:37 recorded",
  level meter), a second `POST /api/session/start` returns
  `409 a session is already running`, Stop finalizes, and `POST /api/session/stop`
  when idle returns `409 no active session`.
- Logs pane: drag-resize tracks the pointer and persists
  (`loreline.logs-dock`), the ceiling leaves the transcript its 180px, tapping
  the bar folds it, the fold survives a reload, and the bar's own controls
  (including Wrap) do not fold it as a side effect.
- Session page: version rows select correctly and the transcript follows;
  a done-but-empty version is unclickable and carries the "produced no
  segments" title; the original row has no Delete; Delete confirms with the
  exact segment count and both cancel and confirm behave.
- Re-processing: queued a Speaches (local) run from the UI; the row appeared
  without a refresh, the version count went to "2 versions", and the job
  completed.
- Player: play, pause, seek by dragging, and seek by clicking a transcript
  timestamp all work, and the active row highlight follows playback.
- Exports: all five formats return the right content type and
  `Content-Disposition` filename; an unknown `fmt` returns 404.
- Provider wizard: hosting step, kind list per hosting, Back navigation, the
  vendor-label name placeholder with " 2" de-duplication, "Get an API key" link,
  Load models gated behind a key with the right tooltip, the OpenRouter routing
  box (Prefer options, both checkboxes, the no-matching-provider note) shown only
  for OpenRouter, Test all grading every row `healthy`, and delete with its
  destructive confirm.
- Services: core rows have no Start/Stop, optional rows do, logs open and
  refresh, and Stop then Start recovered a wedged container.
- Alerting: per-type field sets, Save gated on the type's key field, Send test
  reporting failure, min level persisting, the table toggle keeping the stored
  token, and delete with its confirm.
- Keyboard: tab order on the Dashboard runs header, nav, capture card,
  transcript controls, log controls (16 stops), every control has an accessible
  name, and disabled buttons are correctly skipped.
- Narrow layouts: at 320px only one element overflows unreachably (see F-02);
  the session page's wide table scrolls properly.

---

## UX suggestions

Beyond the numbered findings, things that work as built but could be better.

**Say which transcript everything is about.** Once a session has more than one
version, the Export button, the Summarize dialog and the Generate video dialog
all act on `original` while the page is showing something else (F-11). Even
after that is fixed, each of those controls should name the version it will use,
the way the transcript panel's info bar already does.

**Give the capture card a pre-flight line.** It already warns about a missing
fallback model, a missing diarization endpoint and an unreachable diarizer. The
one thing it does not check is the microphone, which is the single most common
thing to be wrong (F-06). One line, checked on mount, would turn a failed Start
at the table into a warning while there is still time to fix it.

**Show progress that means something during a long job.** A re-transcription
counts segments as it writes them, which is good. A diarization shows only
`diarizing…` for as long as it runs, and a video generation only `Generating…`.
For jobs measured in minutes, an elapsed timer costs nothing and tells the user
the thing is alive.

**Make the glossary editable as a list.** 154 terms in a 12-row textarea, with
no count, no search, no duplicate detection and save-on-blur only. A term count
in the header, a filter box, and an explicit Save would all pay for themselves,
and duplicates would stop accumulating silently.

**Surface per-campaign glossaries or drop them.** The API has
`/api/glossary/{campaign}` and sessions carry a `campaign_id`, but the UI offers
no way to set a session's campaign or to edit any glossary but the default. The
History table renders a Campaign column that is `-` on every row. Either wire it
up or remove the column.

**Give alert channels a status column.** Providers have one and it is genuinely
useful. Alerting is the feature whose failure is least visible, and it is the
one with no health signal at all (F-24, F-25).

**Reconsider "Unknown" and the batch/realtime hint.** Every line of an
undiarized export says "Unknown" (F-29), and in a model picker with the
compatibility filter off, `text-embedding-3-small` is labelled `batch`, which
reads as a transcription property of a model that cannot transcribe.

**Let the Services log viewer be useful.** A tail selector and a filter, or a
quieter health probe (F-23). As shipped, the diarizer's log is 100 percent
health checks.

**Small things.** The "Saved" confirmation under the microphone picker never
clears, unlike the one under Save defaults, which clears after 2.5s. The
fallback provider dropdown offers the primary provider as its own fallback. The
`sample_rate` and `enabled` fields exist on a provider row with no control
anywhere in the UI, and no cloud provider can be given a custom base URL from
the UI even though the capability config marks those surfaces overridable.

---

## Coverage and limits

**Method.** Every frontend source file was read before testing (the per-file
step lists are in `TEST-PLAN.md`), and three background agents read the backend
in parallel; their notes are in `analysis-backend-api.md`,
`analysis-capabilities.md` and `analysis-session-lifecycle.md` beside this file.
Everything reported above was then reproduced in the browser against the live
box, except where a finding explicitly says it was read from source.

**What could not be tested, and why.**

- **Live transcription of speech.** There is no way for me to make sound in the
  room the box is in. The capture pipeline was exercised end to end (device
  open, frames arriving, `captured_seconds` climbing, level meter wired, stop,
  finalize, stored WAV of 125.9s), and transcript rendering was exercised
  against the stored sessions, but no vendor transcribed live speech during this
  run and no streaming interim was observed on screen. The interim-collapsing
  behaviour in `liveFeed`/`turnKey` is therefore untested here.
- **Streaming connector fallback chains.** Killing a vendor connection
  mid-session was not something I could stage safely on a shared box.
- **WebSocket reconnect UI.** Would need the backend stopped, which would take
  the app down for its actual users.
- **Reduced motion.** The tool can emulate `prefers-color-scheme` but not
  `prefers-reduced-motion`, so the Magic Bento opt-out path is unverified.
- **Whether a longer diarization timeout would succeed.** The diarizer's port is
  not reachable from outside the compose network, so F-21 establishes the
  timeout, the empty error and the silence, but not what the service would
  eventually have returned.
- **Video generation.** The dialog, model catalogue, per-model parameter
  derivation and validation were all tested; no generation was submitted, since
  it spends the user's OpenRouter credit on a job that takes minutes.
- **Deleting sessions.** Blocked by this environment's safety guard on
  destructive actions, so the three sessions created during testing are still
  there (see below). The delete *dialog* was reached and read; only the final
  confirmation was not pressed.

**Changes made to the box during testing.** All deliberate, all reported:

1. **Microphone setting changed** from `Jabra SPEAK 410 USB: Audio (hw:2,0)`,
   which no longer exists, to `Jabra SPEAK 410 USB: Audio (hw:0,0)`, which does.
   This fixed a genuinely broken setting (F-06): capture could not have started
   at all before this.
2. **Three sessions created and left behind**, since I could not delete them:
   `8da95aa0…` (2 min silent capture), `5763f0d2…` (10 s capture for the
   concurrent-start test), and `95f12eed…` (the merge of `1eac9795…` and
   `e323e5bc…`, whose originals are untouched). Safe to delete.
3. **The diarization container was stopped and started** through Settings >
   Services, to recover it from the wedged state described in F-26.
4. Restored exactly as found: the default glossary (154 terms, byte for byte),
   `strict_model_filtering` (back to `false`), all other action defaults, and
   the provider list (the two test providers and both test alert channels were
   deleted). One re-transcription version created on `a4f2f5ab…` was deleted
   again.
5. Failed diarize jobs remain on `95f12eed…` and `8da95aa0…` as job rows; they
   changed no transcript.

---

# Re-test against the deployed build, 2026-09-09

`main` at `566db62` was built by CI and deployed to the box with the web UI's
own Update button. Everything below was re-checked against that running
deployment, not against the branch.

## Verified fixed in production

| # | Evidence |
| --- | --- |
| F-01 | Focusing the health dot and pressing it flips `aria-expanded` false to true and the panel computes to `display: block; visibility: visible`. Escape closes it. Was `display: none` on focus. |
| F-02 | At 320x720 the Start button is full width, spans x=40 to x=280 inside a 320px viewport, and the whole page has zero unreachable overflow. Was x=292 to x=401 with 28px visible. |
| F-03 | `gpt-transcribe` On, switch to `gpt-4o-transcribe-diarize` Unsupported, switch to `whisper-1` **On**. A deliberate untick still survives a model switch (Off stays Off), so the distinction holds. |
| F-04 | Five wrong passwords from this machine: that client then gets 429 on the correct password, while the browser on another machine gets **200**. Both got 429 before. |
| F-05 | `/api/system/livez` returns `{"status":"ok"}` with no cookie; `/api/system/healthz` returns 401. |
| F-06 | With a stored device that no longer exists, the Dashboard summary shows "Microphone: Not found - pick another in Settings" as a link to `/settings/client` with a red dot, before Start is pressed. Start stays enabled by design. |
| F-07 | A recorded session's live Logs pane ends with 4 lines including `audio.capture.stop` and `session.stop`. It stopped at 2 before. |
| F-08 | `device=Jabra SPEAK 410 USB: Audio (hw:0,0)` renders as one value, with `rate` and `device_rate` still parsed. |
| F-09 | 156 segments export as 656 cues: **0 overlapping**, 1 cue over 10s (12.8s), no non-positive durations. Was 28 overlapping and 84 over 10s, longest 30.1s. |
| F-10 | A fresh merge has `ended_at` set, 6.42s, matching its 6.42s of merged audio. |
| F-11 | With version `2680abb4` selected, export returns that version (1346 prefixed lines); the default returns `original` (683); an unknown version returns `404 unknown transcript version 'nope'`. The Export menu header reads "Transcript 2680abb4". |
| F-12 | A whitespace-only prompt re-seeds on reopen. See F-31 below for what this uncovered. |
| F-13 | The summary renders as `h5`/`h6` headings, 40 `<li>` items and `<strong>` runs, with zero literal `##` or `**` left. The renderer contains no `{@html}` and no `innerHTML`. |
| F-14 | The dialog shows "6672 characters" and warns "That is a whole recap. Video models take a scene, not a chapter". |
| F-15 | A re-processing run now writes `reprocess.enqueue`, `reprocess.start` (operation, version, provider) and `reprocess.finished` (segments_added, elapsed_s). It wrote only the enqueue line before. |
| F-16 | After Escape on the version-logs dialog, focus is the "Show logs" button of the row that opened it. It was the section header before. |
| F-17 | With the switch on and saved, the OpenAI summarize catalogue drops from 131 to 102 with zero tts / embedding / image / moderation / sora entries; transcription still narrows 131 to 9; with the switch off, 131 and `tts-1` is back. |
| F-18 | The video picker's Favorites group holds only `minimax/hailuo-3-max`. The four STT favourites are gone, and the seeding path was closed too. |
| F-19 | A provider saved with a three-space key comes back `secret_hint: null, secret_set: false`, and `POST /providers/{id}/secret` with a blank value returns 422. |
| F-20 | Load models against a dead base URL now reads "Could not read this provider's model list: could not check: ConnectError: All connection attempts failed Check the base URL and that the service is running." It showed nothing before. |
| F-22 | The banner reads "Last failed job (diarize, 9/6/2026, 1:14:59 AM): ..." and the failing versions' own Diarization cells read "failed". |
| F-23 | The diarization log card reads "1 of 201 lines shown, 200 health probes hidden"; at 5000 lines, "17 of 1108 shown, 1091 hidden" and the startup history is finally readable. |
| F-24 | A webhook with `not-a-url` is refused `422` with `loc: ["body","url"]` and "webhook url must be an absolute http:// or https:// URL". A valid one saves 201. |
| F-25 | A test against a closed port returns `{"ok":false,"detail":"could not connect: All connection attempts failed"}`. |
| F-27 | The merged row carries a "merged" badge titled "Assembled from 2 sessions, which are still here in their own right." and a 0:06 duration that separates it from its 0:02 source at the same timestamp. |
| F-28 | Logged out, `/settings/glossary` redirects to `/login?next=%2Fsettings%2Fglossary` and signing in lands on the glossary. `?next=//example.com/pwned` falls back to `/` on the same origin. |
| F-29 | An undiarized session exports as `[00:01] Hier, sag mal irgendwas.` with no "Unknown:" prefix. |

Both new columns landed cleanly: every session row carries `merged_from` and
`summary_version` (migrations v19 and v20), and a summary stored before the
change reads "version not recorded" rather than claiming `original`.

## New findings from the re-test

### F-30 (medium) The update button cannot deploy a change to the bundled diarization service

This is why F-21 and F-26 did not take effect on the first deploy, and it is a
gap in the deploy story rather than in the fix.

The button hands the job to the updater service, which runs
`deploy/update-fast.sh`: `git pull --ff-only`, `docker compose pull app`,
`docker compose up -d --no-build app`. Only the `app` service. The bundled
diarization service is not pulled from a registry at all, it is **built locally**
from `services/diarization/`, so a release that changes it is fetched by the
`git pull` and then never built.

Measured on the box after a successful button deploy: the checkout was at
`566db62`, my merge, while the diarization container still reported
`created=2026-09-08T12:08:09Z` and `grep -c BoundedSemaphore /app/app.py` was
`0`. The new code was on disk and not running. During a diarization the old
container still went unreachable exactly as F-26 described, four polls in a row,
while `livez` stayed 200.

`docker compose --profile diarization up -d --build diarization` fixed it, and
`grep -c BoundedSemaphore` then returned 1.

The README frames the narrow scope as a feature ("Only the `app` service is
touched, so Caddy, the docker proxy and any enabled profile services keep
running"), and as isolation it is right. The problem is that nothing tells you
the other half of the release was skipped. `deploy/update.sh`, the from-source
path, does rebuild the diarization image when its profile is in `.env`, so the
two update paths differ in what they actually deploy and only the narrower one
is wired to the button.

Suggest, in rough order of value: have the updater compare the pulled revision's
`services/` tree against what is running and say plainly when a profile service
needs rebuilding; or rebuild changed profile services as part of the button's
run; or, at minimum, document in the README's "Updating" section that
`services/diarization/` changes need the manual rebuild, next to the existing
note that the updater does not update itself.

### F-31 (low) Clearing the video prompt refilled it from under the cursor

Found while verifying F-12 on the deployed build. `GenerateVideoDialog` seeded
with `$effect(() => { if (open && !prompt.trim()) prompt = summary })`. The
effect reads `prompt`, so clearing the box re-ran the very effect that seeds it:
select all, delete, and the whole 6672-character recap was back before the first
keystroke. Its own comment claimed it re-seeded "each time the dialog opens, but
never while it is open", which the code did not do.

The empty-string case behaved this way before the F-12 fix too; F-12 made the
whitespace case match it rather than staying stuck, so the visible symptom moved
rather than appeared. Fixed properly on the branch: seed on the transition into
open, read the prompt untracked, and leave "Reset to summary" as the deliberate
way back. Not yet deployed.
