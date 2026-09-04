---
status: accepted
date: 2026-09-04
---

# One health probe behind the settings badge, /healthz and the diarizer

## Context

"Does this provider's key work at this URL" had three gradings. The settings
route built a whole transcription connector to ask it, which made the registry
resolve a model nobody had chosen, and `create_backend(model=None)` and a
curated-catalogue guard existed for that one caller. Eight connectors carried a
`health` method, four of them byte-identical apart from the socket URL and the
first frame. The LLM client had its own. The remote diarizer still returned a
bool graded `status < 500`, the defect `loreline.health` removed elsewhere.

## Decision

`loreline.health_probe.probe_provider(config, api_key)` is the one entry point.
It takes a provider row and its key, picks the surface the yaml declares for the
kind through `probe_target`, the chat surface for a kind that summarizes and
otherwise the transcription surface its default model runs on, and asks it. An
HTTP surface is asked at its declared `health` path with its auth scheme, a
socket surface through one function, `probe_socket(url, headers, frame)`.
`loreline.health` grades every answer, the diarizer's included.

## Consequences

* Connectors have no health method, and `create_backend` requires the model.
* The per-kind probe is one table read from the yaml: the surface, its auth and
  its question. A static socket frame, Deepgram's `CloseStream`, is yaml data
  under `health: {frame: ...}`. A frame that would need a model is not sent.
* `/healthz` carries the diarizer's graded status and detail beside the bool.
