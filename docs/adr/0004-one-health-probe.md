---
status: accepted
date: 2026-09-04
---

# One health probe behind the settings badge, /healthz and the diarizer

## Context

"Does this provider's key work at this URL" had three gradings. The settings
route built a whole transcription connector to ask it, which made the registry
resolve a model nobody had chosen (`create_backend(model=None)` and a
curated-catalogue guard existed for that one caller); eight connectors carried
a `health` method, four byte-identical apart from the socket URL and the first
frame; the LLM client had its own; and the remote diarizer still returned a
bool graded `status < 500`, the defect `loreline.health` removed elsewhere.

## Decision

`loreline.health_probe.probe_provider(config, api_key)` is the one entry
point. It takes a provider row and its key, picks the surface the yaml
declares for the kind (`probe_target`: the chat surface for a kind that
summarizes, else the transcription surface its default model runs on), and
asks it: an HTTP surface at its declared `health` path with its auth scheme,
a socket surface through one function, `probe_socket(url, headers, frame)`.
Every answer is graded by `loreline.health`, including the diarizer's.

## Consequences

* Connectors have no health method; `create_backend` requires the model.
* The per-kind probe is one table read from the yaml: the surface, its auth
  and its question. A static socket frame (Deepgram's `CloseStream`) is yaml
  data, `health: {frame: ...}`; a frame that would need a model is not sent.
* `/healthz` carries the diarizer's graded status and detail beside the bool.
