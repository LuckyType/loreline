---
status: accepted
date: 2026-09-04
---

# One vendor catalogue reader behind the pickers, the video list and the staleness gate

## Context

Three modules fetched and parsed vendor catalogues: the picker
(`stt/catalog.py`), the video client and the staleness reader. They produced
two row types (`ModelInfo` with pricing, `VendorModel` with the facts the
yaml mirrors), and only the staleness reader read the `catalog` surface that
ADR 0002 declared; the picker kept its own kind set for which bodies it could
parse, and the video client re-fetched a list the picker had already read.

## Decision

`loreline.catalog` is the one reader: one fetch, the surface's own auth, one
parser per vendor body, one row type (`VendorModel`, now carrying price, name
and the video knobs) inside one `CatalogProbe` with an explicit status. The
pickers, `list_video_models` and the staleness package project that probe;
none fetches. Whether a picker may offer a catalogue live is declared on the
surface (`picker: false` for Deepgram and Gemini), not decided in code.

## Consequences

* A vendor payload change breaks one parser and one test file.
* A picker can list live models wherever the reader parses that vendor and
  the yaml allows it; today that is the OpenAI-shaped family.
* The staleness package is a consumer of the probe, not its owner.
* An unusable probe means "curated list" to a picker and "not checked" to
  the gate; the same status carries both.
