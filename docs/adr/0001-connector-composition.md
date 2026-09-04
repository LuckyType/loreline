---
status: accepted
date: 2026-09-03
---

# STT connectors compose through capabilities.yaml and one Connector base

Two things compose an STT connector and nothing else: the per-model facts in
`capabilities.yaml`, and one base class, `Connector`, with exactly one level of
inheritance below it, `HttpConnector`, for the batch connectors. There is no
per-vendor Provider class hierarchy, and nobody is to add one.

## Context

Eight connectors serve five vendors over two transports, and every one of them
ran the same loop per utterance with a different vendor call in the middle. The
obvious next shape was a class per vendor, a Deepgram provider owning both its
streaming and its batch connector, and so on.

`capabilities.yaml` is already the single gate on what a model can do, and the
browser renders its pickers from that file. The facts that would go on a vendor
object are per surface, not per vendor. The streaming URL, the batch URL, the
catalogue URL and the auth scheme all differ between one vendor's own
transports. The grain is finer still. Which endpoint and protocol apply follows
from what the chosen model supports, its transports, its preferred one, the
field its glossary travels in, so a vendor object cannot hold the fact without
restating it per model. A provider class would restate `ProviderSpec` in code
and pull capability facts out of the yaml into a place the browser cannot see,
and the two would then drift.

## Decision

* `Connector` owns the per-utterance loop and the `TranscriptEvent`. A
  connector supplies `prepare` and `transcribe_one`.
* `HttpConnector` adds the httpx client's lifetime and the raise-with-vendor-
  body rule for the batch connectors. The hierarchy stops there.
* Anything a model can or cannot do is a fact in `capabilities.yaml`, read at
  request time, never a class attribute.

## Consequences

* Per-kind tables, meaning query parameters, auth headers and payload parsing,
  stay tables in the `_vendor.py` helper modules beside the code that reads
  them, shared by that vendor's two connectors without a class in between.
* Health probing and catalogue reading are separate concerns from
  transcription. Each is deepened on its own rather than folded into a vendor
  object.
* The speaker rule is one function, `first_labelled_speaker`, not eight copies.
