---
status: accepted
date: 2026-09-06
---

# The diarization service remembers a session's speakers

## Context

`SttRouter._merge_diarization` posts one utterance's audio to the diarization
service and merges the segments it gets back. The service clustered each
request on its own, so its labels only meant anything within one call: a
one-voice utterance, which is nearly all of them, came back as `Speaker 0`
whoever had spoken. On a 111 s clip of two LibriVox narrators cut into 12
utterances, 11 of the 11 single-speaker ones came back as `Speaker 0`, and the
only second label came from the utterance holding both voices. A session's
rename map is one label to one name (`sessions.speaker_names`), so naming
`Speaker 0` renamed the whole table. Handed that same clip in a single request,
the same models attributed 98% of the spoken time to the right one of two
speakers: the models were never the problem, the call pattern was.

## Decision

* `POST /diarize` takes an optional `session_id`. Per session the service keeps
  a bank of speaker centroids: each call still clusters its own audio, then
  each cluster is embedded and matched, one to one and above a cosine
  threshold, against the voices that session has already heard, or opens a new
  speaker while under `max_speakers`. Labels are then stable across calls.
  Without `session_id` the per-call behavior is exactly what it was.
* The bank, not the call, carries the speaker bounds: `max_speakers` caps it,
  `min_speakers` raises the bar for reusing a voice while it is below that
  floor, and an exact bound no longer forces a cluster count per call, because
  one utterance holding fewer speakers than the table is normal.
* Memory is bounded three ways, since a session end is a message that can go
  missing: an idle TTL, a cap on remembered sessions, and
  `DELETE /sessions/{id}`, which `RemoteDiarizer.aclose` sends for every id it
  used.

## Consequences

* The service is no longer stateless, and two replicas behind one address would
  answer with different labels for one session. It is deployed as a single
  container per box (`docker-compose.yml`, profile `diarization`); scaling it
  out would need the bank moved out of the process or the session pinned.
* Session memory loads a second copy of the embedding model, once, on the first
  call that asks for it: sherpa-onnx returns clustered segments and never the
  embeddings behind them.
* A wrong match is now sticky within a session rather than only within an
  utterance. A forced match, made when the bank is at its cap, deliberately
  does not update the centroid it borrowed.
