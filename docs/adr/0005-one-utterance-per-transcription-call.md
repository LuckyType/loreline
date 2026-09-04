---
status: accepted
date: 2026-09-04
---

# A connector transcribes one utterance from values it was handed

## Context

`STTBackend.transcribe` took an `AsyncIterator[Utterance]` and yielded events,
and every caller fed it exactly one: the router owns the timeout, the failover
and the diarization per utterance, so it wrapped each one in a single-item
generator to satisfy the type, and eight connectors inherited a loop that never
ran twice. Separately, one start request resolved the same provider and model
pair four times: the session manager, the registry, and the connector twice,
for the glossary ceiling and for the declared conflicts.

## Decision

* `transcribe(utterance, *, session_id, glossary=None) -> TranscriptEvent | None`.
  None means the vendor answered with nothing worth an event, not a failure.
* `create_backend` resolves the pair's `TranscribeCapabilities` once and hands
  the value over. A connector reads `caps.glossary` and the conflict groups off
  it and never queries `capabilities` itself. An unknown model resolves to
  None, which keeps its meaning: not annotated, send what you would have sent.

## Consequences

* This supersedes ADR 0001's "the base owns the per-utterance loop"; its two
  hooks, one event and one speaker rule stand.
* What a connector keeps between utterances is instance state and reads as it:
  the Realtime socket, the `verbose_json` tri-state, the guard's reported flag.
* `prepare` runs per utterance and stays cheap: it shapes values in memory.
