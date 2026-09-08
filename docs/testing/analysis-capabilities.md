# Provider, model and capability rules: a tester's map

Input for a manual black-box run against a live deployment. Everything below is
read off the code in this worktree, not from the running app: where the UI and
the backend disagree, that is recorded under SUSPECTED ISSUES rather than
smoothed over.

Source of truth for every capability fact: `src/loreline/capabilities.yaml`.
The backend reads it through `src/loreline/capability_config.py` (schema and
loader) and `src/loreline/capabilities.py` (typed, cached queries). The browser
reads the same document whole over `GET /api/capabilities`
(`src/loreline/web/routes/capabilities.py:26`, unauthenticated) and never
restates it.

Terminology used below:

* **kind**: the vendor type (`deepgram`, `openai`, ...), one entry per vendor in
  the YAML.
* **provider row**: one saved credential for a kind. Several rows of the same
  kind are allowed. A row carries no model.
* **interaction**: `transcribe`, `summarize` or `video`. Every picker is scoped
  to one.
* **transport**: `realtime` (websocket) or `batch` (HTTP request per utterance
  or per file). A property of the model, not of the vendor.

---

## 1. The vendor list

### 1.1 At a glance

| Kind | Label | Hosting | Auth scheme | Interactions | Live capture | Model list source |
|---|---|---|---|---|---|---|
| `deepgram` | Deepgram | cloud | `token_header` (`Authorization: Token`) | transcribe | yes | curated (catalogue `picker: false`) |
| `assemblyai` | AssemblyAI | cloud | `raw_header` (bare key) | transcribe | yes | curated (no catalogue exists) |
| `openai` | OpenAI | cloud | `bearer` | transcribe, summarize | yes | live `GET /v1/models` |
| `openrouter` | OpenRouter | cloud | `bearer` + attribution headers | transcribe, summarize, video | **no** | live, three separate lists |
| `gemini` | Google Gemini | cloud | `goog_header` native, `query_key` on the Live socket, `bearer` on the chat shim | transcribe, summarize | yes | curated (catalogue `picker: false`) |
| `xai` | xAI (Grok) | cloud | `bearer` | transcribe, summarize, video | yes | live for summarize only, curated for transcribe and video |
| `openai_compat` | Self-hosted (OpenAI compatible) | selfhosted | `bearer`, `auth: optional` | transcribe, summarize | yes | live `{base_url}/models` |

`live_capture: false` on OpenRouter is policy, not capability
(`src/loreline/capabilities.py:379`): its transcription API is one file upload
per request, and a cloud round trip per utterance during play is the wrong
trade. Re-processing stored audio with it is fine and supported.

### 1.2 Per vendor

#### Deepgram (`capabilities.yaml:77`)

* Key URL: `https://console.deepgram.com/signup?jump=keys`.
* Surfaces: realtime `wss://api.deepgram.com/v1/listen` (overridable, health
  probe sends the frame `{"type": "CloseStream"}`); batch
  `https://api.deepgram.com` (overridable, health path `/v1/auth/token`, chosen
  because `/v1/models` answers anonymous callers); catalogue
  `https://api.deepgram.com/v1/models` with `picker: false`.
* Offered models: `nova-3` (default), `nova-2`, `flux-general-en`,
  `flux-general-multi`.
* Hidden: `whisper-large` (batch only, connector unverified). Must never appear
  in any picker or favourites list.
* Glob annotations that never list anything: `nova-3*`, `nova-2*`, `whisper*`.
* UI fields: Name, Language, Favorite models, API key (required) with the "Get
  an API key" link. **No Base URL box.**

#### AssemblyAI (`capabilities.yaml:312`)

* Key URL: `https://www.assemblyai.com/dashboard/api-keys`.
* Surfaces: realtime `wss://streaming.assemblyai.com/v3/ws` (overridable, no
  health frame, the service greets first); batch `https://api.assemblyai.com`
  (overridable, health `GET /v2/transcript?limit=1`). No catalogue endpoint
  exists at this vendor, so the picker is always the curated list.
* Offered models: `universal-3-5-pro` (default), `universal-streaming-english`,
  `universal-streaming-multilingual`.
* Hidden: `universal-2`.
* UI fields: as Deepgram. **No Base URL box**, even though the YAML comment at
  `capabilities.yaml:328` says an EU account sets
  `https://api.eu.assemblyai.com` as the row's base URL. See SUSPECTED ISSUES.

#### OpenAI (`capabilities.yaml:466`)

* Key URL: `https://platform.openai.com/settings/organization/api-keys`.
* Surfaces: realtime `wss://api.openai.com/v1/realtime?intent=transcription`
  (overridable); batch `https://api.openai.com/v1` (**not** overridable on
  purpose); summarize `https://api.openai.com/v1` (overridable); catalogue
  `https://api.openai.com/v1/models`, `picker: true`.
* Transcribe models: `gpt-transcribe` (default), `gpt-live-transcribe`,
  `gpt-realtime-whisper`, `gpt-4o-transcribe-diarize`, `gpt-4o-transcribe`,
  `gpt-4o-mini-transcribe`, `whisper-1`. The last four carry
  `deprecated: 2027-02-26` and must render a "retiring 2027-02-26" note without
  being hidden.
* Summarize models: `gpt-5.6-sol`, `gpt-5.6-terra`, `gpt-5.6-luna` (default),
  `gpt-5.5`. All four declare `temperature: false`, so no temperature control
  should ever be offered for them; all four expose reasoning efforts.
* No video: the Videos API is removed on 2026-09-24
  (`capabilities.yaml:732`). An OpenAI row must not appear in any video picker.
* UI fields: as Deepgram. No Base URL box.

#### OpenRouter (`capabilities.yaml:749`)

* Key URL: `https://openrouter.ai/settings/keys`.
* One gateway base, `https://openrouter.ai/api/v1`, for all three interactions,
  every request carrying `HTTP-Referer: https://github.com/LuckyType/loreline`
  and `X-Title: Loreline`. Health path `/key` (because `/models` is public).
* Three disjoint catalogues, all marked `public: true` so they answer with no
  key: summarize `/models`, transcribe `/models?output_modalities=transcription`,
  video `/videos/models`.
* 15 summarize models (default `openai/gpt-5.6-luna`), 15 transcribe models
  (default `openai/whisper-large-v3-turbo`), 12 video models (default
  `google/veo-3.1-fast`).
* Every OpenRouter transcription model is **batch only** and declares
  `glossary: {supported: false}` and `inline_diarization: false`, including
  `deepgram/nova-3` and `x-ai/grok-stt-1.0`, whose native APIs do support both.
  The gateway exposes no such knob.
* Vendor-specific extras, the **Provider routing** box, shown for this kind and
  no other (`OpenRouterRouting`, `src/loreline/models.py:123`):
  * "Prefer": Balanced (OpenRouter default, sends nothing) / Cheapest (`price`)
    / Highest throughput (`throughput`) / Lowest latency (`latency`).
  * "No data collection" checkbox: sets `data_collection: deny`, excluding
    providers that may store or train on the transcript.
  * "Zero Data Retention only" checkbox: sets `zdr: true`, stricter, only
    endpoints under a ZDR agreement.
  * When either privacy switch is on, a note appears: a model with no eligible
    provider fails the request rather than falling back.
  * Any field left at its default is omitted from the request body entirely
    (`src/loreline/llm.py:60`).
* UI fields: Name, Language, Favorite models, API key, Provider routing. No
  Base URL box.

#### Google Gemini (`capabilities.yaml:1289`)

* Key URL: `https://aistudio.google.com/api-keys`.
* Three different auth spellings on three sibling surfaces: realtime Live API
  websocket with the key as a `?key=` **query parameter**; batch native REST
  `https://generativelanguage.googleapis.com/v1beta` with `x-goog-api-key`;
  summarize on the OpenAI-compatible shim
  `https://generativelanguage.googleapis.com/v1beta/openai` with `Bearer`.
  Catalogue `.../v1beta/models`, `picker: false`.
* Transcribe models: `gemini-3.5-transcribe` (default, batch only, inline
  diarization, `custom_vocabulary` up to 1000 terms, word timestamps) and
  `gemini-3.5-transcribe-live` (realtime only, no diarization, no word
  timestamps, `customVocabulary` up to 1000 terms).
* `gemini-3.5-transcribe` is the only model in the file that declares
  `conflicts`: `[glossary, inline_diarization]` and
  `[glossary, word_timestamps]`. This is what drives the greying out and the
  "turning it on drops them" warning in the capture and re-process panels.
* Summarize models: `gemini-3.8-flash`, `gemini-3.5-flash` (default),
  `gemini-3.5-flash-lite` (`mandatory: true`), `gemini-3.1-pro-preview`
  (`mandatory: true`). Mandatory means the effort dropdown must not offer
  "none".
* No video: no connector exists, entries stay commented out
  (`capabilities.yaml:1512`).
* UI fields: as Deepgram. No Base URL box.

#### xAI (Grok) (`capabilities.yaml:1559`)

* Key URL: `https://console.x.ai/`.
* One host, one key, four addresses, all `Bearer`: realtime
  `wss://api.x.ai/v1/stt` (no health frame, the server greets), batch
  `https://api.x.ai/v1`, summarize `https://api.x.ai/v1`, video
  `https://api.x.ai/v1`. Catalogue declared for **summarize only**.
* Transcribe: one model, `grok-stt-1.0` (default), realtime and batch with
  `prefer: realtime`, inline diarization, `keyterm` glossary capped at 100
  terms of 50 characters, word timestamps. The id is a label: the xAI STT API
  takes no `model` parameter and the connectors send none.
* Summarize: `grok-4.6` (default), `grok-4.5`, `grok-4.3`. None declares a
  `reasoning` block, so the reasoning-effort dropdown must stay hidden for all
  three.
* Video: `grok-imagine-video-1.5` (default), durations 1-15 s, resolutions
  480p/720p/1080p, seven aspect ratios, audio on, image input.
* UI fields: as Deepgram. No Base URL box.

#### Self-hosted, OpenAI compatible (`capabilities.yaml:1762`)

* `auth: optional`, `key_url: null`. Speaches, whisper.cpp, Ollama, LM Studio,
  vLLM.
* Every surface has `url: null` and `overridable: true`: the operator's base URL
  **is** the address. Catalogue is `{base_url}/models`.
* `models: []`. The catalogue is whatever the operator installed, discovered at
  runtime. Capabilities come from four glob patterns, first match wins:
  `*whisper*.en` (English only, `language_codes: [en]`), `*diarize*` (inline
  diarization true), `*whisper*`, and the catch-all `*` (transcribe and
  summarize, batch, no word timestamps, no diarization, glossary via `prompt`).
* UI fields: Name, **Base URL** (the only kind that shows it, placeholder
  `http://localhost:8000/v1`), Language, Favorite models, API key labelled
  "API key (optional)" and with **no** "Get an API key" link.
* No default model anywhere, deliberately, so `default_model()` returns None and
  the connector lets the server pick.

### 1.3 Field visibility rules, stated once

Computed in `frontend/src/routes/settings/providers/+page.svelte:113-129` and
rendered at lines 895-1040.

| Field | Shown when | Notes |
|---|---|---|
| Name | always | Blank is legal; the placeholder is the real fallback, `label`, or `label 2` when a row of that kind already exists. |
| Base URL | `requiresBaseUrl(spec)`, that is, any surface with `url: null` | True for `openai_compat` only. |
| Language | always | Free text, default `de`. |
| Favorite models + Load models | always | Load models is disabled while loading, and while the row has no key on file and none typed. |
| API key | `spec.auth !== 'none'` | True for all seven kinds today. Label is "API key", or "API key (optional)" for `auth: optional`, plus " - blank = keep current" while editing. |
| Get an API key link | `spec.key_url` is set | All kinds except `openai_compat`. |
| Provider routing block | `kind === 'openrouter'` | Never stored on any other kind: the page nulls `routing` on save. |
| Sample rate | never | `[api-only]`, `ProviderConfig.sample_rate` defaults to 16000. |
| Enabled flag | never | `[api-only]`, always saved as `true` from the UI. |

Capability badges in the providers table (`frontend/src/lib/types.ts:40`):
`Realtime` or `Batch` for a transcribe-capable kind, `Summarizing`, `Video`,
in that order. `Realtime` appears when **any** non-hidden model or any glob
pattern of the kind declares `realtime: true`. Expected badges:

* Deepgram: `Realtime`
* AssemblyAI: `Realtime`
* OpenAI: `Realtime`, `Summarizing`
* OpenRouter: `Batch`, `Summarizing`, `Video`
* Gemini: `Realtime`, `Summarizing`
* xAI: `Realtime`, `Summarizing`, `Video`
* Self-hosted: `Batch`, `Summarizing`

With `/api/capabilities` unavailable, `capabilityBadges` returns an empty list
and the Supports column is blank for every row. That is intended.

---

## 2. The "Only show compatible models" toggle

Location: Settings > Providers > **Defaults** card, bottom row, a Switch
labelled "Only show compatible models"
(`frontend/src/routes/settings/providers/+page.svelte:820-845`). Stored as
`ActionDefaults.strict_model_filtering`, default `true`
(`src/loreline/web/schemas.py:125`). It is saved by the **Save defaults**
button, not on flip.

### 2.1 The exact rule

`loreline.capabilities.filter_models` (`src/loreline/capabilities.py:400-433`),
applied server-side inside `POST /api/providers/models`:

1. `strict == False` -> return the list untouched.
2. `interaction is not TRANSCRIBE` -> return the list untouched.
3. `kind not in {openai, openai_compat}` -> return the list untouched.
4. Otherwise keep only models whose lowercased id contains one of
   `transcribe_name_markers`: `whisper`, `transcribe`, `parakeet`, `asr`,
   `stt`, `voxtral`, `nova`, `chirp` (`capabilities.yaml:57-66`).
5. If step 4 would empty the list, the **unfiltered** list is returned instead.
   An empty picker would strand an operator whose self-hosted models are
   perfectly good.

So, in practice, the toggle changes exactly two pickers: **the transcription
picker on an OpenAI row and the transcription picker on a self-hosted row.**
Every other list is either already scoped by the vendor (OpenRouter's
modality-specific endpoint) or curated by the YAML, and passes through
identically in both positions.

### 2.2 Where the model metadata comes from

`loreline.stt.catalog.list_models` (`src/loreline/stt/catalog.py:44-89`):

1. Ask `catalog_for(kind, interaction, base_url)` for a catalogue surface.
2. Fetch it live **only if** it resolves to a URL **and** the surface has
   `picker: true`.
3. If the probe is usable, apply `filter_models`, then stamp per model
   `realtime` and `inline_diarization` from the YAML.
4. Otherwise fall back to `curated_models(kind, interaction)`: the YAML's model
   list for that interaction, hidden entries excluded, and stamp the same two
   flags.

| Kind | transcribe list | summarize list | video list |
|---|---|---|---|
| deepgram | curated (`picker: false`) | n/a | n/a |
| assemblyai | curated (no catalogue) | n/a | n/a |
| openai | **live** `/v1/models`, filtered | **live** `/v1/models`, unfiltered | n/a |
| openrouter | **live**, modality-scoped | **live** `/models` | **live** `/videos/models` |
| gemini | curated (`picker: false`) | curated | n/a |
| xai | curated (no catalogue) | **live** `/v1/models` | curated in the YAML but never reached, see SUSPECTED ISSUES |
| openai_compat | **live** `{base_url}/models`, filtered | **live**, unfiltered | n/a |

A probe that fails for any reason (no key, vendor down, unreadable body, empty
list) silently falls back to the curated list. Failure is never surfaced as an
error in a picker.

Hidden models are dropped twice: server side in `curated_models`, and again in
the browser (`isHiddenModel` / `withoutHiddenRows`,
`frontend/src/lib/capabilities.svelte.ts:208-230`) so a live catalogue cannot
smuggle one in. They are dropped from the favourites checklist too, so a hidden
model cannot survive as a stored favourite.

### 2.3 Caching

`ModelCatalog` (`frontend/src/lib/modelCatalog.svelte.ts:32-93`) caches per
`providerId:interaction:refreshToken` for the life of the browser session. The
Settings page passes `refreshToken={draft.strict_model_filtering}`; the capture,
summarize and re-process pickers pass nothing, so their token is always `''`.
See SUSPECTED ISSUES.

---

## 3. Streaming versus batch

### 3.1 Which model is which

| Kind | Realtime only | Both, `prefer: realtime` | Both, `prefer: batch` | Batch only |
|---|---|---|---|---|
| deepgram | `flux-general-en`, `flux-general-multi` | `nova-3`, `nova-2` | | `whisper-large` (hidden) |
| assemblyai | `universal-streaming-english`, `universal-streaming-multilingual` | `universal-3-5-pro` | | `universal-2` (hidden) |
| openai | `gpt-live-transcribe`, `gpt-realtime-whisper` | | `gpt-transcribe`, `gpt-4o-transcribe`, `gpt-4o-mini-transcribe` | `gpt-4o-transcribe-diarize`, `whisper-1` |
| openrouter | | | | all 15 |
| gemini | `gemini-3.5-transcribe-live` | | | `gemini-3.5-transcribe` |
| xai | | `grok-stt-1.0` | | |
| openai_compat | | | | everything (all glob patterns are batch) |

`is_realtime_model` (`src/loreline/capabilities.py:305-353`) resolves the
transport: a curated model answers for itself, an unannotated one is guessed
from the `realtime_name_markers` (`live`, `realtime`) and otherwise inherits the
kind's default model's transport. `prefer_batch=True` is passed by re-processing
only, and it overrides `prefer` but never a model that has no batch transport at
all (so `gemini-3.5-transcribe-live` still streams a stored file).

The picker's right-hand hint shows `realtime` or `batch` per model
(`frontend/src/lib/modelInfo.ts:48-55`), alongside price and context length
where the vendor publishes them (OpenRouter only).

### 3.2 What happens when a batch model is chosen for a live capture

The live-capture gate is **per kind, not per model**
(`src/loreline/capabilities.py:379-392`). Expected behaviour:

* An OpenRouter row is filtered out of the Dashboard's primary and fallback
  provider dropdowns entirely (`providersFor('capture')`,
  `frontend/src/lib/actionSetup.svelte.ts:74-78,156-159`). There is no warning
  and no disabled entry: the row is simply absent.
* A stored transcription default naming an OpenRouter row is skipped by
  `preferredProvider`, so the capture card seeds on the first live-capable row
  instead. The same default is still honoured in Settings and in the
  re-process panel, which is deliberate: re-processing replays stored audio.
* If an API caller sends `POST /api/session/start` with an OpenRouter provider,
  the session manager rejects it with
  `"<name> (openrouter) cannot drive a live capture - it is available for
  post-session re-processing only"` (`src/loreline/session/manager.py:470-486`).
  `[api-only]`
* A batch-only **model** on a live-capable kind, for example
  `gpt-4o-transcribe-diarize`, `whisper-1` or `gemini-3.5-transcribe`, is fully
  allowed for a live capture. The batch connector runs once per VAD-detected
  utterance. Nothing in the UI warns about it, and nothing should: the transcript
  arrives per utterance instead of as growing interims.

---

## 4. Health checks and what the UI renders

### 4.1 Which surface is probed

`probe_target` (`src/loreline/health_probe.py:56-78`) asks exactly one surface
per row, since the key is the same on all of them:

1. If the kind summarizes, probe the **summarize** surface. That covers OpenAI,
   OpenRouter, Gemini, xAI and the self-hosted kind.
2. Else probe the **transcribe** surface, on the transport the kind's default
   model uses. Deepgram and AssemblyAI both land on their websocket.
3. Else the video surface. No kind reaches this today.

| Kind | Probe |
|---|---|
| deepgram | open `wss://api.deepgram.com/v1/listen`, send `{"type":"CloseStream"}`, read the reply |
| assemblyai | open `wss://streaming.assemblyai.com/v3/ws`, read the greeting |
| openai | `GET https://api.openai.com/v1/models` |
| openrouter | `GET https://openrouter.ai/api/v1/key` |
| gemini | `GET https://generativelanguage.googleapis.com/v1beta/openai/models` |
| xai | `GET https://api.x.ai/v1/models` |
| openai_compat | `GET {base_url}/models` |

Timeout is 10 s per probe, 5 s for a socket's first frame
(`src/loreline/health.py:55-58`). "Test all" fans out concurrently.

### 4.2 Grading

`classify_status` (`src/loreline/health.py:226-249`), checked in this order:

| Answer | Status | Notes |
|---|---|---|
| `< 300` | `healthy` | |
| `429` | `degraded` | Checked before the auth cases: a throttle proves the key works. |
| `>= 500` | `degraded` | Vendor is broken, config is not. |
| `401`, `403`, or a 4xx body naming a credential fault | `unauthorized` | The `auth_hint` scan pulls in Google's 400. |
| `404` (that was not an auth complaint) | `unreachable` | Read as a mistyped base URL. |
| anything else | `unknown` | |
| DNS failure, refused connection, TLS failure, timeout | `unreachable` | |
| an exception inside the probe itself | `unknown` | "the probe itself failed" |

No key stored on a kind with `auth: api_key` short-circuits before any network
call: `unauthorized`, detail `"no API key stored for this provider"`
(`src/loreline/health.py:122-135`). A self-hosted row with no key is still
probed, because its server may not check one. A row whose surface cannot be
located (a self-hosted row with no base URL) grades `unknown` with the message
naming what to fix.

Socket probes grade the same way: a rejected upgrade carries an HTTP status and
goes through the same table; an error frame after the upgrade that names a
credential is `unauthorized`; a socket that opens and stays quiet is `healthy`
(`src/loreline/stt/backends/_ws.py:146-247`).

### 4.3 What the Status column should render

`TEST_BADGE` (`frontend/src/routes/settings/providers/+page.svelte:167-181`):

| State | Badge text | Variant | Dot colour |
|---|---|---|---|
| never tested | `not tested` | outline | grey, tooltip "never tested" |
| in flight | `testing…` | secondary | amber |
| `healthy` | `healthy` | secondary | emerald |
| `degraded` | `degraded` | secondary | amber |
| `unauthorized` | `auth failed` | destructive | red |
| `unreachable` | `unreachable` | destructive | red |
| `unknown` | `unknown` | outline | grey |

The vendor's own sentence rides in the badge's `title` tooltip. If Loreline's
own API fails to answer the Test call, the badge is `unknown` with the client
error as the tooltip, never `unreachable`: that would blame the wrong endpoint.

---

## 5. Validation rules

### 5.1 Provider create and update

`ProviderCreate` (`src/loreline/web/schemas.py:24-40`) requires only `name` and
`kind`. Everything else has a default. There is **no** validation of the API
key format, no trimming, no URL validation on `base_url`, and no check that a
self-hosted row has a base URL at all.

Client-side (`frontend/src/routes/settings/providers/+page.svelte`):

* **Save is disabled only when `effectiveName` is empty**, which happens only
  when no kind is selected. A blank Name is legal and resolves to the kind label,
  deduplicated with a numeric suffix against the other rows.
* `keyMissing` is `spec.auth === 'api_key'` and no key typed this session and no
  key on file. It drives an inline amber warning under the field, disables the
  "Load models" button with the tooltip "Add an API key first", and puts a
  confirm dialog in front of Save: title "No API key", body "No key saved -
  you won't be able to test or transcribe with this provider until you add one.",
  buttons "Save anyway" and "Go back".
* Editing an existing row with the key box left blank keeps the stored key
  (`if body.api_key:` in `src/loreline/web/routes/providers.py:89,109`).
* Deleting a row also deletes its secret, after a confirm.
* The key is stored write-only and only ever read back as a mask: 4 leading and
  4 trailing characters for a key longer than 8, `xx…yy` for 5 to 8, all bullets
  for 4 or fewer (`src/loreline/secrets.py:81-96`).

### 5.2 Bad base URL

`Surface.resolve` (`src/loreline/capability_config.py:609-618`) applies a row's
base URL only to a surface marked `overridable` **and only when the schemes
match**: a `ws://` or `wss://` override reaches a socket surface and is dropped
by an HTTP one, and the reverse. A surface whose URL is `null` or contains
`{base_url}` and gets no override resolves to None, and the caller raises
`"<label> needs a base URL to <interaction>"`.

Reachable consequences:

* Self-hosted row saved with an empty Base URL: saves fine, then Test reports
  `unknown` with the detail "Self-hosted (OpenAI compatible) needs a base URL to
  summarize". Model loading returns an empty list. A session start fails.
* Self-hosted row with a base URL pointing at something that is not an
  OpenAI-compatible server: Test is `unreachable` ("no API at this URL" on a
  404, "could not connect: ..." on a transport failure).
* Redirects are followed, so a server that bounces http to https or adds a
  trailing slash still grades `healthy`.

### 5.3 Action-level validation

* `POST /api/session/start` requires `model` with `min_length=1`, and requires
  `fallback_model` as soon as `fallback_provider` is set
  (`src/loreline/web/schemas.py:55-89`). The Start button is disabled until the
  primary model is chosen, and the fallback model missing blocks Start too.
* Session start also rejects: an unknown or disabled provider, a provider whose
  kind cannot drive a live capture, and `diarization.mode = inline` on a model
  whose `inline_diarization` is false
  (`src/loreline/session/manager.py:470-500`).
* `POST /api/session/{id}/summarize` requires `model` with `min_length=1`.
* `POST /api/reprocess` requires `model` for `operation: transcribe` and ignores
  it for `operation: diarize`.
* Remote diarization requires an endpoint; the capture card blocks Start with an
  inline message when the mode is `remote` and the box is empty, and probes the
  typed endpoint after a 500 ms debounce.
* Glossary: the checkbox is disabled with a reason when the chosen model
  declares `glossary.supported: false`, and force-cleared when the model changes
  to such a model. On `gemini-3.5-transcribe` the checkbox stays enabled but
  carries the warning that turning it on drops speaker labels and word
  timestamps, and the "Inline (from STT)" option greys out while it is on.
* Reasoning effort: the dropdown appears only when the model's `efforts` list is
  non-empty, and a `mandatory: true` model never offers `none`. A saved effort
  that the newly chosen model does not accept is cleared automatically.

---

## UI TEST STEPS

Preconditions: a deployment with at least an OpenAI key, an OpenRouter key and a
reachable self-hosted OpenAI-compatible server. Steps that need a key you do not
have can still be run for their negative half.

### A. Vendor list, wizard and field visibility

1. Open Settings > Providers. Click the `+` button. Confirm Step 1 offers
   exactly two buttons, "Cloud provider" and "Self-hosted", with the note
   "Cloud needs an API key; self-hosted points at a URL on your network."
2. Click "Cloud provider". Confirm Step 2 lists exactly six rows in this order:
   Deepgram, AssemblyAI, OpenAI, Google Gemini, OpenRouter, xAI (Grok), each
   with its one-line note. Confirm "Self-hosted (OpenAI compatible)" is **not**
   in this list.
3. Click "Back", then "Self-hosted". Confirm exactly one row,
   "Self-hosted (OpenAI compatible)".
4. Pick "Self-hosted (OpenAI compatible)". Confirm the form shows: Name, **Base
   URL** with placeholder `http://localhost:8000/v1`, Language, Favorite models,
   and an API key field labelled "API key (optional)" with **no** "Get an API
   key" link. Confirm there is no Provider routing box.
5. Cancel. Add a Deepgram row instead. Confirm the form shows Name, Language,
   Favorite models, API key (labelled plain "API key") with a "Get an API key ↗"
   link pointing at `console.deepgram.com`, and **no Base URL field**. Repeat for
   AssemblyAI, OpenAI, Gemini and xAI: none of them may show a Base URL field.
6. Add an OpenRouter row. Confirm the form additionally shows the "Provider
   routing" box with a "Prefer" dropdown offering exactly Balanced (OpenRouter
   default) / Cheapest / Highest throughput / Lowest latency, plus the two
   checkboxes "No data collection" and "Zero Data Retention only". Tick either
   checkbox and confirm the note appears: a model with no provider meeting these
   rules fails the request rather than falling back.
7. Save the OpenRouter row, reopen it for editing, and confirm the routing
   selections round-tripped.
8. Add a Deepgram row, tick nothing in routing (there is no box), save. Confirm
   via the row's edit dialog that no routing is stored. `[api-only]` to confirm
   the stored JSON has `routing: null`.
9. With every kind saved once, check the Supports column badges against the list
   in section 1.3: OpenRouter must read `Batch Summarizing Video`, Deepgram
   `Realtime`, self-hosted `Batch Summarizing`.
10. Leave the Name box blank when adding a second Deepgram row. Confirm the
    placeholder reads "Deepgram 2" and that saving produces a row named
    "Deepgram 2", not a second "Deepgram".

### B. The compatible-models toggle

11. In Settings > Providers > Defaults, confirm the "Only show compatible
    models" switch is **on** for a fresh install.
12. Set the Transcription provider to the OpenAI row. Open the Model dropdown.
    Confirm every listed id contains one of `whisper`, `transcribe`, `parakeet`,
    `asr`, `stt`, `voxtral`, `nova`, `chirp`. Specifically confirm no
    `dall-e-*`, `tts-*`, `text-embedding-*` or `gpt-image-*` entry.
13. Turn the switch **off** and press **Save defaults**. Reload the page. Reopen
    the same dropdown and confirm the full OpenAI `/models` catalogue now
    appears, image and speech models included.
14. Negative: with the switch off, pick an incompatible model, for example
    `dall-e-3`, as the transcription default and press Save defaults. Confirm it
    saves without complaint (this is intentional: the toggle is an escape hatch,
    not a gate). Then start a session with it and confirm the failure surfaces
    as a transcription error naming the vendor's own message rather than a
    silent empty transcript.
15. Turn the switch back on and Save defaults. Repeat step 12 against the
    self-hosted row: confirm its transcription list narrows to models whose
    names match the markers, and that if none of its models match, the whole
    unfiltered list is shown rather than an empty dropdown (point the row at a
    server whose models are named, say, `my-model-v1`).
16. With the switch on, open the OpenRouter transcription picker and confirm it
    lists only transcription models (this list is scoped by the vendor and must
    look identical with the switch off). Flip the switch, Save defaults, reload,
    and confirm the same list.
17. With the switch on, open the **summarize** picker on the OpenAI row. Note
    what is listed. See SUSPECTED ISSUES item 1: today this list is unfiltered
    in both switch positions.
18. Confirm the Deepgram transcription picker lists exactly `nova-3`, `nova-2`,
    `flux-general-en`, `flux-general-multi`, and never `whisper-large` or any
    `nova-2-meeting`-style variant, in either switch position.
19. Confirm the AssemblyAI picker lists exactly `universal-3-5-pro`,
    `universal-streaming-english`, `universal-streaming-multilingual`, and never
    `universal-2`.
20. Confirm the Gemini transcription picker lists exactly
    `gemini-3.5-transcribe` and `gemini-3.5-transcribe-live`, and the Gemini
    summarize picker exactly the four `gemini-3.x` chat models with no
    transcription model among them.

### C. Favourites and the model picker

21. Edit a provider row with a valid key. Press "Load models". Confirm the list
    populates and that for an OpenRouter row it merges all three interactions
    (transcription, chat and video ids in one checklist).
22. Confirm the filter box narrows the checklist by substring.
23. Tick two models as favourites, save, then open the Dashboard model picker for
    that row. Confirm the favourites appear at the top of the dropdown, grouped
    ahead of the rest of the catalogue.
24. Confirm no hidden model is ever offered: search the favourites checklist and
    every picker for `whisper-large` (Deepgram) and `universal-2` (AssemblyAI).
    Both must be absent everywhere.
25. Pick `whisper-1` on an OpenAI row and confirm the picker shows a
    "retiring 2027-02-26" note in the row and again under the closed trigger,
    while the model stays fully selectable.
26. On an OpenRouter row's summarize picker, confirm each row carries a price
    hint of the shape `$0.15 / $0.60 · 1M`, and that a model with tiered pricing
    shows the ladder in its tooltip. Confirm the Deepgram picker shows no price
    hints at all.
27. Confirm a transcription picker row shows `realtime` or `batch` in its hint,
    matching the table in section 3.1.

### D. Health, test and negative credential cases

28. Add a cloud provider and leave the API key blank. Confirm the amber inline
    warning appears under the field, that "Load models" is disabled with the
    tooltip "Add an API key first", and that pressing Save raises the "No API
    key" confirm dialog. Press "Go back" and confirm nothing was saved.
29. Repeat and press "Save anyway". Confirm the row saves, the API key column
    reads `- none -`, and pressing **Test** returns `auth failed` with the
    tooltip "no API key stored for this provider" essentially instantly, with no
    network round trip.
30. Add a self-hosted row with no key. Confirm there is no warning and no confirm
    dialog (its auth is optional), and that Test actually reaches the server.
31. Paste a deliberately wrong key into a cloud row and press Test. Confirm the
    badge is `auth failed` in red and that the tooltip carries the vendor's own
    sentence, for example "Incorrect API key provided" or "API key not valid.
    Please pass a valid API key.".
32. Self-hosted row, Base URL left empty: save, press Test, confirm the badge is
    `unknown` (grey) with the tooltip "Self-hosted (OpenAI compatible) needs a
    base URL to summarize".
33. Self-hosted row with Base URL `http://127.0.0.1:9/v1` (nothing listening):
    Test must read `unreachable` in red with a "could not connect" tooltip.
34. Self-hosted row pointed at a real HTTP server that serves no `/models`
    (for example a plain nginx root): Test must read `unreachable` with "no API
    at this URL", not `auth failed`.
35. Press **Test all** with several rows configured. Confirm each badge goes
    through `testing…` and settles independently, and that one slow or dead
    provider does not delay the others past about ten seconds.
36. Confirm an untested row reads `not tested` with the tooltip "never tested",
    and that this is the state after a page reload (results are not persisted).
37. `[api-only]` `degraded`: hard to force from the UI. Exhaust a key's quota or
    stub a 429/5xx to confirm the amber `degraded` badge, and confirm a 429 is
    graded as degraded rather than as an auth failure.

### E. Live capture versus batch

38. With an OpenRouter row saved, open the Dashboard. Confirm the primary
    provider dropdown does **not** list it, and neither does the fallback
    dropdown.
39. In Settings > Providers > Defaults, set the Transcription provider to the
    OpenRouter row and Save defaults. Return to the Dashboard and confirm the
    primary picker seeds on some other row rather than showing an empty or
    broken selection.
40. Open a finished session's re-process panel. Confirm the OpenRouter row **is**
    offered there, and that a re-transcription job with it runs to completion.
41. `[api-only]` `POST /api/session/start` naming the OpenRouter provider must be
    rejected with "cannot drive a live capture - it is available for
    post-session re-processing only".
42. On the Dashboard with an OpenAI row selected, choose `gpt-4o-transcribe-diarize`
    (batch only) and start a short session. Confirm it is accepted and that
    transcript lines appear one complete utterance at a time, with no growing
    interim text.
43. With the same OpenAI row, choose `gpt-live-transcribe` and start a session.
    Confirm interim text grows in place and is replaced by the settled line
    rather than appended below it.
44. Select `gemini-3.5-transcribe` (batch only) on a Gemini row for a live
    capture. Confirm it is offered and works, since the kind, not the model,
    gates live capture.

### F. Model-dependent controls

45. On the Dashboard with `nova-3` selected, confirm the Diarization dropdown
    offers "Inline (from STT)". Switch the model to `gpt-transcribe` on an
    OpenAI row and confirm the inline option disappears and any previously
    selected "inline" silently falls back to "None".
46. Select `gpt-4o-transcribe-diarize` and confirm "Inline (from STT)" is offered
    again.
47. Select `gemini-3.5-transcribe` and tick "Use glossary". Confirm a warning
    appears saying the model rejects a glossary alongside speaker labels and
    word timestamps, so turning it on drops them, including the note about
    per-utterance speaker placement. Confirm the "Inline (from STT)" option is
    greyed with the reason while the glossary is on, and becomes selectable
    again when it is off.
48. Select an OpenRouter transcription model in the re-process panel. Confirm the
    "Use glossary" checkbox is **disabled** with the reason "This model has no
    way to receive a glossary, so the terms would be ignored." Confirm switching
    to such a model with the glossary already ticked clears the tick.
49. Open the Summarize dialog on a Gemini row and pick `gemini-3.5-flash-lite`.
    Confirm the Reasoning effort dropdown offers `minimal`, `low`, `medium`,
    `high` and **not** `none`. Pick `gemini-3.5-flash` and confirm `none`
    reappears.
50. Pick an xAI summarize model (`grok-4.6`). Confirm no Reasoning effort
    dropdown is rendered at all.
51. Pick `minimax/minimax-m3` on an OpenRouter row. Confirm no effort dropdown
    appears (the model reasons but exposes no levels).
52. Set a reasoning effort of `xhigh` against an OpenAI model, save defaults,
    then switch the summarize model to a Gemini one. Confirm the stored effort
    is cleared rather than carried over to a model that would reject it.

### G. Video

53. Open a session with a stored summary and click "Generate video". With only
    an OpenRouter row configured, confirm the provider dropdown lists it and the
    model dropdown fills from the live `/videos/models` catalogue.
54. Pick `minimax/hailuo-3-max` and confirm the resolution dropdown offers only
    `768p` and `480p`, and that the audio checkbox is absent (the model declares
    `audio: false`).
55. Pick `google/veo-3.1` and confirm durations 4, 6 and 8 only, resolutions
    720p/1080p/4K, aspect ratios 16:9 and 9:16, and that the audio checkbox is
    offered.
56. Switch from a model offering 15 s to one that does not and confirm the
    duration resets to the new model's first supported value rather than
    carrying an unsupported one over.
57. Add an xAI row and open the same dialog. Confirm the xAI row appears in the
    provider dropdown, then observe what the model dropdown does. Expected per
    the YAML: `grok-imagine-video-1.5`. See SUSPECTED ISSUES item 2.
58. Confirm no OpenAI and no Gemini row appears in the video provider dropdown.

### H. Persistence and edge cases

59. Edit a saved row, leave the API key box blank (placeholder "•••• unchanged"),
    change only the Name, and save. Confirm the stored key survives: the API key
    column still shows the mask and Test still passes.
60. Confirm the API key column shows a mask of the form `sk-a…wxyz` and never
    the full key, and that the key is never echoed into any other field.
61. Delete a provider. Confirm the confirm dialog says it also removes the
    stored key, and that after deletion any default pointing at it is skipped
    rather than leaving a dangling selection.
62. `[api-only]` `enabled: false` on a provider row: no UI control exists.
    Set it through the API and confirm session start rejects the row.
63. `[api-only]` `sample_rate`: no UI control exists; the wizard always sends
    16000.
64. `[api-only]` a per-row `base_url` on any cloud kind, for example
    `https://api.eu.assemblyai.com` on an AssemblyAI row: no UI control exists.
    Set it through `PUT /api/providers/{id}` and confirm the Endpoint column in
    the table then shows it instead of "default".
65. Stop the backend and reload the Settings page. Confirm the capability banner
    appears ("Capability data could not be loaded, so every provider and model
    option is being offered. Some of them may not work.") with a working Retry,
    and that the provider table shows its own error banner with its own Retry
    rather than an empty "No providers yet".
66. With capabilities unavailable, confirm the Supports column is blank for every
    row and that no picker hides anything: the fail-soft rule is that unknown
    means allowed.

---

## SUSPECTED ISSUES

1. **The compatible-models filter never applies to summarize pickers**, so an
   OpenAI or self-hosted row's summarize list is the raw `/models` dump,
   including `whisper-1`, `tts-1`, `dall-e-3` and embedding models. The switch
   copy claims it "hides models that can't do the job you're picking for".
   `src/loreline/capabilities.py:428` (`if interaction is not
   Interaction.TRANSCRIBE: return models`).

2. **xAI's video model is unreachable from the UI.** The YAML declares the
   `video` interaction and curates `grok-imagine-video-1.5`, and the comment at
   `src/loreline/capabilities.yaml:1598` says "the transcription and video
   pickers read the curated lists below", but both video pickers fetch only the
   live catalogue, which xAI does not publish. `list_video_models` returns an
   empty list with no curated fallback, so the Generate video dialog shows "No
   video models available - check the provider's API key" for a perfectly good
   xAI row, and the Settings video picker is empty.
   `src/loreline/video/client.py:128-158`,
   `frontend/src/lib/modelCatalog.svelte.ts:102-117`,
   `src/loreline/capabilities.yaml:1601`.

3. **The strict-filtering toggle is read from the saved defaults, but the
   Settings pickers refresh on the unsaved draft.** Flipping the switch changes
   `refreshToken`, which busts the browser cache and refetches, yet
   `POST /api/providers/models` reads `load_action_defaults`, that is, the last
   *saved* value. So the list visibly reloads and comes back identical until
   "Save defaults" is pressed.
   `frontend/src/routes/settings/providers/+page.svelte:697` versus
   `src/loreline/web/routes/providers.py:177-183`.

4. **The capture, summarize and re-process pickers never invalidate their model
   cache when the toggle changes.** `ModelPicker`'s `refreshToken` defaults to
   `''` and only the Settings page passes one, so a list fetched earlier in the
   browser session survives a toggle change until a full page reload.
   `frontend/src/lib/ModelPicker.svelte:15`,
   `frontend/src/lib/CaptureControls.svelte:521`,
   `frontend/src/lib/SummarizeDialog.svelte:125`,
   `frontend/src/lib/ReprocessPanel.svelte:113`.

5. **No cloud row can be given a base URL from the UI**, although six of the
   seven kinds mark surfaces `overridable: true` and the YAML explicitly
   documents the EU AssemblyAI base URL as a per-row setting. The Base URL box
   is shown only when a surface has `url: null`, which is true for
   `openai_compat` alone. An EU AssemblyAI account, a proxied OpenRouter, or a
   regional Gemini endpoint is therefore `[api-only]`.
   `frontend/src/routes/settings/providers/+page.svelte:120,127,907`,
   `frontend/src/lib/capabilities.svelte.ts:101-105`,
   `src/loreline/capabilities.yaml:328`.

6. **A whitespace-only API key is stored as a real secret.** `hasUsableKey`
   trims before deciding whether to warn, but the save body sends
   `form.api_key || null` untrimmed, so typing spaces produces the "No API key"
   confirm dialog and then, on "Save anyway", writes a key of spaces. The row
   afterwards reports `secret_set: true` with a bullet mask, so the warning
   never returns, and every request fails with a vendor auth error instead.
   `frontend/src/routes/settings/providers/+page.svelte:456` and `:401`.

7. **`base_url` is unvalidated end to end.** It is a bare `str | None` with no
   scheme, host or shape check, and a self-hosted row can be saved with none at
   all. The only feedback is a Test verdict of `unknown` whose message names the
   *summarize* interaction even on a transcription-only server, because
   `probe_target` prefers the summarize surface.
   `src/loreline/web/schemas.py:29`, `src/loreline/health_probe.py:72-78`,
   `src/loreline/capabilities.py:133`.

8. **The Test button never exercises a transcription surface for any kind that
   also summarizes.** OpenAI, OpenRouter, Gemini, xAI and the self-hosted kind
   are all graded on their chat endpoint, so a Gemini row whose Live socket or
   native REST base is wrong still tests `healthy`. Deliberate (one probe per
   row, one credential), but a tester should not read `healthy` as "transcription
   works". `src/loreline/health_probe.py:56-78`.

9. **"Load models" is disabled for a keyless OpenRouter row even though all
   three of its catalogues are public** (`public: true`, verified answering
   without an `Authorization` header). The gate is `keyMissing`, which knows
   nothing about catalogue publicity.
   `frontend/src/routes/settings/providers/+page.svelte:259-263`,
   `src/loreline/capabilities.yaml:774-777`.

10. **OpenRouter's summarize catalogue is fetched unfiltered**, from the bare
    `/models`, which is the whole gateway catalogue rather than a chat-only
    list. The parser knows how to tell a transcription row apart
    (`_is_transcription_model`) but uses that only to suppress a price, so
    transcription-only ids may appear in the summarize picker. Worth confirming
    against the live gateway before treating it as a defect.
    `src/loreline/catalog.py:497-500,546`, `src/loreline/capabilities.yaml:775`.

11. **The `Realtime` badge is per kind, not per configuration**, so a Gemini row
    reads `Realtime` although its default and its only diarizing, word-timestamped
    transcription model is batch only. Cosmetic, but it is the only capability
    signal in the provider table.
    `frontend/src/lib/capabilities.svelte.ts:197-202`.

12. **`flux-general-en` and `flux-general-multi` silently offer no inline
    diarization** because the YAML omits the field (defaulting to `false`) where
    Deepgram's own docs contradict themselves. That is a defensible choice, but
    it is invisible in the UI: the option simply is not there, with no note
    saying why. `src/loreline/capabilities.yaml:173-186`.
