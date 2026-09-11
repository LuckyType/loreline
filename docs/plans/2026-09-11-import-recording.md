# Plan: import a recording ("bring your own recording")

Goal: any audio file a GM already has (phone recording, m4a, mp3, ogg, opus,
webm, flac, wav) becomes a stored session that is indistinguishable from a
captured one, so every existing feature works on it unchanged: re-transcribe,
diarize, rename speakers, summarize, export, merge, delete, video. Nothing
about live capture changes. This is the feature that lets someone use Loreline
on a laptop with no box and no microphone.

## Design decision (write it as ADR 0008)

An import produces the same two artifacts a capture produces, the session WAV
and its utterance index sidecar, and a session row with `status = completed`.
It does not get its own transcription pipeline. Transcription of an import is
an ordinary re-processing job on a session whose "original" version is empty.
Rationale: the batch path (`ReprocessManager._transcribe_session` in
`src/loreline/reprocess/jobs.py`) already reads utterances from
`AudioStore.read_utterances`, and `AudioStore.rebuild_index` already turns a
bare WAV into that index by running the same VAD and chunker the live path
uses. Reusing both means zero new transcription code and no second set of
bugs.

## Backend

1. **Endpoint** `POST /api/sessions/import`, multipart, behind auth, in
   `src/loreline/web/routes/sessions.py`. Fields: `file` (required),
   `started_at` (optional epoch seconds; default: now), `campaign_id`
   (optional; a plain string today, the campaigns work will wire a picker),
   and an optional `transcribe` block (`provider_id`, `model`, `use_glossary`,
   `diarization` as on the existing re-process request) that starts a
   transcription job as soon as the import is stored. One request, one
   session.
2. **Streaming to disk.** Read the upload in chunks into a temp file under the
   data directory (never into memory; a four hour WAV is around 460 MB).
   Enforce `LORELINE_IMPORT_MAX_MB` (new setting, default 1024) while
   streaming and answer 413 past it. `python-multipart` is already a
   dependency; check FastAPI's `UploadFile` streams rather than buffers.
3. **Decode to the capture format**: 16 kHz, mono, 16 bit PCM WAV, the shape
   `SessionAudioWriter` produces. Use `ffmpeg` as a subprocess
   (`asyncio.create_subprocess_exec`, `-i in -ac 1 -ar 16000 -acodec pcm_s16le
   -f wav out`, with a timeout scaled to input size). Fast path: an input that
   is already PCM WAV is handled with the `wave` module plus
   `resample_pcm16` from `src/loreline/audio/resample.py`, so a WAV import
   works even where ffmpeg is missing. If ffmpeg is missing and the input is
   not PCM WAV, answer 422 with a message that names ffmpeg and the formats
   that work without it. Reject decoded audio longer than
   `LORELINE_IMPORT_MAX_HOURS` (default 8).
4. **Session row** via `SessionRepository.create`: a fresh id in the same
   format captures use, `status = completed`, `started_at` from the request,
   `started_mono = 0.0`, `ended_at = started_at + duration`, `audio_path` set,
   `primary_provider = None`. Migration v22 in
   `src/loreline/persistence/database.py` adds `origin TEXT NOT NULL DEFAULT
   'capture'` and `import_name TEXT` to `sessions`; imports write
   `origin = 'import'` and the original filename. Both fields go on the wire
   (`Session` model in `src/loreline/models.py`, regenerate types).
5. **Utterance index** with `AudioStore.rebuild_index(session_id,
   detector_factory=..., base_ts=0.0)` off the event loop, using the same
   detector factory the app already builds for the live path (find where
   `SileroVad` is constructed and reuse it). Confirm the timestamp convention
   the batch path expects for a session with `started_mono = 0.0` and write a
   test that a re-transcription of an import yields rows whose `start_ts`
   begin near zero.
6. **Transcribe now**: when the request carries the `transcribe` block, call
   the same code the existing `POST /api/reprocess` transcribe path calls and
   return the job id with the session. The version log of that job is the
   import's log. Write one app log line `session.imported` with size,
   duration, decoder used and elapsed time.
7. **Cleanup**: the temp file is removed on every path, including a client
   disconnect mid-upload. A failed decode leaves no session row and no WAV.
8. **Delete** already removes WAV, index, transcripts, logs and videos; verify
   an imported session goes the same way.

## Frontend

1. **History page** (`frontend/src/routes/sessions/+page.svelte`): an
   "Import recording" button beside the existing header actions, opening an
   `ImportRecordingDialog.svelte` under `frontend/src/lib/`: file input
   (`accept="audio/*,.m4a,.mp3,.ogg,.opus,.webm,.flac,.wav"`), a date and
   time field defaulting to the file's `lastModified`, a "Transcribe now"
   switch that reveals the same provider and model pickers `ReprocessPanel`
   uses (reuse `ModelPicker` and `actionSetup.transcription`, and the glossary
   and diarization choices), and an upload progress bar. Use
   `XMLHttpRequest` for the upload so progress events work; add the call to
   `frontend/src/lib/api.ts` next to the other session calls. On success go to
   the session page. Errors (413, 422, decode failure) show inline with the
   server's message.
2. **History rows**: an "imported" badge in the same style as the "merged"
   badge, and the filename in the row's title attribute.
3. **Session page**: the header meta shows "Imported from <name>". The
   "original" version of an import has no rows; `TranscriptVersions.svelte`
   must not present an empty, selectable "original" as if it were a
   transcript. Label it "no live transcript (imported)" and make the newest
   completed transcription the default selection. `TranscriptPanel` shows a
   clear empty state with a "Transcribe" call to action that opens the
   re-process row when the session has no transcript yet.
4. **Dashboard**: unchanged.

## Deployment and docs

- `Dockerfile`: add `ffmpeg` to the apt install line (it is not there today).
- `README.md`: the source install lists ffmpeg as a requirement; the "What it
  does" section gets an "Import" paragraph; `.env.example` documents
  `LORELINE_IMPORT_MAX_MB` and `LORELINE_IMPORT_MAX_HOURS`.
- `docs/adr/0008-imported-recordings-are-sessions.md` in the style of the
  existing ADRs.

## Tests

- Unit: the decoder (PCM WAV path with a resample, ffmpeg path skipped when
  the binary is absent, both size and duration limits), temp file cleanup on
  failure.
- Integration (`tests/integration/test_web_session.py` or a new
  `test_web_import.py`): upload a short generated WAV through the test
  client, assert the session row, `origin`, WAV, index; start a transcribe job
  on it with the fake provider the existing re-process tests use and assert
  rows near zero; 413 on an oversized upload; 422 on a non-audio file; delete
  removes everything.

## Out of scope, note as follow-ups in the ADR

- Recording in the browser (MediaRecorder on the phone, uploaded at the end).
  This is the natural next step and removes the file transfer entirely; it
  needs wake lock and chunked upload work that does not belong in this pass.
- Resumable or chunked uploads.
- Importing an existing transcript without audio.
