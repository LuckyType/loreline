# Session lifecycle and background jobs, behaviour map

Input for a manual black-box UI test run against a live deployment. Everything
below is read off the source in this checkout, not off a running box. It says
what should happen, in what order, and what each state and failure mode looks
like on screen.

Companion to `docs/testing/TEST-PLAN.md`, which walks the UI file by file. This
one walks the *lifecycle* instead, so the two overlap on the dashboard and
diverge everywhere else.

Legend, same as the test plan: `[neg]` negative or error path, `[api-only]` not
reachable from the browser, `[obs]` observation only, no click.

Key source files, for anyone who wants to check a claim:

| Concern | File |
| --- | --- |
| Capture lifecycle | `src/loreline/session/manager.py` |
| Streaming live path | `src/loreline/session/streaming.py`, `src/loreline/stt/streaming.py` |
| Utterance live path | `src/loreline/stt/router.py` |
| Re-processing jobs | `src/loreline/reprocess/jobs.py` |
| Video jobs | `src/loreline/video/jobs.py`, `client.py`, `vendors.py` |
| Exports | `src/loreline/export.py`, `src/loreline/web/routes/sessions.py` |
| Storage | `src/loreline/persistence/audio_store.py`, `log_store.py`, `repositories.py`, `database.py` |
| Alerts | `src/loreline/monitoring/alerts.py` |
| Logging and per-version log files | `src/loreline/logging.py` |
| Self-update | `src/loreline/updater/updater.py` |

---

## 1. Capture lifecycle

### 1.1 The states that exist, and the ones that are real

`SessionStatus` (`src/loreline/models.py:52-58`) declares five values:
`idle`, `capturing`, `stopping`, `completed`, `error`.

Only four of them can ever be observed.

* `idle` and `capturing` are what `SessionManager.status()` returns, and it is a
  pure function of one in-memory field: `capturing` while `self._runtime is not
  None`, `idle` otherwise (`src/loreline/session/manager.py:433-434`). This is
  what `/api/system/healthz` reports as `capture_status` and what the header
  badge and the capture card read.
* `completed` and `error` are stored on the session row by
  `SessionRepository.finish` at the end of a session, and are what the history
  list and the session page show.
* **`stopping` is dead.** No code anywhere assigns it, backend or frontend. The
  capture card's amber `Finalizing` state is local component state in the tab
  that pressed Stop; a second tab or a refresh during the stop sees the idle
  start form. See SUSPECTED ISSUES.

There is at most **one** capture session per process. The manager holds a single
`_Runtime` behind an `asyncio.Lock`.

### 1.2 Start

`POST /api/session/start` -> `SessionManager.start`
(`src/loreline/session/manager.py:557-660`). Order matters, because several
steps can fail and each one fails differently:

1. Take the manager lock. If `_runtime is not None`, raise `SessionActiveError`
   -> **409 `a session is already running`**.
2. Resolve providers (`_resolve_providers`, :519-555):
   * unknown primary or fallback -> **404 `unknown primary provider '<id>'`**
   * disabled primary or fallback -> **409 `primary provider '<id>' is disabled`**
   * a kind that cannot drive a live capture (OpenRouter today) -> **400
     `... cannot drive a live capture - it is available for post-session
     re-processing only`**
   * `Inline (from STT)` diarization on a model that returns no speakers ->
     **400 `model '<m>' on '<name>' returns no speaker labels - inline
     diarization would produce an unlabelled transcript`**
3. Build the capture source and the VAD detector (`_default_capture`, :147-155).
   This constructs `SileroVad`, which imports torch/numpy/silero and loads the
   ONNX model **synchronously on the event loop**. See SUSPECTED ISSUES.
4. **Pre-flight the microphone** (`_preflight_capture`, :726-748). The real
   device is opened at its own rate, a resampler is built, and both are thrown
   away. A refusal here is **400** with the sentence `<the default input
   device|input device 'X'> could not be opened for recording (<err>). Pick
   another microphone in Settings, or check that nothing else is using it.`
   This is the "mic is missing" answer: the session is never created, nothing is
   written, and the card stays on the start form with a red line under it.
5. Build the STT backends. A provider kind with no registered connector ->
   **400** (`SessionConfigError`).
6. Load the effective glossary, but only when `use_glossary` is on.
7. Build the diarizer. An invalid diarization config (for example `remote` with
   no endpoint) -> **400**.
8. Create the `Session` row with `status=capturing`, open the WAV writer, set
   `audio_path`, insert the row.
9. Start the persist task, the live path task and (inside it) the capture task.
10. Return **201** with the `Session`.

The response is the session row. The UI reacts to it only by re-polling health;
the capture card flips to `Recording` on the next `/api/system/healthz` answer
(polled every 5000 ms from the root layout), or immediately after a successful
start because `start()` forces a refresh.

**A second Start while one runs** is refused with 409 and the message above,
rendered as a red line at the bottom of the capture card. Note the card does not
re-poll on failure, so it keeps showing the idle start form until the next 5 s
health tick. See SUSPECTED ISSUES.

### 1.3 The one capture loop

`_capture_utterances` (`src/loreline/session/manager.py:1123-1191`) is the same
loop for both live paths. Per frame, in this order:

1. `stats.record(frame)` - drives `captured_seconds` and
   `capture_last_frame_age` on `/healthz`.
2. `_LevelWatch.record` - peak held across the interval, published to the level
   bus at most every 100 ms. This is what `/ws/audio/live-level` relays.
3. `audio_writer.append_frame` off the event loop - **every** frame including
   silence goes into the continuous WAV.
4. `_DiskWatch.check` - reads free space at most every 30 s, alerts once per
   crossing, re-arms at 1.1x the floor.
5. Silero VAD off the event loop.
6. `frames.frame(pcm, ts, is_speech=...)` when a `StreamPath` is installed.
7. `chunker.feed(...)`; a completed utterance is marked in the WAV index
   sidecar and then dispatched (see below).

**Dispatch** (`_dispatch`, :1097-1105): with no `StreamPath` the utterance goes
onto a bounded queue of 64 (`_UTTERANCE_QUEUE_MAX`) for the router. With a
`StreamPath` the utterance goes nowhere until the session has handed over -
the vendor is deciding the turns, so the utterance is only the WAV index's
business. `_offer` drops the **oldest** queued utterance when the queue is full
and logs `capture.utterance.dropped`, which is visible in the live log pane as
lost transcript text with intact audio.

However capture ends (stopped, cancelled, or the source raising before it ever
yielded), `_close_capture` (:1194-1224) flushes the chunker, calls
`frames.done()` and delivers the `_CAPTURE_DONE` sentinel. Both are inside
`finally` blocks, because skipping either leaves a consumer blocked for the life
of the process.

### 1.4 Which live path a session takes

`_start_live_path` (:662-698). The **shape of the primary connector** decides,
nothing else:

* `is_streaming(primary)` true -> `StreamPath` (frames straight to the vendor,
  interims published while a turn is open). Five connectors are streaming today:
  AssemblyAI, Deepgram, Gemini Live, OpenAI Realtime, xAI.
* otherwise -> `SttRouter` (one STT call per VAD-cut utterance, finals only).

### 1.5 What the transcript WebSocket carries

`GET /ws/transcript` (`src/loreline/web/routes/transcript_ws.py`). Auth by
cookie when auth is enabled; a bad or missing cookie closes with 1008.

* **With `?session_id=<id>`** the socket carries every event of that session,
  every version: the live capture, gap markers, and every re-processing run.
  The client routes them by `source`.
* **Without `session_id`** (the dashboard) the socket carries only the *running*
  capture's rows, and only those whose `source` is not a `reprocess:` or
  `diarize:` tag. The filter re-reads `manager.current_session_id()` per event.

Every frame is one `TranscriptEvent` serialized as JSON, with these fields
(`src/loreline/models.py:205-235`):

| Field | Meaning |
| --- | --- |
| `session_id` | which session |
| `source` | the provider id that produced it, or `gap`, or `reprocess:<job_id>`, or `diarize:<version>` |
| `text` | the words |
| `words` | list of `{text, start, end, confidence, speaker}`, often empty |
| `speaker` | diarization label, or null |
| `start_ts`, `end_ts` | seconds from session start (rebased against `started_mono`) |
| `is_final` | false for an interim, true for a settled turn |
| `turn_id` | the replace key; null on the utterance path |

**Interim vs final.** Only the streaming path produces interims. A vendor turn
arrives as a growing `is_final=false` event, at most one per 300 ms
(`StreamConfig.interim_interval_s`), then exactly one `is_final=true` when the
turn closes, all carrying the same `turn_id`
(`<provider_id>:<connection_generation>:<vendor_handle>`). Both the browser and
the database key on `turn_id`: the table upserts on
`(session_id, source, turn_id)` so the transcript holds one row per turn, and
the live feed replaces the held row in place. On the utterance path `turn_id` is
null, every event is final, and every event appends.

**Gap markers.** `source == "gap"`, `is_final=true`, no `turn_id`, and the text
is written by the backend: `"<N>s of audio was not transcribed: the connection
to <provider> dropped."` (`src/loreline/stt/streaming.py:426-449`). It spans
from where transcription stopped to where it resumed, so it is only published
once something transcribes again, or when the path gives up. The pane renders it
as an amber italic line with a dashed rule. Gap markers are excluded from
exports, summaries and diarize jobs by `export.final_rows`.

**Stage by stage, what a tester should see on the dashboard:**

| Stage | On `/ws/transcript` | On screen |
| --- | --- | --- |
| Start pressed, socket already open | nothing yet | card flips to `Recording`, clock from 0:00 |
| Someone speaks, streaming connector | interim events, same `turn_id`, growing text | one dimmed italic row growing in place |
| Turn closes | one final event, same `turn_id` | the row settles, loses the dim |
| Turn closes, remote diarization | the final again, same `turn_id`, now with `speaker` | the same row gains a speaker name, within ~10 s |
| Someone speaks, utterance path | one final per VAD utterance, no `turn_id` | a new row per sentence, never dimmed |
| Connection dies and is re-established | a `gap` event covering the lost span | an amber italic line |
| Stop pressed | the settle finals for open turns | **see SUSPECTED ISSUES: the dashboard does not receive these** |

### 1.6 The fallback chain when a streaming connection dies

Two nested loops. Inside one provider (`TranscriptStream.run`,
`src/loreline/stt/streaming.py:626-678`):

1. A write that times out (5 s), a queue that fills (250 frames, about 5 s), or
   a reader that ends is a dead connection. So is voiced audio written with
   nothing at all coming back for 20 s (`watchdog_s`) **and** the local VAD gone
   quiet for 2 s (`quiet_grace_s`) - the second condition is what stops a long
   sentence being read as a dead socket.
2. A dead connection opens a pending gap, tears the socket down (each step
   bounded at 5 s), settles every open turn as a final, and reconnects after
   `reconnect_backoff_s * attempts`.
3. `max_reconnects` is 3. A connection that stayed up 30 s
   (`healthy_after_s`) earns a fresh budget, so an evening of ordinary blips
   never fails over.
4. Out of budget -> `StreamOutcome.DEAD`. The vendor said "I do not stream this
   model" -> `StreamOutcome.UNSUPPORTED`. Microphone stopped -> `ENDED`.

Across providers (`StreamPath._run`, `src/loreline/session/streaming.py:272-296`):

1. `DEAD` -> retire this provider, alert **`Transcription degraded`** with
   `"Live transcription lost its connection to <name>. Audio keeps recording;
   the session can be re-transcribed later."`, and try the next streaming
   provider (the fallback, if it streams).
2. `UNSUPPORTED` on a real backend -> keep it as the *call-shaped* candidate and
   stop trying to stream. Nothing is wrong with the provider; it just cannot
   stream this model.
3. When the streaming candidates are exhausted:
   * a call-shaped candidate exists (the unsupported primary, or a batch
     fallback) -> `PathEnd.HANDOFF`. The manager builds an `SttRouter` with the
     same config the session would have had from the start, flips the sink to
     queue utterances again, and alerts **`Transcription degraded`** with
     `"Live transcription is running through <name> one utterance at a time:
     text arrives after each sentence rather than while it is spoken."` From the
     **next completed utterance** on this is an ordinary session. Text stops
     arriving mid-sentence and starts arriving per sentence, which is the
     visible tell.
   * nothing left -> `PathEnd.EXHAUSTED`. **Capture keeps running**, the alert
     **`Transcription stopped`** (ERROR) fires with the vendors' own words, and
     `/healthz` starts returning `stt_error`. The dashboard shows the red
     `Live transcription stopped: <vendor message> Audio is still being
     recorded, so the session can be re-transcribed once this is fixed.`

On the utterance path the equivalent chain is per utterance: each provider is
tried in order with a 30 s timeout; a *terminal* failure (no credits, rejected
key, missing model) retires that provider immediately; three consecutive
utterances that nothing transcribed set `stt_degraded_since` and fire
**`Transcription degraded`** once; every provider retired raises
`ProvidersExhaustedError` and lands in the same `_stt_exhausted` as above.

The session's status is **still `completed`** at the end of an exhausted
session. Losing every provider is not an error: the audio survives and can be
re-transcribed.

### 1.7 Stop

`POST /api/session/stop` -> `SessionManager.stop` (:750-759), then `_finish`
(:807-908). With nothing running the route answers **409 `no active session`**.

`_finish` in order, all of it bounded:

1. `source.stop()` - the frame generator ends within 0.5 s.
2. `await asyncio.wait_for(live_task, timeout=30)` (`_STOP_DRAIN_TIMEOUT_S`).
   * On the streaming path: the stop sentinel reaches the send loop, the vendor
     is flushed, open turns get up to 5 s (`final_wait_s`) to produce their
     finals, the socket is closed (<=5 s), whatever is still open is published
     as a final anyway, the trailing gap is closed, and in-flight diarization
     labels get 5 s (`_DIARIZE_DRAIN_S`) before being cancelled.
   * On the utterance path: the router works through whatever is queued. A dead
     backend with a deep backlog is exactly what the 30 s cap is for; the
     timeout cancels the live task and logs `session.stop.drain_timeout`. The
     skipped tail stays re-transcribable from stored audio.
   * A `DiskFullError` here is **not** a crash: status stays `completed`.
   * Any other exception -> status `error`.
3. Close the session bus, await the persist task, then delete any leftover
   interim rows for the session. Order matters: the settle finals must land
   before the interim sweep.
4. Close the audio writer off the event loop (patches the WAV header, writes the
   final index sidecar).
5. Close every STT backend and the diarizer, suppressing failures.
6. `sessions.finish(session_id, status)` - sets `status` and `ended_at`.
7. Fire the closing alert if there is one: `Recording stopped: disk full`
   (ERROR), or `Session error` (ERROR). Never skipped even if a step above
   failed, because a push notification is the only channel that still works when
   the disk is what broke.

Every step from 3 onward is wrapped in `_keep_finalizing`: a failure is logged
and the next step runs anyway, because a row left at `capturing` forever is
worse than a session that ended badly.

The response is the in-memory `Session` with `status` updated but **`ended_at`
still null** - only the DB row has it. A page that trusts the stop response
shows no end time until it refetches.

### 1.8 A capture that ends itself

`live_task.add_done_callback(self._live_finished)` (:659). A live task that
raised means the frame source is gone and the recording is over. `_live_finished`
(:761-780) spawns `_end_unattended`, which takes the lock, claims the runtime
and runs **exactly the same `_finish`**. So a mic that is unplugged mid-session,
or a disk that fills, ends the session on its own within moments, with the same
stored status and the same alert Stop would have produced. A cancelled task
(that is Stop doing its job) and a task that ended cleanly are both ignored.

What this does **not** cover: a device that stays open and simply stops
delivering frames. PortAudio's callback goes quiet, the read loop keeps timing
out every 0.5 s and looping, and nothing raises. The session reports `capturing`
forever. The only signal is `capture_last_frame_age` climbing on `/healthz`,
which the capture card turns into the red banner `No audio has reached the
recorder for N seconds...` after 3 s. Nothing stops the session and no alert is
sent.

### 1.9 Startup sweep

On every boot (`src/loreline/web/app.py:263-283`):

* `SessionRepository.mark_interrupted` flips every row still at `capturing` to
  **`error`** with an `ended_at`, and deletes that session's interim rows. This
  is what a killed process leaves behind.
* `ReprocessManager.reconcile` and `VideoManager.reconcile` flip every
  `queued`/`running` job to `error` with `interrupted by restart` /
  `interrupted by a restart`.
* `LogStore.prune` removes log directories for sessions that no longer exist.
* `recover_orphaned_indexes` rebuilds a **missing** index sidecar for any stored
  WAV that has a session row, by re-running VAD over the whole recording in a
  worker thread. It alerts **`Session audio recovered`** (INFO) per rebuild.
  Note it only fires when the sidecar is *absent*: a session killed after its
  first utterance has a stale-but-present sidecar and is never rebuilt, so
  re-transcription covers only up to the last completed utterance.

---

## 2. Transcript versions

### 2.1 What a version is

A session's transcript exists in **versions**. A version is a set of rows in
`transcript_segments` selected by their `source` tag
(`src/loreline/export.py:23-56`):

| Version | `source` on its rows | Created by |
| --- | --- | --- |
| `original` | the provider id that produced the row, plus `gap` | the live capture |
| `<job_id>` | `reprocess:<job_id>` | one re-transcribe job |
| a relabeling of either | `diarize:<version>` | one re-diarize job |

`variant_rows(events, version)` returns a version's raw rows. For `original`
that is every row whose source is neither a `reprocess:` nor a `diarize:` tag,
which is why a session that failed over between two vendors still reads as one
original even though its rows carry two different provider ids.

`variant_view(events, version)` is what readers use: if a `diarize:<version>`
copy exists it **supersedes** the raw rows entirely, otherwise the raw rows are
returned. So re-diarizing does not add a version, it replaces one version's
speaker labels. Re-running a diarize job against the same target replaces the
previous relabeling (`delete_source` then insert).

`final_rows(events)` drops interims and gap markers. Everything downstream of
the capture reads through it: exports, summaries, and the rows a diarize job
relabels. The browser deliberately does not, because an interim on screen is the
point.

### 2.2 What creates a new version

Only a **re-transcribe** job. `POST /api/reprocess` with
`operation: "transcribe"` creates a `reprocess_jobs` row whose id becomes the
version id, and every event the job's router publishes is stored tagged
`reprocess:<job_id>` (`src/loreline/reprocess/jobs.py:481-508`). Every run is
kept; nothing is ever overwritten, so the original and any number of
re-transcriptions stay comparable side by side.

A **re-diarize** job creates no version. It rewrites one existing version's
final rows into a `diarize:<target>` copy.

### 2.3 What each version stores

The `reprocess_jobs` row (`src/loreline/models.py:370-386`) carries
`provider_id`, `model` (what actually ran, not what the provider row happened to
hold), `use_glossary`, `target`, the `diarization` config, `status`,
`created_at` / `started_at` / `finished_at`, `segments_added`, and `error`.

The segments themselves carry `text`, `words`, `speaker`, `start_ts`, `end_ts`,
`is_final` and `turn_id`. A re-processed version's rows are all final and carry
no `turn_id`, because the router path never revises a row. Timestamps are
rebased against the session's `started_mono`, so a re-transcription lines up with
the same audio clock the original does.

Two versions of one session **will not line up segment for segment**: a
re-transcription is cut at the recording's VAD utterance boundaries, while a
streamed original carries the vendor's turns. The versions table says so in a
footnote when it detects both.

### 2.4 The per-version log file

`LogStore` (`src/loreline/persistence/log_store.py`) keeps
`<data_dir>/logs/<session_id>/<version>.log`, one file per version:
`original.log` for the live capture, `<job_id>.log` for each re-processing run.

The wiring is `bind_log_context` plus the structlog tap
(`src/loreline/logging.py:63-98`). A task binds `session_id` (and `job_id` for a
job) once at the top of its body; asyncio copies the context into every task it
spawns, so the router, the STT connectors and the VAD all get attributed without
knowing sessions exist. The tap renders the line, forwards it to the live log
broadcaster, and appends it to the file for `job_id or "original"`. Lines with
no `session_id` are broadcast but never stored.

Retention: the file goes when the version goes
(`DELETE /api/session/{id}/transcript?version=`) and the directory goes when the
session goes. A startup sweep prunes directories whose session no longer exists.

Read back through `GET /api/session/{id}/logs?version=`, which answers **404
`no logs stored for this version`** when the file is missing or unreadable.

### 2.5 What the session page should show

* A **Transcriptions** table, one row per version, oldest first, with columns
  Transcript, Provider, Model, Diarization, Segments, Created, Status.
* Row 1 is always `original`. Its Status is derived from the *session*, not from
  a job: `live` while the session is capturing or stopping, `error` for an
  errored session, `complete` otherwise. It has a **Show logs** action and no
  Delete, because the original is the live capture and nothing can produce it
  again (refused server side with 409, not merely hidden).
* Each re-transcribe job row shows its `status` badge (`queued` / `running` /
  `done` / `error`, with the error message in the badge's tooltip), its segment
  count (rendered `"<n> so far..."` in muted text while the job is in flight, on
  purpose: it is a raw count, not a percentage, because two models segment the
  same audio differently and there is no honest denominator), and **Show logs**
  plus **Delete**. Delete is disabled while the job is in flight.
* The Diarization cell shows `diarizing...` while a diarize job targets that
  version, otherwise the last successful pass's label, otherwise `-`.
* Clicking a row selects that version and the Transcript panel refetches
  `GET /api/session/{id}/transcript?version=`. A done-but-empty version is
  deliberately inert with the tooltip about having produced no segments.
* Under the table: the "will not line up one to one" footnote when applicable,
  and a red `Last failed job (transcribe|diarize): <error>` line for the most
  recent failure. **A diarize job has no row of its own**, so that red line is
  the only place a failed diarization is reported.

Deleting a version takes its segments, its `diarize:<version>` relabeling, its
job rows (including diarize jobs aimed at it) and its log file. It is refused
with **409** while any job is still writing it.

---

## 3. Re-processing

`POST /api/reprocess` (202) -> `ReprocessManager.enqueue`
(`src/loreline/reprocess/jobs.py:231-278`). Both operations are enqueued through
the same route and the same table.

### 3.1 Validation, before any job row exists

| Condition | Answer |
| --- | --- |
| unknown session | 404 `unknown session '<id>'` |
| no stored audio (WAV or index sidecar missing) | **409 `session '<id>' has no stored audio`** |
| `transcribe` with an unknown provider | 404 `unknown provider '<id>'` |
| `transcribe` with no `model` | 422 (pydantic: `model is required for a "transcribe" job`) |
| `diarize` against a version with no rows | 404 `unknown transcript version '<t>'` |

Note the audio check is `wav_path.exists() and index_path.exists()`. A session
whose process died before its first utterance was marked has a WAV but no
sidecar, and re-processing is refused until the startup recovery sweep rebuilds
it.

### 3.2 Job states

`JobStatus`: `queued` -> `running` -> `done` | `error`. Nothing else exists.

* `queued` is written by `enqueue` and lives only until the spawned task first
  runs, which is essentially immediate. There is no queue: **every job starts at
  once**.
* `running` is set at the top of `_run`, with `started_at`.
* `done` or `error` is set in `_run`'s `finally`, with `finished_at`, and a
  failure stores `error`.
* A process restart flips every `queued`/`running` row to `error` with
  `interrupted by restart`.

### 3.3 What a transcribe job does

1. Builds a connector through `stored_audio_backend`, which forces
   `prefer_batch=True`. This matters: a model whose capability entry prefers
   realtime (Deepgram nova-3, AssemblyAI universal-3-5-pro) would otherwise open
   a streaming socket and push a whole recording into it as fast as the file
   reads, which realtime endpoints handle badly. A model whose *only* transport
   is streaming still gets it, deliberately.
2. Builds its own diarizer wrapped in `_JobBankDiarizer`, which substitutes a
   job-private speaker-bank id (`<session_id>:job:<job_id>`) so the job's
   `aclose` cannot delete the live capture's remote speaker bank.
3. Loads the glossary only when `use_glossary` is on.
4. Reads the stored utterances off the index sidecar in a worker thread. **The
   whole recording is loaded into memory** as one PCM blob and then sliced.
5. Drives an `SttRouter` over them, persisting each event tagged
   `reprocess:<job_id>` and republishing it on the app-wide transcript bus, so a
   `/ws/transcript?session_id=` subscriber sees the version fill up live.

### 3.4 What a diarize job does

1. Reads the **whole continuous WAV** into memory in a worker thread.
2. One diarizer call covering the entire session, with `min_speakers` /
   `max_speakers` and a bank id of `<session_id>:<target>`, so re-running against
   a different version does not inherit another pass's voices.
3. No segments back -> returns 0 and the job is `done` with nothing changed.
4. Otherwise: take `final_rows(variant_rows(events, target))`, relabel each with
   `assign_speakers`, tag them `diarize:<target>`, delete the previous
   `diarize:<target>` rows, and insert the new ones.

### 3.5 Progress reporting

`_LiveSegmentCount` writes `segments_added` back to the job row at most once per
second, and the completion write in `_run`'s `finally` persists the final count
either way, so no run ends on a stale number.

The page polls `GET /api/reprocess?session_id=` every **1500 ms**, but only
while at least one job is queued or running, and tears the interval down on the
falling edge.

Two things a tester should expect:

* For a transcribe job the count climbs steadily from the first utterance.
* For a diarize job the count sits at **0 for the whole diarization call**, which
  is the long part, and then climbs quickly as the relabeled rows are written.
  A long session can therefore show `0 so far...` for minutes with nothing wrong.

### 3.6 Cancellation

**There is none.** No cancel button, no cancel endpoint
(`src/loreline/web/routes/reprocess.py` exposes only `POST ""`,
`GET /{job_id}`, `GET ""`), and `ReprocessManager` cancels its tasks only from
`aclose()` at process shutdown. The only ways to end a running job are to wait,
or to restart the service, which marks it `interrupted by restart`.

### 3.7 Failure

Any exception marks the job `error`, stores a message, and logs the traceback
into that version's log file. The stored message is deliberately shaped for a
reader (`_job_error_message`, `src/loreline/reprocess/jobs.py:543-557`): an
`UNREACHABLE` verdict (refused, timed out, no such host) is passed through as
raised, because it already reads fine; anything else, notably a diarizer that
answered with an HTTP error status, becomes `The diarization service answered but
could not process the audio (is it configured correctly?)`. The vendor's own
words are not lost, they are in the traceback that Show logs reads.

A partially written version is **kept**, not rolled back. A transcribe job that
died halfway leaves an `error` row and however many segments it managed. Delete
the version to clean up.

### 3.8 Concurrency

Concurrent jobs are **allowed and unbounded**. `enqueue` does no check against
running jobs, so several re-transcriptions of one session can run at once, each
producing its own version, and a diarize job can run alongside them. The only
guard anywhere is `delete_version`, which refuses to delete a version a
queued/running job is still writing.

There is also **no guard against the session that is currently capturing**. See
SUSPECTED ISSUES.

---

## 4. Speaker renaming, summaries, video generation

### 4.1 Speaker renaming

* Request: `PUT /api/session/{id}/speakers` with `{"names": {label: display}}`.
  The whole map is replaced, stored as JSON on `sessions.speaker_names`.
* No job, no state: it is a single write that returns `{"ok": true}`.
* **The transcript rows are never modified.** The map is applied at read time by
  `relabel_speakers`, which the export route and the summarize route both call.
  `GET /api/session/{id}` and `GET /api/session/{id}/transcript` return the raw
  labels, so the browser applies the map itself for display.
* Where a rename becomes visible: the transcript list, the transcript search,
  every text export, and the text handed to the summarizer. Not the versions
  table and not the player.
* Two UI paths reach the same endpoint: the **Rename speakers** dialog (labels
  collected from the *currently shown version's* distinct speakers) and clicking
  a speaker name inline in the transcript. They disagree about merge semantics;
  see SUSPECTED ISSUES.

### 4.2 Summaries

* Request: `POST /api/session/{id}/summarize` with `{provider_id, model,
  reasoning_effort?}`.
* **Synchronous.** There is no job row, no polling and no progress. The HTTP
  request is held open for the whole LLM call, which has a 120 s upstream
  timeout (`src/loreline/llm.py:27`).
* Input: `relabel_speakers(final_rows(canonical_transcript(...)))` rendered
  through `to_txt`. Always the **original** version, whichever version is
  selected on screen.
* Errors: 404 session/provider not found, **400 `provider is not an LLM
  provider`**, **400 `session has no transcript`**, **502** with the LLM's own
  message. The dialog stays open and shows the message above its footer.
* Result: stored on the session row as `summary`, `summary_provider`,
  `summary_model`, and returned in the response. It renders in the **Summary**
  section as plain pre-wrapped text; the section's fold meta becomes
  `<provider> . <model>`.
* While running: the dialog's button reads `Summarizing...` and is disabled.
  Nothing else is disabled and there is no spinner.

### 4.3 Video generation

* Request: `POST /api/video` (202) with `{session_id, provider_id, model,
  prompt, duration?, resolution?, aspect_ratio?, generate_audio, seed?}`.
* Validation: 404 unknown session / unknown provider, **409 `provider '<name>'
  cannot generate video`** (only OpenRouter and xAI declare the video
  interaction), **400 `prompt is empty`**.
* States: the same `queued` -> `running` -> `done` | `error` as re-processing,
  in the `video_jobs` table, with `remote_id` (the upstream handle) and
  `video_path` (set once the bytes are on disk).
* The run (`src/loreline/video/jobs.py:191-233`):
  1. Build the vendor-specific submit body. The vendors genuinely differ:
     OpenRouter posts to `/videos`, answers with `id`, calls success `completed`
     and serves the bytes from `/videos/<id>/content`; xAI posts to
     `/videos/generations`, answers with `request_id`, calls success `done`,
     hands back a URL on a storage host, defaults `generate_audio` to **true**
     so a GM who turned audio off is represented by an explicit false, and takes
     no `seed`.
  2. Submit, persist `remote_id` before the first poll.
  3. Poll with backoff: **sleep first**, so the earliest possible answer is 5 s
     in; interval grows by 1.5x to a 30 s ceiling; hard deadline **3600 s**,
     after which the job fails with `generation still <status> after 60
     minutes`.
  4. A terminal failure status raises with the provider's own message. For
     OpenRouter, `expired` and `cancelled` count as failures.
  5. Download the bytes (10 minute timeout) and write them to
     `<data_dir>/video/<job_id>.mp4`. Downloaded rather than linked because both
     vendors' result URLs expire. An empty body is a failure, not a zero-byte
     file.
* Cancellation at shutdown re-raises `CancelledError` on purpose, leaving the row
  `running` so the next boot's reconcile sweep marks it interrupted with
  everything else.
* Results: `GET /api/video/{job_id}/content` serves the file
  (`video/mp4`, filename `<session_id>-<job_id>.mp4`), **409 `video is not
  ready`** while the job is not done, **404 `video file is missing`** when the
  row says done but the file is gone.
* What the UI shows: under the Summary section's buttons, one line per job,
  `"<model> . <duration>s . <resolution>"`, with `Generating...` while queued or
  running, the red error text when failed, and an inline `<video controls>`
  player when done. No percentage, no ETA, no download button (the browser's own
  video menu is the only route). Polled every **5000 ms** while a job is in
  flight. Delete (with a confirm) removes the row and the file.

---

## 5. Exports

`GET /api/session/{id}/export?fmt=<fmt>`. Unknown format -> **404 `unknown
format '<fmt>'`**. Unknown session -> 404.

Content is always `relabel_speakers(final_rows(canonical_transcript(rows)))`:
the **original** version, interims and gap markers removed, speaker renames
applied. There is no `version` parameter.

Every response carries `Content-Disposition: attachment; filename="<session
id>.<ext>"`.

| `fmt` | Media type | Filename | Shape |
| --- | --- | --- | --- |
| `txt` | `text/plain; charset=utf-8` | `<id>.txt` | one line per segment, `[mm:ss] <Speaker>: <text>`, newline separated, trailing newline. A segment with no speaker renders the literal `Unknown`. Empty session: a **zero-byte** file. |
| `md` | `text/markdown; charset=utf-8` | `<id>.md` | `# Session <id>` header, then `*Campaign:* <campaign_id>` when set, then one blank-line-separated `**<Speaker>** (mm:ss): <text>` per segment, trailing newline. Empty session: just the header and a newline. |
| `srt` | `application/x-subrip; charset=utf-8` | `<id>.srt` | numbered blocks from 1, `HH:MM:SS,mmm --> HH:MM:SS,mmm`, then the text. The speaker is prefixed as `<Speaker>: ` **only when the segment actually has one** (unlike txt/md, no `Unknown` filler). Empty session: an **empty** file. |
| `vtt` | `text/vtt; charset=utf-8` | `<id>.vtt` | `WEBVTT` header, then unnumbered cue blocks with `HH:MM:SS.mmm --> HH:MM:SS.mmm`. Same conditional speaker prefix as SRT. Empty session: just `WEBVTT` and a newline. |
| `json` | `application/json` | `<id>.json` | `{"session": <the whole Session row>, "transcript": [<every TranscriptEvent, full field set>]}`, `indent=2`. Empty session: the session object and an empty array. |

Timestamps in txt/md are `mm:ss` and are not clamped to an hour, so a four hour
session renders `240:15`. SRT/VTT use full `HH:MM:SS`. All are session-relative
because the rows were rebased at write time.

Alongside the five text exports, the session header's Export menu also offers
**Audio (.wav)**, which is `GET /api/session/{id}/audio` and not an exporter at
all: a plain `FileResponse` of the stored WAV, `audio/wav`, filename
`<session_id>.wav`, **404 `no audio for session`** when the store has no WAV or
no index sidecar. It is greyed out and reads `Audio (empty)` when the reported
duration is at or below 0.05 s.

---

## 6. Alerts and monitoring

### 6.1 Channels

Alert configuration is a list of channels persisted as JSON under the
`kv_settings` key `alerts`. Each channel carries its own `enabled` flag and its
own `min_level` gate. Three types (`src/loreline/monitoring/alerts.py`):

| Type | Fields | Transport | Secret |
| --- | --- | --- | --- |
| `ntfy` | `server` (default `https://ntfy.sh`), `topic` | `POST <server>/<topic>` with the message as the body and `Title` / `Priority` headers (`default` / `high` / `urgent` by level) | optional bearer token |
| `telegram` | `chat_id` | `POST https://api.telegram.org/bot<token>/sendMessage` with `"<title>\n\n<message>"` | **required** bot token; without one the send is refused and logged |
| `webhook` | `url` | `POST <url>` with `{"title", "message", "level"}` | none |

Tokens live in the secret store under `alert:<channel_id>:token` and are never
returned by the API. The channel list endpoint reports `token_set: true|false`
instead.

Delivery is best effort throughout: a transport error or any status at or above
400 is logged and reported as `false`, never raised. A capture session cannot die
because ntfy is down.

CRUD: `GET|POST /api/system/alerts/channels`,
`PUT|DELETE /api/system/alerts/channels/{id}` (404 `alert channel not found`),
and the test route below.

### 6.2 What triggers an alert

Every alert this app sends, with its level:

| Title | Level | Fired by |
| --- | --- | --- |
| `Transcription degraded` | WARNING | a streaming provider retired after exhausting its reconnects; a handoff to the utterance path; or three consecutive utterances that no provider transcribed (fires **once** on the transition, not per failure) |
| `Transcription stopped` | ERROR | every provider is dead for good. Audio keeps recording |
| `Disk space low` | WARNING | free space crossed the configured floor during a capture. Once per crossing, re-armed at 1.1x the floor |
| `Recording stopped: disk full` | ERROR | `ENOSPC` while writing the recording. Says how much audio is safe |
| `Session error` | ERROR | a session finalized with status `error` |
| `Session audio recovered` | INFO | the startup sweep rebuilt an orphaned index sidecar |

`/healthz` is the other monitoring surface and is deliberately unauthenticated.
Its rollup is `error` when capture status is `error`, `degraded` when free space
is under the floor, `ok` otherwise. The fields a tester should watch during a
capture are `capture_status`, `active_session_id`, `captured_seconds`,
`capture_last_frame_age`, `stt_degraded_since` and `stt_error`.

### 6.3 What the test button should do

`POST /api/system/alerts/channels/{id}/test` -> `{"ok": true|false}`.

It sends the fixed message `Loreline test alert` /
`Test notification from Loreline.` at level **INFO**, and it **ignores both the
channel's `enabled` flag and its `min_level` gate**
(`src/loreline/monitoring/alerts.py:172-186`). So a passing test proves the
credentials and the URL work; it does **not** prove that real alerts will arrive,
because a channel left at the default `min_level: warning` would filter an INFO
alert out, and a disabled channel receives nothing in normal operation.

`ok: false` is returned for four different situations that the UI cannot tell
apart: no such channel id, an incomplete channel (ntfy with no topic, telegram
with no chat id, webhook with no url), telegram with no stored token, and a
delivery that was attempted and refused.

---

## 7. Audio storage

### 7.1 Where it lives

`<data_dir>/audio/<session_id>.wav` plus `<session_id>.index.json`
(`src/loreline/persistence/audio_store.py`). `data_dir` defaults to `./data`.
Generated videos sit next door under `<data_dir>/video/`, and per-version logs
under `<data_dir>/logs/<session_id>/`.

The WAV is mono 16-bit PCM at the **primary provider's** `sample_rate` (16000 by
default), and it holds the **complete continuous capture including silence**, so
the recording can be re-VAD'd and re-diarized from true source later.

The sidecar records each voiced utterance as
`{start, end, offset_frames, n_frames}`, an exact byte offset into the WAV
rather than a timestamp correlation, so `read_utterances` reconstructs the exact
utterances for re-STT without re-running VAD.

Durability: `wave` patches the WAV header on every write and the sidecar is
rewritten atomically (temp file plus rename) after every marked utterance, WAV
flushed first so the index never references audio that has not reached disk. An
unclean death therefore loses at most the final in-flight utterance.

`AudioStore.exists()` requires **both** files. A session whose process died
before the first utterance completed has a WAV and no sidecar, which is what the
startup recovery sweep is for.

### 7.2 Retention and cleanup

There is **no time-based or size-based retention**. Recordings are removed only
when their session is:

* `POST /api/session/delete` deletes the transcript rows, the WAV and sidecar,
  the whole log directory and the session row. The **currently running** session
  is silently skipped.
* Deleting a transcript version removes its segments, its diarize copy, its job
  rows and its log file, but never touches audio.
* `POST /api/session/merge` **copies** audio into a new session and leaves the
  originals intact, so a merge roughly doubles disk usage. Merging is refused
  for the running session and needs at least two sessions.
* Deleting a video job removes its mp4.
* The only automatic sweep is the log-directory prune at startup.

The live guard against filling the disk is the in-capture `_DiskWatch` plus the
`/healthz` `degraded` badge, both against `LORELINE_DISK_ALERT_THRESHOLD_MB`
(default 500 MB, 0 disables). At 16 kHz mono a session writes roughly 115 MB per
hour.

### 7.3 What the session player streams

`GET /api/session/{id}/audio` returns the stored WAV as a `FileResponse`
(`audio/wav`, filename `<session_id>.wav`), **404** when the store has no
complete pair. The player element loads it with `preload="metadata"` and hides
the native controls behind its own play/pause button, an elapsed/total clock, a
seek bar, and one dot per transcript segment placed at that segment's
`start_ts` percentage (gap markers excluded, taken from the **currently shown
version**).

The player renders nothing at all when the session has no `audio_path`, and only
the note about an empty recording when the duration is at or below 0.05 s.

Both directions of transcript linkage exist: clicking a segment's timestamp
seeks and plays, and playback highlights the segment whose `start_ts` was most
recently passed, scrolling it into view with `block: 'nearest'` so a hand-parked
transcript is not yanked around.

`SessionDetail.audio_duration_s` is computed on every read from the WAV header
rather than stored, because the WAV keeps growing throughout a live capture. It
is also the only reliable way to tell a real recording from an errored session's
bare-header WAV.

---

## UI TEST STEPS

Browser level, against a live deployment. Assumes at least one enabled
transcription provider with a working key, and a second one for the failover
steps. `[neg]` negative or error path, `[obs]` observation only,
`[api-only]` not reachable from the browser.

### A. Capture: the happy path

1. Open `/`. Confirm the capture card shows the idle form: Transcription
   provider, Model, **Start session**, the dashed Fallback / Diarization /
   Glossary summary, and an **Edit** toggle. Confirm the header shows no
   `Capturing...` badge.
2. Pick a provider, then a model. Confirm the hint `Pick a model to start - it
   is chosen per session, not stored on the provider.` disappears and **Start
   session** enables.
3. Press **Start session**. Confirm within one second the card flips to a green
   dot, bold `Recording`, an `M:SS` clock counting from about 0:00, `. N
   providers`, `. waiting for audio`, and a level meter.
4. `[obs]` Speak into the mic. Confirm the level meter moves and the audio
   summary changes from `waiting for audio` to `M:SS recorded` and climbs.
5. `[obs]` Confirm the header gains the `Capturing...` badge with a green dot.
6. `[obs]` Hover the health dot and confirm the popover's `Capture` row reads
   `capturing`, `Transcript stream` reads `connected`, `Log stream` reads
   `connected`.
7. `[obs]` With a **streaming** provider (Deepgram, AssemblyAI, OpenAI Realtime,
   Gemini Live, xAI): confirm a dimmed italic transcript line appears while you
   are still speaking, grows in place, and then settles to normal weight when
   you pause. Confirm it does **not** spawn one row per revision.
8. `[obs]` With a **batch** provider: confirm no dimmed row ever appears and one
   settled row arrives per sentence, after the sentence.
9. `[obs]` Confirm the live logs pane fills with lines and that the count next to
   the `Logs` heading climbs.
10. Press **Stop session**. Confirm the card goes amber with `Finalizing`, the
    button reads `Finalizing...`, and the note `Transcribing what is still
    queued and closing the recording. This can take up to half a minute; the
    audio is already on disk.` appears.
11. Confirm the card returns to the idle start form and the header badge
    disappears.
12. Go to **History**, open the new session. Confirm Status reads `completed`,
    the header shows a start time and a duration, and the versions table has one
    `original` row with `complete` status and a segment count matching what you
    saw live.

### B. Capture: negative and edge cases

13. `[neg]` Press **Stop session** when nothing is running. Reach it by opening
    two tabs, stopping from tab A, then pressing Stop in tab B before its 5 s
    health poll. Expect a red line reading `no active session` under the card.
14. `[neg]` Start a session, then in a second tab press **Start session**.
    Expect the red line `a session is already running` in the second tab, and
    confirm the first tab's recording is unaffected. `[obs]` Note the second
    tab's card keeps showing the idle form for up to 5 s before flipping to
    `Recording`.
15. `[neg]` **Mic missing.** In Settings > Client set the input device to one
    that does not exist, or unplug the USB mic, then press **Start session**.
    Expect the start to be **refused** with the red line `<device> could not be
    opened for recording (<err>). Pick another microphone in Settings, or check
    that nothing else is using it.` Confirm **no** session row appears in
    History and no WAV is created.
16. `[neg]` **Mic disappears mid-capture.** Start a session, then unplug the
    mic. Expect, within a few seconds, the red banner `No audio has reached the
    recorder for N seconds...`, the dot turning red and the level meter reading
    zero. `[obs]` Confirm the session does **not** stop itself and no alert is
    sent; you have to press Stop. Then confirm the stored session is `completed`
    with the audio captured up to the unplug.
17. `[neg]` **Provider with no live capture.** Select an OpenRouter provider in
    the capture picker. It should not be offered at all. If it is reachable by
    any means, expect `... cannot drive a live capture - it is available for
    post-session re-processing only`.
18. `[neg]` **Disabled provider.** Disable a provider in Settings while it is
    selected in the capture card, then press Start. Expect `primary provider
    '<id>' is disabled`.
19. `[neg]` **Inline diarization on a model with no speakers.** Pick such a
    model; Inline should vanish from the Diarization dropdown. If forced,
    expect `model '<m>' on '<name>' returns no speaker labels - inline
    diarization would produce an unlabelled transcript`.
20. `[neg]` **Remote diarization with no endpoint.** Set Diarization to Remote
    and clear the endpoint. Expect Start to be blocked client side with an
    inline error; if forced, expect a 400.
21. `[obs]` **Silent session.** Start a session, say nothing for a minute, stop
    it. Expect status `completed`, zero transcript segments, and a WAV whose
    reported duration is the full minute. Confirm the Export menu offers
    `Audio (.wav)` enabled (there is real audio), and that the transcript panel
    is empty rather than broken.

### C. Capture: failover and degradation

22. `[neg]` **Streaming connection dies.** Start with a streaming primary, then
    block that vendor's host at the firewall for longer than the reconnect
    budget (three attempts). Expect: an amber italic gap line in the transcript
    reading `<N>s of audio was not transcribed: the connection to <provider>
    dropped.`, a `Transcription degraded` push alert, and, if a fallback is
    configured, text resuming through it.
23. `[obs]` **Handoff to the utterance path.** Configure a streaming primary
    with a **batch** fallback and kill the primary. Expect the alert `Live
    transcription is running through <name> one utterance at a time: text
    arrives after each sentence rather than while it is spoken.`, and confirm
    the visible tell: dimmed interim rows stop appearing and rows start arriving
    per sentence instead.
24. `[neg]` **Every provider dead.** Configure one provider, revoke its key mid
    session (or block its host permanently). Expect a `Transcription stopped`
    ERROR alert, the red dashboard line `Live transcription stopped: <vendor
    message> Audio is still being recorded, so the session can be re-transcribed
    once this is fixed.`, and confirm the clock and the recorded duration keep
    climbing. Stop it and confirm the stored status is **`completed`**, not
    `error`, and that the audio is intact and re-transcribable.
25. `[obs]` **Degraded, not dead.** Cause three consecutive utterances to fail
    on a batch provider (a brief network drop). Expect the amber line `Live
    transcription has been failing since HH:MM - audio is still being recorded
    and the session can be re-transcribed later.` and exactly **one**
    `Transcription degraded` alert, not one per utterance.
26. `[neg]` **Disk low, then full.** Fill the audio filesystem to just under the
    configured floor and start a session. Expect a `Disk space low` alert within
    30 s naming the free megabytes and an estimate of recording time left, and
    exactly one alert even after minutes on the boundary. Then fill it
    completely. Expect the session to end **on its own**, a `Recording stopped:
    disk full` alert saying how much audio is safe, and a stored session at
    `completed` whose WAV plays back to the point the disk filled.
27. `[neg]` **Kill the process mid-capture** (`systemctl restart loreline` or
    `docker compose restart app`). On restart, confirm the session row shows
    `error` with an end time, that no half-typed interim row survives in the
    transcript, and that the WAV is playable. `[obs]` Confirm whether
    re-transcription covers the whole recording or stops at the last completed
    utterance.

### D. Transcript versions

28. Open a finished session. Confirm the **Transcriptions** section is open by
    default and has one row, `original`, Status `complete`, with a **Show logs**
    action and **no** Delete.
29. Click **Show logs** on `original`. Confirm the dialog title reads
    `Logs . original` and the pane shows the capture's log lines.
30. `[neg]` Open a session recorded before per-version logs existed, or any
    version with no log file. Expect the muted line `No logs were stored for
    this version.`
31. Run a re-transcribe (steps below). Confirm a second row appears with the
    first 8 characters of the job id, the provider, the model, the segment
    count and the status badge.
32. Click the new row. Confirm the Transcript panel's fold meta changes to
    `<8 chars> . <n> segments` and the transcript body swaps.
33. `[obs]` Confirm the footnote appears under the table when the original was
    streamed: `The original was transcribed live in vendor turns;
    re-transcriptions are cut at the recording's utterance boundaries, so
    segments will not line up one to one between versions.`
34. Delete the re-transcription. Confirm the dialog reads `Delete version <8
    chars> and its N segments? Its diarization goes with it; the audio and the
    other versions are untouched.` with a red **Delete** button. Accept it and
    confirm the row goes, the view falls back to `original`, and **Show logs**
    for that version now reports no stored logs.
35. `[neg]` Try to delete `original`. There is no button; `[api-only]`
    `DELETE /api/session/{id}/transcript?version=original` should answer 409
    `the original transcript cannot be deleted`.

### E. Re-processing

36. On a finished session, in the **Transcriptions** section, pick a provider
    and model in the `New transcription` row and press **Re-process audio**.
    Confirm the button reads `Queuing...`, then a new row appears with status
    `queued` and Segments `0 so far...`.
37. `[obs]` Confirm within 1.5 s the status becomes `running` and the segment
    count climbs. Click the running row and confirm the transcript fills in
    live, segment by segment, over the WebSocket.
38. `[obs]` Confirm the count stops climbing and the badge turns `done`, with
    the final count matching the number of rows in the panel.
39. `[neg]` **No stored audio.** Open a session that has none (an errored start,
    or one whose WAV was deleted from disk). Confirm the re-process form is
    replaced by `No stored audio for this session - re-processing and
    diarization are unavailable.` `[api-only]` `POST /api/reprocess` for it
    should answer 409 `session '<id>' has no stored audio`.
40. `[neg]` **Failing job.** Re-process with a provider whose key is wrong.
    Confirm the badge turns red `error`, hovering it shows the message, the red
    line `Last failed job (transcribe): <error>` appears under the table, and
    **Show logs** on that version shows the traceback.
41. `[neg]` **Cancel mid-job.** There is **no cancel control anywhere**, and no
    endpoint behind one. Confirm this: start a long re-transcription and look for
    any abort affordance in the versions table, the panel and the row's context.
    The only escapes are waiting, or restarting the service. Do the restart and
    confirm the row becomes `error` with `interrupted by restart`.
42. **Concurrent jobs.** Start two re-transcriptions on the same session with
    different models, back to back. Confirm both are accepted, both run at once,
    and two distinct versions appear. `[obs]` Note there is no queue and no
    limit.
43. `[neg]` **Delete a version while a job writes it.** Start a re-transcription
    and press its **Delete** immediately. Confirm the button is disabled with
    the tooltip `Wait for the job to finish`. `[api-only]` `DELETE
    .../transcript?version=` should answer 409 `transcript version '<v>' is
    still being written`.
44. **Re-diarize.** In the **Transcript** panel's toolbar (not the Transcriptions
    section), pick a diarizer, optionally set min/max speakers, and press
    **Diarize**. Confirm the selected version's Diarization cell shows
    `diarizing...` at the next poll, and that when it completes the transcript's
    speaker labels change in place with **no new version row**.
45. `[obs]` Confirm the segment count sits at 0 for the whole diarization call
    and then climbs quickly. On a long session this is minutes of apparent
    inactivity and is expected.
46. Re-run the same diarize job with different min/max speakers. Confirm the
    previous relabeling is **replaced**, not stacked.
47. `[neg]` Diarize with a bogus endpoint. Confirm the failure appears **only**
    as the red `Last failed job (diarize): <error>` line, since diarize jobs get
    no row of their own, and that the stored message reads `The diarization
    service answered but could not process the audio (is it configured
    correctly?)` for an HTTP error and the raw reason for an unreachable host.
48. `[neg]` `[api-only]` **Re-process the currently capturing session.** Start a
    capture, wait for a first utterance, and `POST /api/reprocess` with
    `operation: "diarize"`, `target: "original"` for that session id. This is
    accepted. See SUSPECTED ISSUES.

### F. Speakers, summaries, video

49. On a diarized session, press **Rename speakers**. Confirm one input per
    detected label, prefilled with any stored name. Set names and **Save names**.
    Confirm the transcript rows update and the search matches the new names.
50. Click a speaker name inline in the transcript, type a new name, press Enter.
    Confirm it commits and Escape on a second attempt cancels.
51. `[neg]` Set names on `original`, switch to a re-transcription with different
    labels, open the dialog and **Save names** without touching anything.
    Confirm whether the original's names survive. See SUSPECTED ISSUES.
52. `[neg]` Open **Rename speakers** on an undiarized version. Confirm the
    button is disabled.
53. Press **Summarize**. If the transcript has no speakers, confirm the prompt
    `This session has no diarized speakers. Summarize anyway?` first. Pick a
    provider and model and submit. Confirm the button reads `Summarizing...`,
    the dialog stays open until the summary is on screen, and the Summary
    section then shows the text with `<provider> . <model>` in its fold meta.
54. `[neg]` Summarize a session with no transcript. Expect the dialog to stay
    open with `session has no transcript`.
55. `[neg]` Summarize with a bad key. Expect the dialog to stay open with the
    LLM's own message (a 502 detail).
56. `[obs]` Select a re-transcription version, then Summarize. Confirm which
    version's text was actually summarized. See SUSPECTED ISSUES.
57. Press **Generate video**. Confirm the model list loads on open, the prompt
    is seeded from the summary, and that only the knobs the chosen model
    supports are rendered. Submit and confirm the dialog closes and a job line
    appears reading `Generating...`.
58. `[obs]` Confirm the job line polls every 5 s, and that on completion it is
    replaced by an inline video player fed from `/api/video/{id}/content`.
    Confirm there is no percentage and no ETA at any point.
59. `[neg]` Generate with an empty prompt. Expect the **Generate** button
    disabled; if forced, `prompt is empty`.
60. `[neg]` Generate with a non-video provider. Expect the button disabled with
    the tooltip `Add an OpenRouter provider in Settings`; if forced, 409
    `provider '<name>' cannot generate video`.
61. `[neg]` Generate with a bad key or an unknown model. Expect the job to reach
    `error` and the red vendor message on the job line.
62. `[neg]` Delete a video job **while it is still generating**. Confirm the
    line disappears immediately. See SUSPECTED ISSUES.
63. `[obs]` Re-summarize the session, then reopen the video dialog. Confirm
    whether the prompt is re-seeded from the new summary.

### G. Exports

64. On a session with content, open **Export** and download each of `Text
    (.txt)`, `Markdown (.md)`, `Subtitles (.srt)`, `Subtitles (.vtt)`,
    `JSON (.json)`. Confirm each file is named `<session id>.<ext>` and matches
    the shapes in section 5.
65. `[obs]` Confirm the txt and md exports show `Unknown` for unlabelled
    segments while the srt and vtt exports omit the speaker prefix entirely.
66. `[obs]` Rename a speaker, re-export txt, and confirm the display name is in
    the file.
67. `[obs]` On a session with a gap marker, confirm the marker is **not** in any
    export, and that the interim rows visible mid-capture are not either.
68. `[neg]` **Export an empty session.** Take the silent session from step 21
    and export each format. Expect: `.txt` a **zero-byte** file, `.srt` a
    zero-byte file, `.vtt` a file containing only `WEBVTT`, `.md` a file with
    only the `# Session <id>` header, `.json` a valid object with an empty
    `transcript` array. Confirm the browser still downloads rather than erroring.
69. `[neg]` `[api-only]` `GET /api/session/{id}/export?fmt=pdf`. Expect 404
    `unknown format 'pdf'`.
70. `[obs]` Select a re-transcription version and export. Confirm which version
    came out. See SUSPECTED ISSUES.
71. Download **Audio (.wav)** and confirm it plays and matches the reported
    duration. `[neg]` On a session with no audio confirm the entry is absent;
    on one with an empty recording confirm it is greyed and reads `Audio
    (empty)`.

### H. Alerts

72. In Settings > Alerts, add an **ntfy** channel with a topic. Press its test
    button and confirm `Loreline test alert` arrives on the device.
73. Add a **Telegram** channel with a chat id but **no** token. `[neg]` Press
    test and confirm it reports failure.
74. Add a **webhook** channel pointing at a request bin. Press test and confirm
    the received body is `{"title", "message", "level"}` with `level` = `info`.
75. `[neg]` Set a channel's `min_level` to `error`, press test, and confirm the
    test **still delivers**. This is the gate bypass: a passing test does not
    prove that a WARNING alert would arrive. Then trigger a real
    `Transcription degraded` (step 25) and confirm it does **not**.
76. `[neg]` Disable a channel and press test. Confirm it still delivers.
77. `[neg]` Point a webhook at a URL that returns 500 and confirm the test
    reports failure.
78. Delete a channel and confirm its stored token goes with it (re-add the same
    channel and confirm `token_set` is false).
79. `[obs]` Trigger each real alert from the table in section 6.2 and confirm
    the title, the level and the wording match.

### I. Storage and the player

80. On a finished session, confirm the player renders with a play button, an
    elapsed/total clock, a seek bar and one dot per transcript segment.
81. Click a transcript timestamp. Confirm playback jumps there and starts.
82. `[obs]` Let it play and confirm the current row is highlighted with a left
    bar and scrolled into view, and that a hand-scrolled paused transcript is
    not yanked around.
83. Switch to a re-transcription version and confirm the dots on the seek bar
    change to that version's segments.
84. `[neg]` On a session with no audio, confirm the player renders nothing at
    all and that transcript timestamps are plain text rather than buttons.
85. `[neg]` On a session whose recording is an empty header, confirm the player
    shows only `This session has no captured audio - the recording is empty.`
86. **Delete a session** from History. Confirm the dialog says `Delete N
    session(s)? This also removes their audio.`, and afterwards confirm on the
    box that `<data_dir>/audio/<id>.wav`, the sidecar, and
    `<data_dir>/logs/<id>/` are gone.
87. `[neg]` Select the **currently capturing** session and delete it. Confirm
    the call reports success and the row is still there after the reload. See
    SUSPECTED ISSUES.
88. **Merge** two finished sessions. Confirm the dialog wording, that you land
    on the merged session, that its audio is the two recordings back to back,
    that the transcript timestamps run continuously, and that both originals are
    still in History with their audio intact.
89. `[neg]` Try to merge with the running session selected. Expect `cannot merge
    a running session`.
90. `[neg]` Try to merge one session. Expect the button disabled; if forced,
    `merge needs at least 2 sessions`.
91. `[obs]` Confirm there is no retention setting anywhere in Settings and that
    nothing ever deletes old audio automatically.

### J. Cross-cutting

92. `[neg]` Kill the backend while the dashboard shows `Recording`. Confirm what
    the page does. See SUSPECTED ISSUES.
93. `[obs]` Open a session's detail page **while that session is capturing**.
    Confirm whether the transcript follows the live capture or is frozen at the
    moment the page loaded. See SUSPECTED ISSUES.
94. `[obs]` Stay on the dashboard, stop a session and start a new one without
    reloading. Confirm whether the old session's lines are still in the
    transcript pane.
95. `[obs]` Start a capture, then press Stop and immediately press Start again
    in the same tab. See SUSPECTED ISSUES.
96. `[obs]` Run the self-update (Settings > Client) while a session is
    recording. Confirm the recording ends, the page loses its connection, and
    after the restart the session shows as `error` with playable audio.

---

## SUSPECTED ISSUES

Ordered roughly by how much damage each can do. Line numbers are against this
checkout.

1. **A new session can start while the previous one is still finalizing.**
   `SessionManager.stop` clears `_runtime` inside the lock and then runs the
   whole teardown outside it
   (`src/loreline/session/manager.py:750-759`), so `status()` answers `idle` and
   `start()` sees a free slot while the old capture's frame source is still
   draining, its WAV header has not been patched, its persist task is still
   writing and its backends are still open. Pressing Stop then Start quickly can
   pre-flight the microphone against a device the previous capture has not
   released yet, and in the worst case run two capture loops at once. `_finish`
   is bounded at 30 s, so the window is not small.

2. **The dashboard goes deaf for the whole teardown, so a session's last words
   never arrive.** Both live feeds filter on `manager.current_session_id()`,
   which `stop()` has already set to None: the transcript socket drops anything
   whose session is not the active one
   (`src/loreline/web/routes/transcript_ws.py:30-32`) and the log socket drops
   anything whose session id does not match
   (`src/loreline/logbus.py:40-44`). Everything `_finish` produces is emitted
   after that point: the settle finals for open turns, the trailing gap marker,
   `session.stop.drain_timeout`, `session.stop`. Symptom on screen: press Stop
   mid-sentence and the dimmed interim row stays dimmed forever, and the log
   pane shows nothing at all for the 30 s the user is being asked to wait.

3. **Re-processing has no guard against the session that is currently
   capturing.** `enqueue` checks only that the session exists and that a WAV
   plus sidecar are on disk (`src/loreline/reprocess/jobs.py:231-241`), both of
   which are true from the first completed utterance of a live capture. Session
   delete and session merge both refuse the running session
   (`src/loreline/web/routes/sessions.py:293-295`, `:320-321`); this does not. A
   diarize job against `original` then snapshots the half-recorded transcript
   into a `diarize:original` copy which **permanently supersedes** the real rows
   on every read (`src/loreline/export.py:38-56`), so the rest of the evening
   vanishes from the session page, the exports and the summaries. A transcribe
   job reads a WAV that is still being appended to.

4. **Deleting a running video job orphans its file.**
   `VideoManager.delete` removes the file and the row
   (`src/loreline/video/jobs.py:174-177`) but never cancels the task. The task
   keeps polling, and when it finishes it writes the mp4
   (`:229`) and calls `_videos.update`, which updates zero rows. Result: an
   `.mp4` under `<data_dir>/video/` that no row references and nothing will ever
   clean up.

5. **`SileroVad` is built on the event loop inside `start()`.**
   `_default_capture` constructs it (`src/loreline/session/manager.py:147-155`),
   which imports torch and numpy and calls `load_silero_vad(onnx=True)`
   synchronously, and `start()` calls that factory directly at `:574`. Every
   Start therefore blocks the whole server, including every other request and
   every WebSocket, for the model load. A deployment without the `audio` extra
   raises `ImportError` out of the factory, which no handler in the start route
   catches, so a missing dependency surfaces as an opaque **500** rather than as
   the friendly device message the pre-flight was written to give.

6. **`SessionStatus.STOPPING` is declared and never used.**
   `src/loreline/models.py:57`; `manager.status()` can only return `idle` or
   `capturing` (`src/loreline/session/manager.py:433-434`). The consequence is
   that `Finalizing` is per-tab state only: a second tab, or a refresh during
   the stop, shows the idle start form as though nothing were happening, and
   `/healthz` reports `idle` while the recording is still being closed.

7. **The session detail page never follows a live capture.**
   `detail.transcript` is fetched once on mount and the `original` view reads
   straight from it (`frontend/src/routes/sessions/[id]/+page.svelte:231-241`,
   `:123-125`), while the page's WebSocket only accepts frames whose source is
   `reprocess:<selected version>` (`:113-121`). So opening a capturing session's
   page shows a `live` badge next to a frozen transcript and a segment count
   that never moves, even though the server is pushing that session's events
   down the very socket the page has open.

8. **Exports and summaries always use the `original` version.**
   `export_session` and `summarize_session` both call `canonical_transcript`
   (`src/loreline/web/routes/sessions.py:262`, `:223`) and neither route accepts
   a version parameter. Select a re-transcription, export it or summarize it,
   and you silently get the original instead, with nothing on screen saying so.
   The summarize dialog even computes its "no diarized speakers" warning from
   the *shown* version, so the warning and the input can disagree.

9. **No job can be cancelled.** There is no cancel route for re-processing
   (`src/loreline/web/routes/reprocess.py` has only enqueue, get, list) and none
   for video; `ReprocessManager` cancels tasks only at process shutdown
   (`src/loreline/reprocess/jobs.py:337-345`). A wedged job pins the page's
   1.5 s poll indefinitely and the only escape is a service restart. A video
   generation additionally polls upstream for up to an hour before it gives up
   (`src/loreline/video/jobs.py:60`).

10. **Whole recordings are read into memory for re-processing.**
    `read_wav` does `path.read_bytes()` (`src/loreline/persistence/audio_store.py:301`)
    and `read_utterances` does `wav.readframes(wav.getnframes())` (`:311`), then
    slices copies out of it. A five hour session at 16 kHz mono is roughly
    576 MB per call, and the diarize job additionally holds the same bytes while
    posting them. Two concurrent jobs on a long session can plausibly OOM a Pi
    or a small VM, and nothing limits concurrency (issue 13).

11. **A session that ends itself can outlive the database.** `_end_unattended`
    is spawned as a task and stored on `_unattended_end`
    (`src/loreline/session/manager.py:780`) but nothing ever awaits it. At
    shutdown the lifespan calls `manager.stop()`, which returns None because the
    runtime was already claimed, and then closes the connection
    (`src/loreline/web/app.py:306-309`) while `_finish` may still be writing.
    The `sessions.finish` write then fails against a closed connection, leaving
    the row at `capturing` until the next boot's sweep turns it into `error`.

12. **Per-version log writes are blocking I/O in the logging hot path.** The
    structlog tap calls `LogStore.append` inline (`src/loreline/logging.py:94`),
    which opens, writes and closes the file per line, on the event loop, for
    every log line that carries a `session_id`. That is several syscalls per
    line during a capture and during every re-processing job. It is deliberate
    (the docstring explains why a cached handle would be worse) but it is still
    synchronous disk I/O on the loop, and a slow or full disk stalls every
    request and every WebSocket with it.

13. **Nothing bounds concurrent jobs.** `ReprocessManager.enqueue` never checks
    for a job already running on the same session
    (`src/loreline/reprocess/jobs.py:231-278`), and neither does
    `VideoManager.enqueue`. Any number of re-transcriptions of the same session
    can run at once, each holding its own copy of the recording (issue 10) and
    its own connector.

14. **A diarize job can target a version that is still being written.**
    `enqueue` validates the target only by asking whether it has any rows yet
    (`src/loreline/reprocess/jobs.py:246-250`). A transcribe job that has
    written its first segment therefore passes, and the diarize job relabels a
    half-finished transcript into a `diarize:<version>` copy that then
    supersedes the finished one. `delete_version` has exactly this busy check
    (`:311-319`); enqueue does not.

15. **A failed session insert leaves an orphan WAV.** The audio writer is
    constructed, and therefore the file is created, before the session row is
    inserted (`src/loreline/session/manager.py:599` then `:602`). A failed
    insert leaves a stray `.wav` with no sidecar and no session row, which
    `orphaned_wavs()` finds but `recover_orphaned_indexes` skips because there
    is no session to attach it to, so it is never cleaned up by anything.

16. **A passing alert test proves less than it looks like it does.**
    `test_channel` ignores both the channel's `enabled` flag and its `min_level`
    (`src/loreline/monitoring/alerts.py:172-186`) and sends at INFO. A channel
    disabled, or left at the default `min_level: warning`, passes its test and
    then delivers nothing in normal operation. `ok: false` also conflates four
    distinct causes (unknown channel, incomplete channel, missing token, refused
    delivery) with no way for the UI to tell them apart.

17. **Summaries are a synchronous request with no job row.** Unlike
    re-processing and video, `POST /api/session/{id}/summarize` holds the HTTP
    request for the whole call (`src/loreline/web/routes/sessions.py:206-249`),
    bounded only by the LLM client's 120 s timeout
    (`src/loreline/llm.py:27`). A long transcript on a slow reasoning model can
    hit that timeout, or a reverse proxy's, and there is no partial result and
    nothing to retry against.

18. **The two speaker-rename paths disagree.** The inline edit merges into the
    stored map (`frontend/src/lib/TranscriptPanel.svelte:129-143`) while the
    dialog replaces it with only the labels present in the currently shown
    version (`frontend/src/lib/RenameSpeakersDialog.svelte:56-61`). Naming
    speakers on `original`, switching to a diarized re-transcription with
    different labels and pressing Save silently drops every name for the
    original's labels. The Save button also has no busy state, so it can be
    double submitted, and a failure writes into the page banner **behind** the
    still-open modal.

19. **The elapsed clock keeps ticking after the backend dies.** The health poll
    swallows every non-401 error and leaves the previous payload in place
    (`frontend/src/routes/+layout.svelte:68-79`), so killing the backend
    mid-session leaves `Recording`, a counting clock, a `Capturing...` badge and
    a green health dot, with only the two WebSocket dots in the popover turning
    amber. Nothing says the app lost contact.

20. **The logs dialog can sit on `Loading...` forever.** The placeholder is shown
    whenever `lines.length === 0`
    (`frontend/src/lib/SessionLogsDialog.svelte:80-82`), and a version whose log
    file exists but is empty returns **200** with `logs: ""`. Only a 404 produces
    the real empty state. `LogStore.append` swallows its own write failures by
    design (`src/loreline/persistence/log_store.py:51-68`), so a full disk is a
    plausible way to get exactly that file.

21. **Deleting the running session reports success and does nothing.** The route
    skips the active id in a loop and still answers 200
    (`src/loreline/web/routes/sessions.py:293-295`). The list reloads and the
    row is still there, which reads as a broken delete rather than a refusal.

22. **Interims are deleted session-wide, not version-wide.**
    `delete_interims` drops every row with `is_final = 0` for the session
    regardless of `source` (`src/loreline/persistence/repositories.py:275-286`).
    Harmless today because only the streaming capture writes interims, but it is
    a session-scoped delete standing in for a version-scoped one, and it runs
    from both `_finish` and the startup sweep.

23. **Several polling callbacks have no error handling.**
    `refreshVideoJobs` and `deleteVideo`
    (`frontend/src/lib/SessionSummary.svelte:76-90`) and `refreshJobs`
    (`frontend/src/routes/sessions/[id]/+page.svelte:183-185`, `:223`) are called
    bare from intervals. A transient failure is an unhandled rejection: nothing
    reaches the banner, the list simply stops updating while the interval keeps
    hammering.

24. **The transcript pane never resets between sessions.** The live feed seeds
    once and nothing clears it when a session ends
    (`frontend/src/lib/liveFeed.svelte.ts:84-114`), so stopping one session and
    starting another without reloading leaves the previous session's lines above
    the new ones with no separator. The `turnKey` includes the session id, so
    rows do not collide, but the pane is misleading.

25. **`. N providers` on the recording row is the count of all configured
    providers**, not the ones this session is using
    (`frontend/src/lib/CaptureControls.svelte:469`). It reads as session
    metadata and says `. 0 providers` if the provider load has not finished.

26. **A failed Stop can permanently swallow the "session ended badly" banner.**
    `stoppedByUs` is armed before the request and cleared only by the effect
    that sees capture go away (`frontend/src/lib/CaptureControls.svelte:418`,
    `:352-370`). A Stop that errors while the session is still running leaves
    the flag set, and the next unexpected death of that or a later session is
    treated as already reported, so no banner appears at all.
