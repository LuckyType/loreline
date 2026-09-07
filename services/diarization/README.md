# Loreline Diarization Service (sherpa-onnx)

Self-hosted speaker diarization for Loreline. Runs on a LAN x86 host (off the
capture device). Exposes the HTTP contract consumed by Loreline's
`RemoteDiarizer`.

## Endpoints

- `GET /healthz` -> `{"status": "ok", "session_memory": true, "generation": "..."}`,
  or HTTP 503 with `{"detail": ...}` until both models below are configured and
  have loaded successfully
- `POST /diarize` (multipart `file` = mono WAV) ->
  `{"segments": [{start, end, speaker}, ...], "generation": "..."}`, or the same
  HTTP 503 shape while the models are not ready
- `DELETE /sessions/{session_id}` -> `{"deleted": bool}`, forgetting one session's
  remembered speakers. 200 either way, including for an id that was never seen.

Two fields describe the service rather than the audio. `session_memory` says this
build understands `session_id` instead of accepting and ignoring it, which is what
the image before it did and what no status code distinguishes; Loreline's endpoint
probe grades a service without the flag as degraded and says so on the settings
page. `generation` identifies this process and changes when it restarts, which is
how a caller notices that a session's speaker numbering has started over (the bank
is process memory - see below).

What the labels mean depends on whether the call carries a `session_id`:

- **Without one**, they are consecutive per call (`Speaker 0..k-1`) regardless of
  raw cluster ids, and mean nothing from one call to the next. If `min_speakers`
  and `max_speakers` are both sent and equal, that exact cluster count is enforced
  (otherwise clustering is automatic).
- **With one**, they are the session's speaker numbers, so a call can answer
  `Speaker 3` alone, and can leave a gap. A cluster the session could not place -
  no embedding at all, or one from a fragment too short to identify anyone by that
  matched no known voice - has its segments left out of the answer rather than
  labelled with a number that would name somebody else. Loreline's merge already
  handles words that no segment covers, which is what those words become.

## Session speaker memory

`POST /diarize` also takes an optional `session_id` form field, and it is what
makes the labels usable for a caller that diarizes one utterance at a time,
which is how Loreline's live capture calls this service.

Without it every call clusters on its own, so "Speaker 0" in one utterance has
nothing to do with "Speaker 0" in the next: a session of any number of people
comes back as one speaker, since almost every single-speaker utterance is
labelled `Speaker 0`. Measured on a 111 s two-narrator clip cut into 12
utterances, 11 of the 11 single-speaker ones came back as `Speaker 0`.

With it the service keeps a bank of speaker embeddings per session. Each call
still clusters its own audio; each of those clusters is then embedded and
matched against the voices that session has already heard, above a cosine
threshold, or opens a new speaker while the session is under `max_speakers`.
The same clip through the same path labels all 12 utterances correctly.

Semantics with a bank, which differ from a stateless call on purpose:

| Field | Meaning |
|---|---|
| `min_speakers` | A floor on the session. While the bank holds fewer voices, reusing one takes a stricter match, so a table said to hold three people is not collapsed into one. |
| `max_speakers` | A cap on the session's bank. Once it is full, a new voice is labelled with the nearest speaker already in it and does not modify that speaker's centroid. |

An exact bound (`min == max`) does **not** force that cluster count per call
here: it describes the session, and one utterance holding fewer speakers than
the table is the normal case, not an error.

Bounding the memory, since "the session ended" is a message that can go
missing: a bank is dropped after `DIAR_SESSION_TTL_S` idle (swept on every
call), the service keeps at most `DIAR_MAX_SESSIONS` of them and evicts the
least recently used, and `DELETE /sessions/{id}` drops one outright, which is
what Loreline sends when a capture stops.

| Variable | Default | What |
|---|---|---|
| `DIAR_SPEAKER_THRESHOLD` | `0.5` | Cosine similarity above which a cluster is a voice the session already knows |
| `DIAR_SESSION_TTL_S` | `3600` | Idle seconds before a session's bank is evicted |
| `DIAR_MAX_SESSIONS` | `32` | How many sessions are remembered at once |
| `DIAR_MAX_SPEAKERS` | `12` | How many voices one session may open before a further one borrows the nearest label instead. `0` lifts the cap |

The default threshold comes from a measurement rather than a guess: on a
two-narrator LibriVox clip through the models below, embeddings of the same
speaker sit at cosine 0.63 to 0.88 (median 0.77) and of different speakers at
-0.00 to 0.22 (median 0.14). 0.5 is the middle of that gap, and is also what
the within-call clustering uses as its distance threshold.

`DIAR_MAX_SPEAKERS` is the only bound on the live path, which sends neither
`min_speakers` nor `max_speakers`: one utterance cannot say how many people are
at the table. A tabletop group is three to six people, eight with guests, so the
default of twelve is roughly double a large table - it cannot squeeze out a voice
that is really there, and it still stops a noisy room from growing a speaker list
nobody can rename. A `max_speakers` sent with a call overrides it for that call.

A restart renumbers. The bank is this process's memory and nothing else (one
container per box, no store to keep in step), so a service restarted mid-session
forgets every voice and numbers the next turn from `Speaker 0` again. That is why
every answer carries `generation`: Loreline logs a warning the first time it
changes for a session, and the fix is renaming the speakers on the session page,
since only a human knows which half of the transcript was whom.

Session memory loads a second copy of the embedding model, once: sherpa-onnx's
diarization API returns clustered segments and never the embeddings behind them,
so there is nothing to reuse. `/healthz` loads it too, rather than waiting for
the first session call to reach it - every call Loreline makes carries a session
id, so a service whose extractor cannot load can serve none of them, and a health
check that skipped it would report that service as healthy.

## Models

sherpa-onnx needs a segmentation model (pyannote) and a speaker-embedding model.
Download from the sherpa-onnx model releases and point the service at them:

```bash
export DIAR_SEGMENTATION_MODEL=/models/sherpa-onnx-pyannote-segmentation-3-0/model.onnx
export DIAR_EMBEDDING_MODEL=/models/3dspeaker_speech_eres2net_base_sv_zh-cn_3dspeaker_16k.onnx
```

## Run

```bash
pip install -r requirements.txt
uvicorn app:app --host 0.0.0.0 --port 8001
```

Or via Docker (mount the model directory):

```bash
docker build -t loreline-diarization .
docker run --rm -p 8001:8001 \
  -e DIAR_SEGMENTATION_MODEL=/models/seg.onnx \
  -e DIAR_EMBEDDING_MODEL=/models/emb.onnx \
  -v /path/to/models:/models loreline-diarization
```

If the models are not configured, or fail to load, both `/healthz` and `/diarize`
return HTTP 503: the first successful call loads them and the result is cached
for the life of the process, so a misconfigured deployment shows unhealthy
before a diarize job ever runs, not only once one fails. A *failed* load is
cached too, for a minute, so a broken deployment does not run a doomed ONNX
init for every request it is sent, while a volume that mounts late is still
picked up without a restart. Use `mocks/diarization.py` for offline
development/tests.
