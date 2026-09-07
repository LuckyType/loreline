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

## Consequences

* Batch and reprocess are untouched: same contract, same connectors, same
  test fakes, same `stored_audio_backend()` forcing `prefer_batch=True`.
* The four per-utterance connectors keep their behavior and their 800ms/30s
  floor until each is migrated; a half-finished migration leaves every model
  working, some faster than others.
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
  the opposite, gets corrected on the way.
* A reprocess version of a streamed session will not line up segment for
  segment with its original: reprocess reads VAD boundaries from the index,
  the original carries the vendor's turns. Nothing breaks, the index has no
  reader in the browser, but the two versions of one session will differ in
  shape, not only in text, and the version list should say so.
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
  not. Phase 3, the remaining four connectors, and Phase 5, the ADR that
  supersedes this one, are still ahead.

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

**Phase 1, OpenAI Realtime only, behind an opt-in.** Server VAD on, a
persistent reader task, one `TranscriptEvent` per server-decided turn with
timing from the VAD events, finals only. Add the `SessionManager` streaming
path gated to this connector. Verify end to end with paced real audio, the
standard this project already applies to realtime vendors, measure latency
against today's 800ms floor, and check the session page and diarization
before touching any other connector.

**Phase 2, failover and timeout for a stream.** Build Decision (3) and (4)
against the Phase 1 connector plus a call-shaped fallback. A fallback is any
enabled provider row (`SessionManager._resolve_providers`, `req.
fallback_provider`), so a streaming primary with a batch fallback is a
configuration a user can already save; this is where the two shapes first
have to hand audio to each other.

**Phase 3, the remaining four**, one at a time: `AssemblyAIBackend`,
`DeepgramBackend`, `GeminiLiveBackend`, `XaiBackend`, each verified against
its real vendor with paced audio.

**Phase 4, what a turn carries.** With real turn shapes in hand: the
remote-diarization buffering from Decision (5), timestamps for the two
vendors that return none, replace-by-key handling for interims in `_persist`
and the feeds, and how the version list presents a reprocess whose segments
no longer match the original's. Consider scoping the speaker-consistency gap
alongside, since both are about what a turn means once STT stops being the
source of utterance boundaries.

**Phase 5, close the loop.** Update `stt/base.py`'s docstring, CONTEXT.md's
Transport definition and `openai_realtime.py`'s module docstring to what
shipped, and record the final shape in a new ADR that supersedes this one,
the way ADR 0005 superseded one bullet of ADR 0001.
