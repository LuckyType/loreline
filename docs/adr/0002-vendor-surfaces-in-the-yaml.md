---
status: accepted
date: 2026-09-03
---

# Vendor surfaces are declared in capabilities.yaml, per interaction and transport

## Context

`capabilities.yaml` said what a model can do, not how to reach its vendor. Each
provider carried one `base_url` and a `catalog_endpoint` that only the staleness
check read. The real addresses were fourteen constants across twelve modules,
one per connector, plus auth tables in code: `_AUTH` in the staleness reader,
`_BASE_URLS` and `_health_path` in the LLM client, the kind sets in the picker
catalogue. Every connector applied a provider row's `base_url` by its own rule.
Gemini showed why one `base_url` cannot work. Its transcription and its chat are
two Google surfaces with different paths and different auth, so the second lived
in code with an apology in the yaml.

## Decision

Each provider declares a `surfaces` block: one surface per interaction, and one
per transport under `transcribe`, each with `url` and `auth`, optional fixed
`headers`, an optional `health` probe path, and `overridable: true` where a
provider row's `base_url` may replace it. A `catalog` surface, single or per
interaction, is where the picker and the staleness check read the vendor's list.
Endpoints and auth follow the surface a model's chosen transport uses, never the
vendor object and never the model. One accessor, `capabilities.surface_for`,
applies the row's override, so a socket address reaches the streaming surface
only and an HTTP address the HTTP ones, and yields the effective URL and auth.
`AuthScheme` spells the credential in one place.

## Consequences

* Every endpoint constant and per-kind auth table is gone. Connectors, the LLM
  and video clients, both catalogue readers and the diarizer call the one
  accessor. The tables left in code are wire parsing, `_ENVELOPES`.
* ADR 0001's "per-kind tables stay tables" now means tables of wire behaviour,
  not of endpoints.
* A self-hosted row with no base URL is an error, not a request to OpenAI.
