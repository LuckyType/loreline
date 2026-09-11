---
status: accepted
date: 2026-09-11
---

# An imported recording is a session, not a second kind of thing

## Context

Every feature this app has is written against one pair of artifacts a capture
leaves behind: a continuous mono 16 kHz 16-bit PCM WAV per session
(`SessionAudioWriter.append_frame`) and a JSON sidecar naming each voiced
utterance's offset into it (`AudioStore.read_utterances`). Re-transcription
reads them (`ReprocessManager._transcribe_session`), diarization reads the WAV
(`_diarize_session`), the player streams it, the exports and the summary read
the rows those two produce, merge concatenates them, and delete removes them.
Nothing in that list asks how the audio got there.

A GM without a box at the table still has a recording of the evening: a phone
memo, a handheld recorder's card, a Discord rip. Loreline could do nothing
with it. That is the one gap that puts the whole app out of reach of somebody
who has not bought and wired the hardware, and it is the smallest gap in the
codebase: the audio exists, the pipeline exists, only the path between them is
missing.

Two shapes were available.

The first is an import pipeline of its own: an uploaded file gets its own
storage, its own transcription driver, its own version model, its own page.
That is a second implementation of everything above, and every feature added
afterwards would have to be written twice or would silently only work on one
of them. The version table, the diarize target, the merge, the speaker rename
map and the video prompt are all keyed on a session; a second kind of thing
means each of them grows a branch.

The second is to make an import produce exactly what a capture produces and
then get out of the way. That is what was already nearly true: `rebuild_index`
exists, takes a bare WAV and a detector factory, and reconstructs the sidecar
by running the same VAD and the same `VadChunker` the live path uses - it was
written for the startup sweep that adopts a recording an unclean death left
without one (`recover_orphaned_indexes`). An imported WAV is the same problem
wearing a different hat: complete audio, no index.

## Decision

* An import writes the two capture artifacts and one session row, `status =
  completed`, `started_mono = 0.0`, `primary_provider = None`, `ended_at =
  started_at + the recording's length`. Nothing downstream is told it is an
  import.
* Transcribing an import is an **ordinary re-processing job**. There is no
  import transcription path. `POST /api/session/import` may carry a
  `transcribe` block, and all that does is call `ReprocessManager.enqueue` the
  way the New transcription dialog does. The version that job writes is the
  import's transcript, and its log is the import's log.
* The utterance index is built by `AudioStore.rebuild_index` with
  `base_ts=0.0` and the detector `loreline.audio.vad.default_detector` returns
  - the same one the live capture and the startup sweep use. `base_ts=0.0`
  with `started_mono = 0.0` is what makes the recording's own clock the
  session clock: `ReprocessManager._drive` rebases every event by
  `started_mono`, so a segment's timestamp is its position in the WAV, which
  is what the player, the timeline dots and the SRT export already read.
* Decoding is `ffmpeg` as a subprocess, except for a file the `wave` module
  can already read as 16-bit PCM, which is rewritten by the stdlib plus the
  resampler the capture path uses. So a WAV import works on a box with no
  ffmpeg, and everything a phone actually records needs it.
* `sessions.origin` (`'capture' | 'import'`) and `sessions.import_name` are
  added (migration v22). They exist for the browser and for nothing else: an
  import's "original" version is empty by construction, and without `origin`
  that emptiness is indistinguishable from a capture whose STT died on the
  first utterance. The file name is kept because it is the only name the
  recording ever had.
* A failed import leaves nothing: no session row, no WAV, no temp file. That
  includes a client that hangs up mid-upload, and it includes a `transcribe`
  block that could not be started - the request meant "a transcribed
  recording", and half of that would leave the GM uploading the file a second
  time and holding two sessions, one of them silent.

## Consequences

* Zero new transcription code, and no second set of transcription bugs. Every
  feature added to sessions afterwards works on imports without being asked
  to.
* The browser is the only place that branches on `origin`, and it branches on
  exactly one fact: the version table does not offer an empty "original" as a
  selectable transcript, the session page opens on the newest transcription
  instead, and the transcript card offers a Transcribe button where a capture
  would have text. Nothing in `src/loreline` outside the import route reads
  the column.
* `ffmpeg` joins the Dockerfile and the source install. It is the first
  external binary this app shells out to.
* A session's status enum gains no member. An import is `completed` on
  arrival, which is what every reader of that field is actually asking.
* Two ceilings are configuration rather than constants,
  `LORELINE_IMPORT_MAX_MB` (413 past it) and `LORELINE_IMPORT_MAX_HOURS` (422
  past it), because one request that can write a gigabyte is a denial of
  service on a Raspberry Pi with a 32 GB card.

## Follow-ups, deliberately not in this pass

* **Recording in the browser.** `MediaRecorder` on the phone at the table,
  uploaded when the session ends, is the natural next step and removes the
  file transfer entirely. It needs a wake lock and chunked upload, neither of
  which this shape needs, and both of which are their own problem.
* **Resumable or chunked uploads.** One request, one session, is enough for a
  LAN. It is not enough for a phone on mobile data, which is exactly what the
  item above would be.
* **Importing a transcript without audio.** Every version here is produced
  from stored audio; a transcript with no recording behind it would be the
  first version that is not, and the player, the diarizer and re-transcription
  all assume there is one.
