---
status: accepted
date: 2026-09-06
---

# A realtime connector may stream continuous audio instead of one utterance per call

## Context

CONTEXT.md describes realtime transport as "a socket that answers while audio
is still going out, batch is one request per utterance." No connector does
that yet. `OpenAIRealtimeBackend` is the only one that keeps its socket
between calls (`_ensure_ws` caches `self._ws`); `AssemblyAIBackend`,
`DeepgramBackend`, `GeminiLiveBackend` and `XaiBackend` each open a socket
inside `transcribe_one`, send one utterance, and close it. And all five,
OpenAI included, only ever see audio that has already been cut: `SessionManager
._capture_utterances` runs the mic through `SileroVad` + `VadChunker`, which
hands over an `Utterance` only after 800ms of trailing silence or a 30s cap
(`VadChunker`'s defaults, and `manager.py` constructs it with nothing else),
and `SttRouter.run` awaits `backend.transcribe(utterance, ...)` once per
utterance. So realtime transport today buys a faster answer after an
utterance closes, never a word before it does, and it never buys interim
text at all: `Connector.transcribe` (`stt/base.py`) stamps every event
`is_final=True`, even though `TranscriptEvent.is_final` defaults to False
for exactly that purpose and `TranscriptList.svelte` already dims non-final
rows (`dimInterim`, used by the Dashboard's live pane). The slot exists;
nothing fills it.

The four per-utterance connectors are not careless about this. Each records
why it reconnects: `DeepgramBackend` because `CloseStream` is "the only flush
signal Deepgram defines unconditionally" and a shared stream's `Finalize` ack
"can arrive seconds late, where it poisons the next utterance's reads";
`AssemblyAIBackend` because `Terminate` is the only flush and force-endpointing
a reused session "leaves no way to tell flush done from still transcribing";
`XaiBackend` for the same reason about `audio.done` versus `Finalize`;
`GeminiLiveBackend` because `audioStreamEnd` is the only "no more audio" signal
and "a fresh session per utterance keeps a late frame from poisoning the next
utterance's reads". Every one of those is a fact about flushing a
caller-bounded utterance through a shared stream. None of them is an argument
against a stream with no caller-bounded utterance in it, where there is no
flush per utterance and no next read window for a late frame to poison; one
reader consumes the vendor's own finals for the life of the session. Read
those comments as the cost of reusing a socket under the one-utterance
contract, which is precisely the contract this ADR leaves in place for batch
and steps outside of for streaming.

ADR 0005 settled a different question: it removed an `AsyncIterator[Utterance]`
wrapper around what was, in every real call, one item. Its contract,
`transcribe_one(utterance, prepared)`, is a call bounded by a caller-decided
utterance. This ADR is about a connector that is handed no boundary and
decides its own turns. Different shapes, not the same one twice.

The alternative of dropping VAD everywhere and slicing by a fixed size or
duration was considered and rejected. A fixed cut lands mid-word and mid-turn,
which costs transcription accuracy at every cut and diarization accuracy on
top, and it would replace the boundaries the session page draws its timeline
dots, click-to-jump and karaoke highlight from (`SessionPlayer.svelte` and
`TranscriptList.svelte`, both keyed on `TranscriptEvent.start_ts`). Avoiding
mid-word cuts means detecting silence, which is VAD by another name. VAD
stays.

Storage is untouched by any of this. `SessionAudioWriter.append_frame`
(`persistence/audio_store.py`) writes every captured frame, silence included,
to one continuous WAV per session regardless of how STT is fed; the utterance
index is a separate sidecar with exactly two readers, `reprocess/jobs.py`
(`read_utterances`) and `session/recovery.py` (`rebuild_index`). Nothing in
the browser reads it.

Diarization is context here, not the problem. `SttRouter._merge_diarization`
ships one utterance's isolated audio to the sherpa-onnx service per call and
gets back clusters with no memory of the previous call; inline mode reads that
utterance's `event.words`. Both are utterance-shaped, which already limits
speaker-label consistency across a session (a separate, known gap). Streaming
changes what supplies the boundary diarization keys off; it does not close
that gap.

## Decision

Two connector shapes, side by side. Batch and reprocess keep ADR 0005's
`transcribe_one(utterance, prepared) -> Transcription | None` unchanged. A
realtime connector may instead implement a streaming shape: fed raw PCM
frames with no caller-decided boundary, it decides its own turns by its
vendor's protocol and yields `TranscriptEvent`s as they finalize.

1. `SessionManager` gains a second live path beside `_capture_utterances` /
   `SttRouter.run`, taken only when the resolved model prefers realtime
   (`is_realtime_model`, `prefer: realtime` in `capabilities.yaml`) and its
   connector implements the streaming shape. Frames go to the connector
   straight from capture, through a streaming resampler where the vendor's
   rate differs from the capture rate (capture stays pinned to what
   `SileroVad` accepts; OpenAI wants 24kHz and today upsamples per utterance
   with `resample_pcm16`). `VadChunker` keeps running for the WAV's utterance
   index; it leaves the dispatch path. Events go to the same `EventBus` the
   router publishes to, so the socket relay, `_persist`, versions and the UI
   need no idea which path produced them, on one condition: the streaming
   path publishes finals only, until interim handling exists downstream.
   `_persist` is an `INSERT` per event (`repositories.py`, no upsert), and
   both `LiveFeed`s accept by `start_ts` plus text, so a growing interim
   published as-is would be stored and shown once per revision.

   *Amended on landing (2026-09-06).* The condition was met rather than
   waited on: the interim handling landed with the first connector, so
   interims ship from the start. `TranscriptEvent` gained a `turn_id`, a key
   every revision of one vendor turn carries; `transcript_segments` gained the
   column and a unique index on `(session_id, source, turn_id)`, so
   `TranscriptRepository.add` upserts by it; and both `LiveFeed`s take a `key`
   callback that replaces a held item instead of appending. A start timestamp
   was the cheaper key and was rejected: it is stable across a turn's interims
   only for a vendor reporting server-VAD offsets, and Deepgram and AssemblyAI
   both revise a turn's start as their endpointing refines, where a key that
   quietly stops matching writes a second row rather than failing. Rows the
   utterance path writes carry no `turn_id` at all, which SQLite counts as
   distinct from every other NULL, so that path still appends exactly as it
   did.

2. Migration is per connector, not a flag day, and a connector's shape is a
   fact about its class, not a new field in `capabilities.yaml`.
   `OpenAIRealtimeBackend` goes first because its connection lifecycle already
   matches, and only that: it runs with `turn_detection: None`, appends one
   utterance, commits, and reads until one `completed` event, whose payload is
   text with no timing. Streaming means server VAD on, a reader task that
   outlives any call, and timestamps taken from the server's speech
   start/stop events rather than from a local `Utterance`. That is a rewrite
   of the protocol handling around a socket that is already kept. The other
   four need the socket kept as well. Until a connector is migrated, its
   models keep today's path and today's floor.

3. Failover is redefined, not reused. `SttRouter._transcribe_with_failover`
   retries one bounded call against the fallback; a stream has no bounded
   call. A streaming session reconnects the same provider a bounded number of
   times, then opens a fresh stream against the fallback from the next frame
   on, resampled to the fallback's rate if it differs. What was in flight on
   the dead connection is gone. Today's live transcript can lose audio too,
   `_offer` drops the oldest queued utterance when STT lags and the router
   publishes nothing when both providers fail, but every such loss is a whole
   utterance that the index still names, so a reprocess recovers exactly it.
   A streaming loss is a span aligned to nothing, so it needs a visible marker
   in the transcript, not just a log line.

   *Amended on landing.* Three things this did not say, each decided against
   the code. **The marker** is a row whose `source` is `gap`
   (`models.GAP_SOURCE`), beside the existing `reprocess:` and `diarize:`
   tags. A source rather than a flag, because everything that already routes
   by source then routes it for free: it belongs to the live capture's
   version, so the session page and the dashboard show it (rendered as a rule,
   not as a line of speech), while `export.final_rows` keeps it out of
   exports, summaries and the rows a diarize job relabels, which read finals
   only for the same reason. **The budget** counts *consecutive* failures and
   is reset by a connection that stayed up longer than `healthy_after_s`:
   without that, an evening's ordinary reconnects would spend it and fail over
   for no reason, while a socket that dies every few seconds still never earns
   a reset. **A fallback that does not stream** was the case with nowhere to
   go: the session hands over to the utterance path for good, the chunker's
   utterances start reaching the queue they were being withheld from, and an
   `SttRouter` built with the fallback drains it exactly as it would have from
   the start.

   *Hardened after review (`f21a8f1`).* The marker did not yet survive a
   failover: a provider that ran out of reconnects published its gap
   immediately, closed at whatever it had last written, before the caller had
   even chosen the next provider. It now stays open across that handoff.
   `TranscriptStream.pending_gap` hands it to `StreamPath`, which seeds it
   into the next stream it opens, still naming the provider that lost it
   rather than the one now carrying it, and that stream's first written frame
   closes it, the real moment transcription resumed. A gap nothing reopens
   for, every streamable provider retired, a handoff to the utterance path, or
   a session ending on a dead socket, is closed instead at the last frame
   capture produced (`StreamPath._close_gap`).

4. Timeout is a liveness watchdog, not `asyncio.wait_for` around a call: audio
   sent, nothing back (no partial, no final, no acknowledgement) inside a
   configured window means the connection is dead and (3) applies.

   *Amended on landing.* "Audio sent" had to become "*voiced* audio sent",
   read off the local VAD the capture loop is running anyway. With server VAD
   on, a silent room produces no vendor messages at all by design, so a plain
   "nothing received" timer fires on every coffee break, reconnects, and marks
   a gap over silence. Silero keeps deciding the WAV's utterance index and now
   also answers "was there anything to transcribe", which is the one question
   that makes the watchdog's silence meaningful. A second liveness rule came
   with it: a write that blocks past `send_timeout_s`, and a frame queue that
   overflows, are both dead connections, because each means audio was dropped
   and the byte count the vendor's offsets are measured against no longer
   matches the capture clock. Reconnecting is what restores that mapping, with
   a fresh `t0`.

   And "voiced audio sent" was still not enough, which only the real vendor
   showed: see "What the real vendor said" below. The rule that survived
   measurement is that the *local VAD has to go quiet first*. Every vendor
   emits something at a turn boundary and none of them promise anything inside
   one, so silence past the end of a turn is a dead connection and silence
   during one is a long sentence.

   *Hardened after review (`f21a8f1`, wired into every connector at
   `fd82240`).* A connector's `signals()` gained a fifth thing to yield beside
   the four turn signals: `StreamAlive`, for a vendor message that proves the
   socket is alive but names no turn. All five connectors yield it now:
   Deepgram's `Metadata` and empty lead-ins, AssemblyAI's `Begin`,
   `SpeechStarted` and `Termination`, x.ai's `transcript.created`, OpenAI's
   `session.updated` and `input_audio_buffer.committed`, Gemini's padding and
   resumption frames. The watchdog counts messages *the stream* was told
   about, not messages the socket carried, so a connector that consumed one of
   these silently left it unable to tell a slow connection from a dead one.
   `StreamConfig.quiet_grace_s` now states its invariant outright rather than
   leaving it to this decision's prose: it must exceed the vendor's own
   endpointing silence, or an ordinary pause would read as a dead connection
   before the vendor had any chance to close the turn itself.

5. Diarization for a migrated connector keys off the vendor's turn boundary
   instead of an `Utterance`. Remote diarization has nothing to send until a
   turn closes, so it either buffers each vendor turn into a clip and ships
   that (unchanged past that point) or is withheld for migrated connectors
   until the buffering exists. Inline diarization reads `event.words` and
   needs no shape change, but it needs words: `GeminiLiveBackend` returns
   none and no timing of any kind, and OpenAI's `completed` event carries
   neither, so for those two a turn's `start_ts`/`end_ts` has to come from
   server VAD events or from bytes-sent bookkeeping, and inline diarization
   has nothing to work with. Decided in Phase 4, after Phase 3 shows what
   each vendor's turns actually carry.

   *Amended on landing.* Remote diarization took the buffering option and took
   it immediately, because withholding it would have made the first migrated
   connector worse than the path it replaced. `RollingPcm` holds the last 90
   seconds of capture (only in remote mode, where somebody wants it back), a
   closed turn's span is sliced out of it, and from there it is literally the
   same call: `SttRouter._merge_diarization` and the streaming path both go
   through one `merge_diarization`, which is also the only place a live
   capture ships audio to the diarizer. OpenAI's `speech_started` and
   `speech_stopped` do carry the offsets a `start_ts` needs
   (`audio_start_ms` / `audio_end_ms`), so the two-vendors-without-timing
   problem is Gemini Live's and, in a different form, Deepgram's and
   AssemblyAI's. For a vendor that states no offset at all the base falls back
   to the capture timestamp of the last frame written, which is late by that
   vendor's own latency and is the best answer available without one.

   *Hardened after review (`f21a8f1`).* That call no longer sits in front of
   publishing. A closed turn's clip is sliced from `RollingPcm` and the turn
   is published unlabelled in the same step; `merge_diarization` then runs in
   a background task bounded by 10s, and on success the same turn is
   published again with its speakers, under the same `turn_id`, which the
   repository upserts on and both live feeds key on, so the second
   publication replaces the first rather than following it. On failure or
   timeout the unlabelled row simply keeps standing, and the session's stop
   drain gives labels still in flight a further 5s before dropping them.
   Awaiting the diarizer in front of publishing, which is what this did, put a
   remote HTTP call on the connector's reader task: a slow answer held the
   reader past the stream's own watchdog and cost a healthy socket a gap
   marker, and an error answer raised through the reader and declared the
   vendor dead over a fault that had nothing to do with it.
   `SttRouter._merge_diarization` took the same fix on the utterance path: a
   diarizer error now publishes the utterance unlabelled rather than raising
   it out of the live path.

## Consequences

* Batch and reprocess are untouched: same contract, same connectors, same
  test fakes, same `stored_audio_backend()` forcing `prefer_batch=True`.
* The four per-utterance connectors have since been migrated (Phase 3, all
  four merged on this branch): the 800ms/30s floor now describes only the
  utterance path itself, batch, reprocess, the call-shaped fallback, and
  whichever models a migrated connector still cannot stream (OpenAI's two
  realtime-routed models; see "What the real vendor said" below).
* `stt/base.py`'s docstring and ADR 0005 stay correct for batch and for any
  unmigrated realtime connector; once one is migrated, both need a line
  saying a second shape exists. That docstring also still counts "eight real
  connectors" and "four batch connectors"; there are eleven, five realtime
  and six batch, and the same edit should fix the count. *Done on landing,
  along with `CONTEXT.md`, which gained the streaming shape, a turn, a turn
  id and the gap marker as terms.*
* CONTEXT.md's Transport definition becomes literally true for a migrated
  connector, and `openai_realtime.py`'s module docstring, which still says
  "we open a session per voiced utterance" above an `_ensure_ws` that does
  the opposite, gets corrected on the way. *Done on landing: CONTEXT.md's
  Transport entry says so directly, and the module docstring now describes
  both shapes and why OpenAI keeps them on separate sockets.*
* A reprocess version of a streamed session will not line up segment for
  segment with its original: reprocess reads VAD boundaries from the index,
  the original carries the vendor's turns. Nothing breaks, the index has no
  reader in the browser, but the two versions of one session will differ in
  shape, not only in text, and the version list should say so. *Done,
  fast-forwarded onto this branch at `d13fdc2`: `TranscriptVersions.svelte`
  shows a note under the version table whenever at least one re-transcription
  job exists alongside an original that carries a `turn_id`.*
* Interim events are the user-visible win of streaming and the thing most
  likely to corrupt the transcript if published early: finals only in Phase
  1, and replace-by-key semantics in `_persist` and both `LiveFeed`s before
  any connector publishes a partial. *Superseded on landing: the second half
  was built first, so the first half never applied. See the amendment under
  Decision (1) for the key, and note the rule it forced everywhere else, that
  only finals are a transcript. Exports, summaries and diarize jobs read
  through `export.final_rows`; the browser's views do not, because an interim
  on screen is the whole point. A session's ending settles every open turn
  rather than dropping it, and `delete_interims` sweeps whatever an ending
  that could settle nothing left behind, so a stored transcript never holds a
  half-typed row.*
* The timeline dots, click-to-jump and karaoke highlight key off
  `TranscriptEvent.start_ts` on the bus, which both paths populate, so no
  frontend change follows as long as a migrated connector reports a real
  start on the session clock. See Decision (5) for the two vendors where
  that is not free.
* The cross-utterance speaker-consistency gap (no speaker embeddings kept
  between diarizer calls) is adjacent to Phase 4 and stays its own piece of
  work. *Closed separately and in time to matter here: ADR 0007's
  session-scoped speaker bank landed alongside this, and the streaming path
  passes the same `session_id` to every call. It matters more per turn than
  it did per utterance, since a vendor's turns are shorter and there are more
  of them.*
* The phases below were written as an order to build in, and the first
  landing did not follow it: Phase 1 and Phase 2 arrived together with the
  parts of Phase 4 that Phase 1 turned out to need (replace-by-key, the
  remote-diarization buffering). They are left as written, because what they
  record is why each piece was thought to be separable, and two of them were
  not. Phase 3, the remaining four connectors, has since landed (see "What
  the real vendor said" below for each); of Phase 5, the docstring and
  CONTEXT.md updates landed with Phase 1, and only its closing ADR is still
  ahead.

## What the real vendor said

Phase 1 asked for verification against paced real audio before touching any
other connector (`scripts/stream_check.py`, a public-domain LibriVox clip fed
at wall clock in 20ms frames). Three things came back that no mock could have
said, on 2026-09-07 against real OpenAI:

* **The two models this repo routes to the realtime connector cannot be
  streamed.** `gpt-live-transcribe` and `gpt-realtime-whisper` both answer a
  server-VAD `session.update` with "Turn detection is not supported for this
  transcription model". `gpt-4o-transcribe`, `gpt-4o-mini-transcribe` and
  `whisper-1` accept it on the same socket, and `capabilities.yaml` sends all
  three to batch (`prefer: batch`). So the connector migrated first is, today,
  migrated for no model anybody can select. A session that picks one of the two
  is handed straight back to the utterance path with the same connector
  (`StreamUnsupportedError` to `PathEnd.HANDOFF`), so nothing regresses and
  nothing is silently lost, but nothing streams either. Whether to flip
  `gpt-4o-transcribe`'s `prefer` to realtime is a product decision, about cost
  and about which endpoint an existing session moves to; it is not made here.
  Whether "this model's realtime session supports server-side turn detection"
  should become a `capabilities.yaml` field rather than a connect-time
  discovery is the same question asked of Decision (2), and worth revisiting
  once Phase 3 shows whether any other vendor has the same split.
* **The win is a faster final, not text while speaking.** Against
  `gpt-4o-transcribe`, median time from the end of a turn (the vendor's own
  `audio_end_ms`) to its settled text was **758ms** over seven turns of a
  60-second clip, comfortably under the utterance path's floor of 800ms of
  trailing silence *before* the request goes out plus a round trip. But its
  `delta` events do not arrive during a turn: every turn produced one interim
  roughly half a second before its final, so median time to first interim was
  **7.6s**, which is a turn's length, not a latency. The interim machinery is
  right and the vendor is not using it. Phase 3 should measure this per vendor
  before anyone promises a GM text while they speak.
* **A watchdog that only counts voiced audio kills long turns.** Because that
  session says nothing for a whole turn and everything at its close, the first
  run's 15-second window fired inside a 24-second turn, dropped the
  connection, and cost 19 seconds of speech to the gap that followed. Decision
  (4)'s amendment above is that measurement: the watchdog now waits for the
  local VAD to go quiet before it counts silence as death, because every
  vendor emits *something* at a turn boundary and none of them promise
  anything inside one.

Phase 3 repeated the same harness against the other four vendors as each was
migrated, all on 2026-09-07 unless noted.

**AssemblyAI**, `universal-3-5-pro`, merged at `a4d7351`:

* Interims arrive mid-turn: first interim **1004ms** median, final **988ms**
  median after a turn ends. A 60s LibriVox clip at wall clock produced 8
  turns, 0 gaps, sentence-shaped 3.3-10s spans with a visible 10.0s cap and a
  dropped word at two turn boundaries.
* **Deviation from the plan.** The endpoint closes any single audio message
  outside 50-1000ms (close code 3007, "Input Duration Violation", verified
  live), which Phase 0 did not anticipate: the streaming shape buffers audio
  to 60ms before sending rather than at capture size.
* Labels are session-scoped and hold across turns, the reason this vendor was
  worth migrating for diarization, but accuracy on a two-narrator clip was
  poor: 8 of 10 turns labelled B including clear narrator-A passages, and a
  second speaker invented on 2 of 8 single-narrator turns. Spliced readings
  are not a real conversation, so this is a caveat on the measurement, not a
  verdict on the feature.
* **The language finding.** `prepare()` and `open_stream()` both sent
  `language=<row language>` in the query string, which Universal-Streaming v3
  does not define as a parameter at all; the vendor silently dropped it and
  picked up no language hint. The real parameter is `language_codes`, a JSON
  array (`?language_codes=["de"]`), documented for `universal-3-5-pro` only.
  Fixed at `f8d832f`, on this branch as of `7bbcb70`: both call sites build
  the query string through the connector's one `_params` method, so the fix
  is gated on the model in one place. Verified by unit and integration tests
  pinning the query-string encoding, and against the real vendor with a
  German LibriVox clip streamed at wall clock: 8 turns of correct German over
  60 s, first interim 1062 ms median, final 768 ms median, and `transcribe_one`
  on a 7 s clip equally correct. The old `language=de` also came back as
  German on that clean clip, because `universal-3-5-pro` code-switches by
  default, so the value of the fix is deterministic steering rather than a
  visible change on easy audio.

**Deepgram**, `nova-3`, merged at `fcd0af3`:

* Interims arrive roughly once a second: first interim **988ms** median on a
  60s single-reader clip, **1151ms** on a 90s two-reader clip. Final text
  lands 152 to 187ms after a turn ends. 0 gaps. Diarization: 91% of words
  outside the splices carried the right narrator's label, and each narrator
  kept one speaker number for the whole clip.
* Two places the real vendor contradicts its own docs. `SpeechStarted` is not
  a turn start: 12 arrivals for 7 turns, and every timestamp lagged the
  segment it belonged to, so the connector does not use it. `UtteranceEnd`
  can arrive late, describing a turn `speech_final` already closed, without
  the documented `last_word_end: -1` marker (4 of 4 measured); it is ignored
  whenever its own end is at or before the open turn's start, since acting on
  it orphaned dimmed interim rows that never resolved.
* No change to `streaming.py` was needed for this vendor; its turn shape fit
  the contract as written.

**Gemini Live**, `gemini-3.5-transcribe-live`, merged at `5631eaf`, 90 turns
over a 780s paced run:

* First interim **752ms** after speech onset (p90 1264ms); 89 of 90 turns
  carried several mid-turn revisions (median 16); final text settled **265ms**
  after the last interim (p90 450ms); turn length p50 7.0s. A separate paced
  13-minute run cost one gap marker of one second.
* **Session cap, and a deviation from the plan.** `goAway` arrives after 9
  minutes with `timeLeft: "50s"`, and the connector spends that time by
  leaving at the next turn boundary, so the reconnect lands on silence rather
  than mid-word. `sessionResumption` is accepted in every setup, but the
  service has never once answered with a handle, so every reconnect is a
  fresh session regardless.
* `silenceDurationMs: 300` was chosen from three runs (500 produced 4 to 23s
  turns, 200 split a sentence mid-way); `endOfSpeechSensitivity` was measured
  inert.
* No offsets at all: Google states no timing of any kind for this model, so
  `at` stays unset and the base falls back to the capture timestamp of the
  last frame written, exactly as Decision (5) anticipated for "a vendor that
  states no offset at all."
* Unverified: `contextWindowCompression` as a way to extend the 9-minute cap.

**x.ai**, mock-verified only: no `xai-` key exists in the secret store, so
this connector has never spoken to the real endpoint, and no frame in the repo
was ever recorded from api.x.ai, unlike every other vendor's mock. `TurnFinal`
no longer states `at`, matching Deepgram: a turn's start is what its first
partial said and does not move regardless of what a later message claims.
`interim_results=true` plus `endpointing` is documented as the endpoint's
default behaviour rather than an opt-in feature, so nothing here raises
`StreamUnsupportedError`; whether that documentation holds, and which of the
two documented readings of "cumulative" text is the real one, are both
unverified.

## Hardening after review

Five fix groups landed after the connectors above were verified, closing gaps
a review found without reopening the decisions themselves. The streaming-core
ones are folded into Decisions (3), (4) and (5) above; this collects all five
with their merge hashes.

* **Streaming core (`f21a8f1`).** Diarization moved beside the stream
  (Decision 5) and gap markers now survive a failover (Decision 3), both
  above. In addition: a turn's key is derived once, when it opens, so a final
  that revises the turn's start moves its timestamps but never the row it
  replaces. A partial naming a turn this connection already closed is dropped
  rather than reopened. An empty final for a turn that published an interim
  settles with that interim's text, since a vendor closing out a near-silent
  lead-in with nothing is routine, not a retraction. `RollingPcm.slice`
  returns the clip's real start alongside the clip, so a turn whose opening
  aged out of the 90-second window shifts the diarizer's segments by what was
  actually sent rather than by what was asked for. Open turns, stated ends
  and closed refs are each bounded per connection (32, 64 and 128
  respectively), so a misbehaving vendor cannot grow one without bound. A stop
  arriving mid-reconnect ends the stream at once rather than spending the rest
  of the reconnect budget on audio that is not coming. `StreamPath.run` now
  guards every provider's stream, so a connector that raises instead of
  returning a `StreamOutcome` becomes a failover rather than an escaped
  exception, and connection teardown, closing the socket and settling open
  turns, runs each step under its own 5s bound and never raises.
  `SttRouter._merge_diarization`
  gained the same resilience as the streaming path: a diarizer error
  publishes the utterance unlabelled instead of raising out of the live path.
* **Diarization service (`c5e7868`).** A cluster with no segment over the
  minimum embedding length may match a remembered voice but never opens a new
  one. `DIAR_MAX_SPEAKERS` (default 12) bounds a session's bank when the
  caller sends no `max_speakers` of its own, which is always true on the live
  path. The degrade path no longer renumbers the whole call when one cluster
  has no embedding: that cluster's segments are left unlabelled and the rest
  still resolves against the session, rather than every speaker in the call
  losing its session identity because one cluster could not be placed.
  `/healthz` also loads the embedding extractor and reports
  `session_memory: true` and a per-process `generation`, so `probe_diarizer`
  grades a service without the flag `DEGRADED` rather than `HEALTHY`.
  `install.sh` writes `COMPOSE_PROFILES=diarization` to `.env` so later
  updates rebuild the service's image; `update.sh` and `update-fast.sh` print
  the manual rebuild command when a diarization container is running with the
  profile inactive. See ADR 0007's restart bullet for the session-memory side
  of this.
* **Reprocess bank id (`6148240`).** A re-transcribe job diarizes under
  `{session_id}:job:{job.id}` through `_JobBankDiarizer` (`reprocess/jobs.py`)
  and deletes only that bank at close: its own `RemoteDiarizer` used to share
  the live session's bank id, so closing it after the job finished deleted
  the live capture's remembered speakers out from under it. `merge.py`'s
  wordless-turn fallback sums each speaker's overlap across every segment
  touching the span rather than taking whichever single segment overlapped
  most.
* **Transcript UI (`a97bd7a`).** The session page dims interim rows. A
  session still `CAPTURING` when the process dies has its leftover interims
  swept at the same startup pass that fails it
  (`SessionRepository.mark_interrupted`), so a killed process does not leave
  a half-typed row looking like settled text forever. The dashboard's live
  feed keys a turn by session id as well as source and turn id, since a
  vendor's own turn handle can restart from a small integer every session
  (AssemblyAI does), which used to let a new session's first turn replace the
  previous session's row in a pane that never clears between sessions. Gap
  markers are excluded from segment counts, the session player's timeline
  dots, and search, since a gap is audio nobody transcribed rather than a
  segment of speech.
* **Connectors (`fd82240`).** Gemini invents a ref it never had: turns are
  numbered per connection and every signal carries that number, so a late
  `inputTranscription` arriving after `generationComplete` already settled the
  turn from its newest interim replaces that row instead of writing a second
  final, which is the same key-stability fix the streaming core made for a
  revised start, applied to a vendor that names no turns at all. xAI stopped
  restating a turn's start on its final (see "What the real vendor said"
  above). Deepgram clears its interim buffer only when a settling segment
  carried text, so a segment that settles empty no longer erases what was
  already said. OpenAI logs and continues on a per-item transcription failure
  instead of ending the connection over it, and now raises rather than
  returning on an unrecognized `session.update` error, so that attempt spends
  the reconnect budget instead of leaving a socket configured with no server
  VAD running silently. `StreamUnsupportedError` is narrower: probed against
  Deepgram's and AssemblyAI's real error bodies, a 400 or `error_code 3006` is
  permanent only when the vendor names the model in it; a refused optional
  parameter (a keyterm, a language code, an endpointing value) is dropped and
  retried once instead, and the drop persists across reconnects and into the
  utterance-path fallback, which shares the same parameter builder and used to
  fail the same way. AssemblyAI now leaves at a turn boundary 60 seconds
  before its own session cap, mirroring Gemini's `goAway` handling. A non-JSON
  frame is skipped with a log line instead of ending the reader, and closing a
  socket is bounded and survives the caller being cancelled mid-close. Each
  vendor's mock was corrected to match what was measured rather than what was
  guessed: OpenAI's deltas arrive only at turn close, Gemini's service has
  never once sent a resumption handle so the mock no longer offers one by
  default, and xAI's word offsets now move on settle since that is the
  behaviour the fix above exists for.

## Implementation plan

**Phase 0, research, no code.** Each connector already parses part of its
vendor's turn protocol; start from that and find what is missing for a
stream that never closes. Deepgram: `Results` with `is_final` is parsed
(`deepgram.py`), and the comment there records that Deepgram's own endpointing
already splits one utterance into several finals with a near-silent lead-in;
confirm `UtteranceEnd` and `speech_final` as the turn signals. AssemblyAI:
`Turn` with `end_of_turn` and `turn_order` is parsed (`assemblyai.py`), and
words carry speakers under `speaker_labels=true`; confirm behavior on a
session that is never `Terminate`d. Gemini Live: measured, not documented,
in `gemini_live.py`: `generationComplete` ends a turn (`turnComplete` never
appeared in 200+ frames), `interimInputTranscription` is the partial,
`inputTranscription` the final, no word timings, and audio pushed faster than
realtime desynchronizes the service's turn machinery, which a paced live feed
avoids by construction. x.ai: `transcript.partial` with `is_final` and
`transcript.done` are parsed (`xai.py`); `Finalize` was deliberately left
unused and is the thing to re-examine. OpenAI: `turn_detection: None` today;
confirm the server VAD event names and that `speech_started`/`speech_stopped`
carry the offsets a `start_ts` needs. Write the per-vendor findings down
before Phase 1.

**Landed**: each connector's module docstring records these per-vendor
findings, updated against the real service rather than left as this phase's
notes alone.

**Phase 1, OpenAI Realtime only, behind an opt-in.** Server VAD on, a
persistent reader task, one `TranscriptEvent` per server-decided turn with
timing from the VAD events, finals only. Add the `SessionManager` streaming
path gated to this connector. Verify end to end with paced real audio, the
standard this project already applies to realtime vendors, measure latency
against today's 800ms floor, and check the session page and diarization
before touching any other connector.

**Landed**, at `21a1524`, with two deviations from this paragraph. No opt-in
flag exists or was needed: `SessionManager` picks the streaming path from a
fact about the connector's class (`is_streaming(primary)`), never from a flag
or the model row, so migrating a connector is what turns its models on (see
the docstring above `_start_live_path` in `session/manager.py`). And interims
shipped from the start rather than finals only, per the amendment under
Decision (1). The connector migrated first turned out to be inert for both
models `capabilities.yaml` routes to it, since OpenAI refuses server VAD for
`gpt-live-transcribe` and `gpt-realtime-whisper`; see "What the real vendor
said" above.

**Phase 2, failover and timeout for a stream.** Build Decision (3) and (4)
against the Phase 1 connector plus a call-shaped fallback. A fallback is any
enabled provider row (`SessionManager._resolve_providers`, `req.
fallback_provider`), so a streaming primary with a batch fallback is a
configuration a user can already save; this is where the two shapes first
have to hand audio to each other.

**Landed**: `StreamConfig`, `TranscriptStream`'s reconnect and watchdog, and
`StreamPath.handoff` in `session/streaming.py` are this phase, and the
amendments under Decision (3) and (4) are what real audio changed about it.

**Phase 3, the remaining four**, one at a time: `AssemblyAIBackend`,
`DeepgramBackend`, `GeminiLiveBackend`, `XaiBackend`, each verified against
its real vendor with paced audio.

**Landed**, verified against the real vendor for three of the four
(AssemblyAI at `a4d7351`, Deepgram at `fcd0af3`, Gemini Live at `5631eaf`; see
"What the real vendor said" above for the numbers). `XaiBackend` landed on
this branch too but mock-verified only, no `xai-` key exists in this
environment's secret store. Two deviations the plan did not anticipate:
AssemblyAI's streaming shape buffers audio to 60ms before sending rather than
at capture size, because the endpoint closes any single message outside
50-1000ms; and Gemini Live leaves its session at the next turn boundary
rather than mid-turn when the vendor sends `goAway`, so its 9-minute session
cap costs a reconnect timed to land on silence instead of a word.

**Phase 4, what a turn carries.** With real turn shapes in hand: the
remote-diarization buffering from Decision (5), timestamps for the two
vendors that return none, replace-by-key handling for interims in `_persist`
and the feeds, and how the version list presents a reprocess whose segments
no longer match the original's. Consider scoping the speaker-consistency gap
alongside, since both are about what a turn means once STT stops being the
source of utterance boundaries.

**Landed earlier than written here**: the remote-diarization buffering and
the replace-by-key handling arrived with Phase 1 rather than after Phase 3,
because Phase 1 needed both to ship interims safely (see the amendments under
Decision (1) and (5)). The version-list note landed separately
(`TranscriptVersions.svelte`, fast-forwarded onto this branch at `d13fdc2`).
The speaker-consistency gap was scoped alongside as suggested here, and
closed by ADR 0007 rather than by this one.

**Phase 5, close the loop.** Update `stt/base.py`'s docstring, CONTEXT.md's
Transport definition and `openai_realtime.py`'s module docstring to what
shipped, and record the final shape in a new ADR that supersedes this one,
the way ADR 0005 superseded one bullet of ADR 0001.

**Landed in part**: the docstring and CONTEXT.md updates shipped with Phase
1, ahead of schedule (see Consequences). The new ADR to supersede this one
has not been written; this docs pass extended this ADR in place instead with
the other four vendors' measurements and the language finding, since nothing
here has been superseded, only completed.
