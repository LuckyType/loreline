# Loreline Diarization Service (sherpa-onnx)

Self-hosted speaker diarization for Loreline. Runs on a LAN x86 host (off the
capture device). Exposes the HTTP contract consumed by Loreline's
`RemoteDiarizer`.

## Endpoints

- `GET /healthz` -> `{"status": "ok"}`, or HTTP 503 with `{"detail": ...}` until
  both models below are configured and have loaded successfully
- `POST /diarize` (multipart `file` = mono WAV) -> `{"segments": [{start, end, speaker}, ...]}`,
  or the same HTTP 503 shape while the models are not ready
- `DELETE /sessions/{session_id}` -> `{"deleted": bool}`, forgetting one session's
  remembered speakers. 200 either way, including for an id that was never seen.

Speaker labels are consecutive (`Speaker 0..k-1`) regardless of raw cluster ids. If
`min_speakers` and `max_speakers` are both sent and equal, that exact cluster count
is enforced (otherwise clustering is automatic).

## Session speaker memory

`POST /diarize` also takes an optional `session_id` form field, and it is what
makes the labels usable for a caller that diarizes one clip at a time rather
than the whole session at once. That is how Loreline's live capture always
calls this service, one VAD utterance per call on the batch path or one
vendor turn per call on a streaming connector's path (ADR 0006): either way,
each call still clusters only the audio it was given.

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

The default threshold comes from a measurement rather than a guess: on a
two-narrator LibriVox clip through the models below, embeddings of the same
speaker sit at cosine 0.63 to 0.88 (median 0.77) and of different speakers at
-0.00 to 0.22 (median 0.14). 0.5 is the middle of that gap, and is also what
the within-call clustering uses as its distance threshold.

Session memory loads a second copy of the embedding model, once, on the first
call that asks for it: sherpa-onnx's diarization API returns clustered segments
and never the embeddings behind them, so there is nothing to reuse.

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
before a diarize job ever runs, not only once one fails. Use
`mocks/diarization.py` for offline development/tests.
