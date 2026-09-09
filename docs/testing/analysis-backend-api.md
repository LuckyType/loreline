# Backend API surface: behaviour level map

Input to a manual black box UI test run against a live deployment. Built by
reading `src/loreline/web/` in full plus the domain modules the routes delegate
to. Nothing here is inferred from the OpenAPI document alone: every error branch
below was traced to the line that raises it.

Target under test: the deployed app (LAN, plain HTTP) at `http://10.10.50.55/`.

Counts: **56 HTTP routes + 4 WebSocket routes = 60 endpoints.**

Legend, matching `docs/testing/TEST-PLAN.md`:
`[neg]` negative or error path, `[api-only]` not reachable from the web UI,
`[obs]` observation only.

---

## 0. Cross cutting rules

### 0.1 Router order and the SPA fallback

`src/loreline/web/app.py:327-341` includes routers in this order: system,
capabilities, auth, audio, providers, glossary, sessions, reprocess, video,
transcript_ws, logs_ws, and then mounts the built SvelteKit SPA at `/`.

`src/loreline/web/spa.py:22-40`: a path under `api/` or `ws/` that reaches the
static mount returns a real 404. Everything else falls back to `index.html`, so
deep links like `/sessions/<id>` survive a refresh.

Consequence for testing: a mistyped API path returns a JSON-less 404, a mistyped
app path returns the SPA shell with HTTP 200.

### 0.2 Auth model

`src/loreline/web/auth.py`.

- A single shared password, `LORELINE_AUTH_PASSWORD`. When it is **empty, auth
  is disabled entirely** and `require_auth` is a no-op (`auth.py:236-237`). Every
  protected route then answers as if logged in.
- With a password set, `POST /api/auth/login` issues an HS256 JWT into the
  HttpOnly cookie `loreline_token` (`auth.py:36`, `routes/auth.py:36-48`).
- TTL: `settings.jwt_ttl_seconds`, default 12 hours (`settings.py:104`). The
  cookie's `max_age` is the same value, so the browser drops it at the same
  moment the token expires.
- The token carries a 16 hex char fingerprint of the current password
  (`auth.py:127-148`). Changing `LORELINE_AUTH_PASSWORD` and restarting
  invalidates every outstanding cookie immediately.
- The JWT signing secret is auto generated and persisted into the secret store
  on first boot if unset or left at the shipped default (`auth.py:41-58`).
- `Secure` is set on the cookie only when the browser's own connection was TLS
  (`auth.py:92-124`). `X-Forwarded-Proto` is believed only from a peer inside
  `LORELINE_TRUSTED_PROXIES`.

**Missing or expired cookie on an HTTP route:** `401` with body
`{"detail":"authentication required"}` (`auth.py:239`).

**Frontend handling of that 401:** `frontend/src/lib/api.ts:58-65`. Any 401 on
any path other than `/api/auth/login` clears the `authed` store and calls
`goto('/login')`. So an expired cookie bounces the whole app to the login form
on the next fetch, which the layout's 5 second health poll guarantees happens
quickly.

**Missing or expired cookie on a WebSocket route:** all four WS handlers check
the cookie themselves and call `ws.close(code=1008)` **before** `accept()`
(`routes/transcript_ws.py:42-47`, `routes/logs_ws.py:26-31`,
`routes/audio.py:69-74`, `routes/audio.py:118-123`). See SUSPECTED ISSUES 2:
this reaches the browser as a failed handshake, not as close code 1008.

**Unauthenticated on purpose:** `GET /api/capabilities`, `GET /api/system/healthz`,
`POST /api/auth/login`, `POST /api/auth/logout`.

### 0.3 Error body shapes

- Raised `HTTPException`: `{"detail": "<string>"}`.
- Pydantic or query/path validation failure: `422` with
  `{"detail":[{"type":..., "loc":[...], "msg":..., "input":...}]}`. The frontend's
  `request()` reads `body.detail` and, when it is a list rather than a string,
  falls through to `res.statusText` (`api.ts:66-75`), so a 422 surfaces in the UI
  as the bare word "Unprocessable Entity" or "Unprocessable Content".
- No global exception handler is registered (`app.py:319-341`), so an unhandled
  exception is a bare `500 Internal Server Error` with no JSON body.

### 0.4 Global side effects at startup (context for "why is state like that")

`app.py:251-310` lifespan: connects the DB, marks interrupted sessions,
reconciles orphaned reprocess and video jobs to `error`, prunes log directories
for sessions that no longer exist, kicks off a background WAV index recovery
sweep, and kicks off a background stale-favourite-model check.

---

## 1. Auth routes (`src/loreline/web/routes/auth.py`)

### `POST /api/auth/login`
- **UI action:** `/login`, the Sign in button (or Enter in the password field).
  `frontend/src/routes/login/+page.svelte`.
- **Auth:** none required.
- **Body:** `LoginRequest` (`schemas.py:18-21`), `{"password": str}`. `password`
  is required, no length limit, no trimming.
- **200:** `{"ok": true}` plus `Set-Cookie: loreline_token=...; HttpOnly; SameSite=Lax`
  (`+ Secure` only when the request reached the app over TLS).
- **401 `invalid password`** when `verify_password` fails (`routes/auth.py:31-33`).
  Note `verify_password` returns False unconditionally when no password is
  configured (`auth.py:161-166`), so with auth disabled every login attempt is a
  401 while the API itself is wide open.
- **429 `too many attempts, try again shortly`** after 5 failures from the same
  client IP, for 30 seconds (`auth.py:185-204`, `routes/auth.py:27-30`). The
  limiter is checked **before** the password, so the 6th attempt is refused even
  with the correct password. A successful login clears the counter.
- **422** when `password` is absent or not a string.
- **Side effects:** mutates the in-memory `LoginRateLimiter` dict; writes no disk
  state. Not persisted across restarts.

### `POST /api/auth/logout`
- **UI action:** the Logout button in the header (`routes/+layout.svelte`).
- **Auth:** none required. Callable while already logged out; still returns 200.
- **Body:** none.
- **200:** `{"ok": true}` plus a `Set-Cookie` expiring `loreline_token` with the
  same attributes it was set with (`routes/auth.py:57-62`).
- **No error branches.**

---

## 2. Capabilities (`src/loreline/web/routes/capabilities.py`)

### `GET /api/capabilities`
- **UI action:** loaded once per page load by `loadCapabilities()` in
  `frontend/src/lib/capabilities.svelte.ts`, called from the root layout. The
  amber "capability config failed to load" banner with a Retry link calls it
  again.
- **Auth:** **none**, deliberately (`capabilities.py:10-13`): the login screen's
  first-run provider wizard needs it, and the payload is the shipped
  `capabilities.yaml`, identical on every install, with no keys, no operator
  base URLs and no session data.
- **Params:** none.
- **200:** `CapabilityConfig` (`capability_config.py:861-888`): `version`,
  `providers` keyed by the seven provider kinds, `transcribe_name_markers`,
  `realtime_name_markers`.
- **No error branches.** A malformed `capabilities.yaml` would have failed the
  process at import (`capability_config.py:891-903`), not this request.

---

## 3. System routes (`src/loreline/web/routes/system.py`)

### `GET /api/system/healthz`
- **UI action:** polled every 5 seconds by the root layout health dot; also
  called by `CaptureControls.svelte` and `LiveTranscriptPane.svelte` to learn
  the active session id.
- **Auth:** **none** (`system.py:1-5` says so explicitly, for external pollers).
- **Params:** none.
- **200:** `HealthResponse` (`system.py:69-114`): `status` (`ok` | `degraded` |
  `error`, from `monitoring/health.py:25-31`), `version`, `uptime_seconds`,
  `capture_status` (`idle` | `capturing` | `stopping` | `completed` | `error`),
  `active_session_id`, `disk_free_bytes`, `disk_total_bytes`, `alerts_enabled`,
  `diarizer_endpoint`, `diarizer_reachable`, `diarizer_status`, `diarizer_detail`,
  `stt_degraded_since`, `stt_error`, `captured_seconds`, `capture_last_frame_age`.
- **`status` rules:** `error` when capture status is `error`; else `degraded`
  when free disk is below `LORELINE_DISK_ALERT_THRESHOLD_MB`; else `ok`.
- **Side effect:** probes the **saved** diarizer endpoint over HTTP when
  `defaults.diar_endpoint` is set, memoised for 20 seconds in a module level
  global (`system.py:52-66`). With no endpoint saved, all four `diarizer_*`
  fields are null.
- **No error branches.** A dead diarizer yields `diarizer_status: "unreachable"`,
  not an HTTP error (`diarization/remote.py:180-209` never raises).

### `GET /api/system/diarizer/probe?endpoint=<url>`
- **UI action:** Dashboard capture card, advanced panel: typing in the
  Diarization endpoint field, debounced 500 ms (`CaptureControls.svelte:175`).
- **Auth:** required.
- **Query:** `endpoint`, **required string, no format validation at all**.
- **200:** `DiarizerProbeResponse` (`system.py:158-175`): `reachable` (bool),
  `status` (`healthy` | `degraded` | `unauthorized` | `unreachable` | `unknown`,
  see `health.py:64-100`), `detail`.
- **422** when `endpoint` is omitted.
- **401** without a cookie.
- **Side effect:** the server issues an outbound `GET <endpoint>/healthz` on
  every call, uncached, 10 second timeout. A healthy service that does not
  advertise session speaker memory is graded `degraded` rather than `healthy`
  (`diarization/remote.py:205-209`).
- **No 4xx/5xx for a bad endpoint:** garbage such as `not-a-url` still returns
  200 with `status: "unreachable"`.

### `GET /api/system/revision`
- **UI action:** Settings > Client, on mount, rendered next to Update now.
- **Auth:** required.
- **200:** `{"commit": "<40 hex>"}` or `{"commit": null}` when `git rev-parse
  HEAD` fails (`updater/updater.py:141-144`). No error branch.

### `POST /api/system/update`
- **UI action:** Settings > Client, the **Update now** button (label becomes
  `Updating…` and disables while busy).
- **Auth:** required.
- **Body:** none.
- **200:** `UpdateResult` (`updater/updater.py:108-115`): `ok`, `previous_commit`,
  `new_commit`, `returncode`, `output`.
- **Failure is still HTTP 200 with `ok: false`.** The UI reads `ok` and shows
  "Update failed (see output)." plus an output pane when `output` is multi line
  (`settings/client/+page.svelte:56,156`).
- **Side effects, source install:** runs `bash deploy/update-source.sh` in the
  app dir, which pulls, syncs and schedules a detached service restart.
- **Side effects, Docker install:** POSTs to the sibling updater container with
  a bearer token (`updater.py:170-232`). Unreachable or unconfigured yields the
  "update from the host" message with `ok: false`. A 401 from the updater yields
  the rejected message; a 409 yields the busy message.
- **Note:** the request can outlive the process it restarts. A timeout or
  dropped connection in the browser is an expected outcome here, not a bug.

### `POST /api/system/rollback` `[api-only]`
- **UI action:** none. `api.rollback` exists in `frontend/src/lib/api.ts:97-101`
  but has **no call site anywhere in `frontend/src`**.
- **Auth:** required.
- **Body:** `RollbackRequest` (`schemas.py:268-275`): `commit`, constrained to
  `^[0-9a-fA-F]{7,40}$` so it cannot be read as a git option.
- **200:** `UpdateResult`, same shape as update. Failure is 200 with `ok: false`.
- **422** for a commit shorter than 7 chars, longer than 40, non hex, or one
  starting with `-` (this is the injection guard).
- **Side effects:** `git reset --hard <commit>`, `uv sync --frozen`, then a
  detached `systemd-run` restart. Inside Docker it changes nothing and returns
  the container message.

### `GET /api/system/autostart`
- **UI action:** Settings > Client, on mount, drives the Autostart switch.
- **Auth:** required.
- **200:** `{"enabled": bool}`.
- **503** with the message
  `systemd unit 'loreline' is not available in a Docker deployment`
  (`updater/autostart.py:49-52`, surfaced at `system.py:225-226`). **This is the
  expected answer on the Docker deployment**, so on the box under test the
  Autostart row should render its unavailable state, not a toggle that works.

### `PUT /api/system/autostart`
- **UI action:** Settings > Client, flipping the Autostart switch.
- **Auth:** required.
- **Body:** `AutostartUpdate` (`schemas.py:262-265`): `{"enabled": bool}`, required.
- **200:** `{"enabled": bool}`, re-read from `systemctl is-enabled` afterwards.
- **503** in a Docker deployment, same message as GET.
- **409** `failed to enable systemd unit 'loreline': <combined output>` when the
  unit exists but `sudo systemctl enable|disable` returned non zero, typically a
  missing sudoers rule (`autostart.py:76-78`, `system.py:236-237`).
- **422** when `enabled` is missing or not a bool.

### `GET /api/system/defaults`
- **UI action:** loaded by `actionSetup` (`lib/actionSetup.svelte.ts`) on app
  start, and rendered in Settings > Providers under the defaults section.
- **Auth:** required.
- **200:** `ActionDefaults` (`schemas.py:110-133`): `stt_provider`, `stt_model`,
  `diar_mode`, `diar_endpoint`, `summarize_provider`, `summarize_model`,
  `summarize_prompt`, `video_provider`, `video_model`,
  `summarize_reasoning_effort`, `strict_model_filtering` (bool, default true).
- **A blank stored `summarize_prompt` is served as the built in default text**
  (`system.py:249-252`, `llm.py:32-37`), so the field is always concrete and
  editable.
- **No error branches.** Never stored yet returns all-blank defaults
  (`deps.py:25-28`).

### `PUT /api/system/defaults`
- **UI action:** Settings > Providers: picking a default STT provider or model,
  a default diarization mode or endpoint, an LLM provider or model, a summary
  prompt, a video provider or model, a reasoning effort, or flipping
  "Only show compatible models".
- **Auth:** required.
- **Body:** `ActionDefaults`. **Every field is an unvalidated free string** apart
  from `strict_model_filtering`. Missing fields default to `""`, so a partial PUT
  silently clears the rest.
- **200:** the stored object, with `summarize_prompt` re-filled with the built in
  default when it was stored blank.
- **Round trip rule:** a prompt equal to the built in text, or blank, is stored
  as `""` so it keeps tracking future changes to the built in text
  (`system.py:264-269`). Clearing the field and saving is the reset gesture.
- **422** only for a non-bool `strict_model_filtering` or a non-string field.
- **Side effect:** writes one row into `kv_settings` under the key
  `action_defaults` (`deps.py:16`). Saving `diar_endpoint` also changes what
  `/healthz` probes every 20 seconds.

### `GET /api/system/alerts/channels`
- **UI action:** Settings > Alerting, on mount.
- **Auth:** required.
- **200:** `list[AlertChannelView]` (`schemas.py:236-247`): `id`, `type`,
  `enabled`, `min_level`, `server`, `topic`, `chat_id`, `url`, `token_set`. The
  token itself is never returned, only whether one is stored.
- **No error branches.** Empty list when nothing is configured. Legacy pre-list
  config is migrated on read (`monitoring/alerts.py:97-127`).

### `POST /api/system/alerts/channels`
- **UI action:** Settings > Alerting, Add channel wizard, Save.
- **Auth:** required.
- **Body:** `AlertChannelWrite` (`schemas.py:221-233`): `type` (**enum**, one of
  `ntfy` | `telegram` | `webhook`, required), `enabled` (bool, default true),
  `min_level` (**enum** `info` | `warning` | `error`, default `warning`),
  `server` (default `https://ntfy.sh`), `topic`, `chat_id`, `url`, `token` (all
  optional and nullable).
- **201:** `AlertChannelView` with a freshly minted 32 hex `id`.
- **422** for an unknown `type` or `min_level`, or a missing `type`.
- **No validation that the type's own field is present.** An `ntfy` channel with
  no `topic` is created happily and then silently never delivers
  (`alerts.py:196-201` returns False). The UI blocks this client side
  (`settings/alerts/+page.svelte:138-140`), so reaching it is `[api-only]`.
- **Side effects:** appends to the `alerts` `kv_settings` row (read, modify,
  write, no lock); writes `alert:<id>:token` into the secret store only when
  `token` is truthy.

### `PUT /api/system/alerts/channels/{channel_id}`
- **UI action:** two gestures. The pencil (Edit) on a row, then `Save changes`.
  **And** ticking or unticking the row's Enabled checkbox in the `On` column,
  which sends the whole row back with `enabled` flipped and no `token`.
- **Auth:** required.
- **Path:** `channel_id`, any string.
- **Body:** `AlertChannelWrite`, same rules. **This is a full replace**: fields
  left out revert to their schema defaults, `server` back to `https://ntfy.sh`.
- **200:** `AlertChannelView`.
- **404 `alert channel not found`** when no channel has that id (`system.py:317`).
- **422** as for create.
- **Side effect:** the stored token is **only overwritten when a non-empty
  `token` is sent**; sending `""` or omitting it keeps the old token
  (`system.py:321-322`). There is no way to clear a token except deleting the
  channel.

### `DELETE /api/system/alerts/channels/{channel_id}`
- **UI action:** Settings > Alerting, the trash button on a row, then the
  destructive confirm `Delete this alert channel?`. **The call has no error
  handling**: a failure shows nothing and the list is not refreshed.
- **Auth:** required.
- **200:** `{"ok": true}`.
- **404 `alert channel not found`** when the id matches nothing (`system.py:333`).
- **Side effects:** rewrites the alerts settings row and deletes
  `alert:<id>:token` from the secret store.

### `POST /api/system/alerts/channels/{channel_id}/test`
- **UI action:** Settings > Alerting, the **Test** button on a row.
- **Auth:** required.
- **200:** `{"ok": bool}`.
- **No 404.** An unknown `channel_id` returns `{"ok": false}`, exactly like a
  configured channel whose delivery failed (`alerts.py:172-186`). The UI renders
  both as "Test failed".
- **Side effect:** ignores the channel's `enabled` flag and `min_level` and posts
  a real notification titled `Loreline test alert` to ntfy, Telegram or the
  webhook URL.

### `GET /api/system/services`
- **UI action:** Settings > Services, on mount and then **polled every 5
  seconds** (`settings/services/+page.svelte`). Rows split into Core services
  (`controllable: false`) and Additional services (`controllable: true`).
- **Auth:** required.
- **200:** `list[ServiceState]` (`services.py:37-51`): `name`, `container_id`
  (12 chars), `state`, `status`, `image`, `controllable`. Sorted by name and
  scoped to the compose project named by `COMPOSE_PROJECT_NAME`, default
  `loreline` (`app.py:190-195`).
- **Empty list, HTTP 200,** when `LORELINE_DOCKER_API` is unset (`system.py:363-364`).
  This is not an error and the page renders an empty Services tab.
- **503 `could not reach the Docker API: <detail>`** when the socket is
  configured but unreachable (`services.py:83-86`).

### `POST /api/system/services/{name}`
- **UI action:** Settings > Services, the Start/Stop button on an Additional
  services row.
- **Auth:** required.
- **Path:** `name`, any string.
- **Body:** `ServiceAction` (`system.py:353-356`): `{"running": bool}`, required.
- **200:** the refreshed `ServiceState`.
- **503 `service '<name>' cannot be controlled from the UI`** for anything
  outside the allowlist `{"speaches", "diarization"}` (`services.py:128-131`).
  `app` and `caddy` are deliberately excluded so the UI can never stop itself or
  the proxy.
- **503 `unknown service '<name>'`** when the name is not in this compose project
  (`services.py:120-125`).
- **503 `docker start failed (<code>): <text>`** when Docker refuses.
- **503 `Docker API not configured (LORELINE_DOCKER_API is unset)`**.
- **422** when `running` is absent or not a bool.
- **Idempotent:** Docker's 304 (already in that state) is treated as success.

### `GET /api/system/services/{name}/logs?tail=<n>`
- **UI action:** Settings > Services, the Logs icon on any row, and the Refresh
  button in the logs panel. The client always sends `tail=200`.
- **Auth:** required.
- **Query:** `tail`, **int, default 200, no bounds at all**. Negative and huge
  values are passed straight through to the Docker API.
- **200:** `{"name": str, "logs": str}`. Empty output renders as `(no output)`
  in the UI.
- **503** for the same four causes as the start/stop route.
- **422** when `tail` is not parseable as an int.

---

## 4. Audio routes (`src/loreline/web/routes/audio.py`)

### `GET /api/audio/devices`
- **UI action:** Settings > Client, on mount, fills the Microphone dropdown.
- **Auth:** required.
- **200:** `list[InputDevice]`. **Empty list when the `audio` extra is not
  installed** (`audio.py:41-42`), which is not an error: the dropdown then shows
  only "System default".
- **No error branches.**

### `GET /api/audio/device`
- **UI action:** Settings > Client, on mount, selects the stored microphone.
- **Auth:** required.
- **200:** `{"device": str | null}`. `null` means system default.
- **No error branches.**

### `PUT /api/audio/device`
- **UI action:** Settings > Client, **picking an entry in the Microphone
  dropdown**. There is no explicit Save button; "Saved" appears beside the row.
  If the level meter was running it is stopped and restarted on the new device.
- **Auth:** required.
- **Body:** `DeviceSetting` (`schemas.py:92-95`): `{"device": str | null}`.
- **200:** `{"ok": true}`.
- **No validation that the device exists.** Any string is accepted and stored;
  a device that has since disappeared is flagged only in the UI as
  `<name> (not found)` (`settings/client/+page.svelte:37-48`).
- **Side effect:** writes the `input_device` `kv_settings` row. This is what
  `POST /api/session/start` uses when the request omits `device`
  (`sessions.py:83-84`).

### `WS /ws/audio/level?device=<index or name>`
- **UI action:** Settings > Client, the **Test** button beside the Microphone
  dropdown (label toggles to Stop). Raw `new WebSocket(...)`, no auto reconnect
  (`settings/client/+page.svelte:87-89`).
- **Auth:** cookie checked before `accept()`; failure closes with 1008.
- **Query:** `device`. Coerced by `parse_device` (`audio.py:59-63`): all digits,
  optionally leading `-`, becomes an int index; anything else stays a string
  name; empty or absent becomes None (system default).
- **Messages, server to client:** `{"peak": float, "rms": float}`, both 0-1,
  throttled to about 20 Hz. `peak` is a hold-and-reset maximum across the
  interval.
- **Error frame:** `{"error": "<message>"}` then close, when the `audio` extra is
  missing or the device cannot be opened (`audio.py:96-99`).
- **Side effect:** **opens the physical capture device** at 16 kHz for as long as
  the socket is open, and calls `source.stop()` in `finally`.

### `WS /ws/audio/live-level`
- **UI action:** Dashboard capture card while recording, feeding the level meter.
  Uses the auto reconnecting `connect()` helper (`CaptureControls.svelte:320`).
- **Auth:** cookie checked before `accept()`; failure closes with 1008.
- **Query:** none.
- **Messages:** `{"peak": float, "rms": float}` relayed from the readings
  `SessionManager` already takes off the running capture. **Opens no device of
  its own.**
- **Silent while idle:** nothing is published outside an active capture, so the
  socket simply stays open and sends nothing. That is correct behaviour, not a
  hang.

---

## 5. Provider routes (`src/loreline/web/routes/providers.py`)

All four provider paths sit behind `require_auth` at router level
(`providers.py:21-23`).

### `GET /api/providers`
- **UI action:** Settings > Providers table, and `actionSetup` on app start
  (every picker in the app depends on this).
- **200:** `list[ProviderView]`: the full `ProviderConfig` (`models.py:150-176`)
  plus `secret_set` (bool) and `secret_hint` (masked, e.g. `sk-1…9abc`, or
  `••••` for values of 4 chars or fewer, `secrets.py:81-96`). **The raw key is
  never returned.**
- **No error branches.**

### `POST /api/providers`
- **UI action:** Settings > Providers, Add provider wizard, the Save button.
- **Body:** `ProviderCreate` (`schemas.py:24-40`):
  - `name`: required string, **no min length, not trimmed, blank accepted**.
  - `kind`: **required enum**, one of `deepgram`, `openai`, `openai_compat`,
    `assemblyai`, `gemini`, `openrouter`, `xai` (`models.py:11-25`).
  - `base_url`: optional string, **no URL validation**.
  - `favorite_models`: list of strings, default `[]`, **unbounded**.
  - `sample_rate`: int, default 16000, **no range check** (0 and negatives are
    accepted and are then used as the capture sample rate).
  - `language`: string, default `"de"`, **no code validation**.
  - `routing`: optional `OpenRouterRouting` (`models.py:123-147`): `sort` in
    `price` | `throughput` | `latency` or null, `data_collection` in `allow` |
    `deny`, `zdr` bool. Ignored for every non-OpenRouter kind.
  - `enabled`: bool, default true.
  - `api_key`: optional string, write only.
- **201:** `ProviderView` with a new 32 hex `id` and `auth_ref = "provider:<id>"`.
- **422** for an unknown `kind`, an unknown `routing.sort` or
  `routing.data_collection`, or a missing `name`/`kind`.
- **Side effect:** upserts the provider row; writes the secret store entry only
  when `api_key` is truthy (`providers.py:89-90`).
- **UI guard:** Save is disabled unless `effectiveName` is non-empty, and a blank
  Name box falls back to a deduplicated vendor label such as `Deepgram`,
  `Deepgram 2` (`settings/providers/+page.svelte:222-240, 1058`). So the blank
  name branch is `[api-only]`.

### `PUT /api/providers/{provider_id}`
- **UI action:** Settings > Providers, the Edit button on a row, then Save.
- **Body:** `ProviderCreate`, identical rules. **Full replace**: any field left
  out reverts to its schema default (`sample_rate` back to 16000, `language`
  back to `de`, `favorite_models` back to `[]`, `enabled` back to true).
- **200:** `ProviderView`.
- **404 `provider not found`** for an unknown id (`providers.py:101`).
- **The existing `auth_ref` is preserved**, so the stored key survives an edit
  that does not send one (`providers.py:102`).
- **An empty `api_key` never clears the key** (`providers.py:109-110`). The key
  field's placeholder is literally `•••• unchanged` when editing.

### `DELETE /api/providers/{provider_id}`
- **UI action:** Settings > Providers, the trash button on a row, then the
  destructive confirm `Delete this provider? This also removes its stored key.`
  **The call itself has no error handling**: a failure is a silent unhandled
  rejection and the table is not reloaded.
- **200:** `{"ok": true}`.
- **404 `provider not found`** for an unknown id (`providers.py:120`).
- **Side effect:** deletes the provider row **and** its stored secret. Sessions
  that referenced it keep the raw id, which the UI renders as an 8 char prefix.

### `POST /api/providers/{provider_id}/secret` `[api-only]`
- **UI action:** none. `api.setProviderSecret` exists in `api.ts:161-165` with
  **no call site**; the providers page sends `api_key` inline on create/update
  instead.
- **Body:** `SecretWrite` (`schemas.py:43-46`): `{"value": str}`, required,
  **no min length**.
- **200:** `{"ok": true}`.
- **404 `provider not found`** for an unknown id (`providers.py:133`).
- **Side effect:** writes the secret and, when the row had no `auth_ref`, back
  fills one and re-upserts the provider.
- **Note:** `{"value": ""}` writes an empty secret. `hint()` then reports
  `secret_set: false` while `get()` returns `""`, so the UI says "no key" while
  the connector sends an empty Authorization value.

### `POST /api/providers/{provider_id}/test`
- **UI action:** Settings > Providers, the **Test** button on a row.
- **200:** `TestResult` (`providers.py:39-52`): `status` in `healthy` |
  `degraded` | `unauthorized` | `unreachable` | `unknown`, plus `detail` carrying
  the vendor's own words.
- **404 `provider not found`** for an unknown id (`providers.py:155`). **This is
  the only HTTP error branch.** A rejected key, a dead host and a kind with no
  probe surface all come back as 200 with a graded `status`.
- **Side effect:** one outbound request to the vendor, capped at 10 seconds
  (`health.py:55`). Costs nothing and starts no capture.

### `POST /api/providers/models`
- **UI action:** every model picker in the app, lazily on first open of the
  dropdown, deduplicated per (provider row, interaction, refresh token) by
  `lib/modelCatalog.svelte.ts`. Also the `Load models` button in the add/edit
  provider wizard, which is the one caller that can send an **unsaved** `api_key`
  for a provider that does not exist yet. The Generate video dialog loads its
  models on **dialog open** rather than on dropdown open.
- **Body:** `ProviderModelsRequest` (`providers.py:55-67`):
  - `kind`: **required enum**, same seven values.
  - `base_url`: optional string.
  - `api_key`: optional string. When blank and `provider_id` is given, the stored
    key for that provider is used instead (`providers.py:172-176`).
  - `provider_id`: optional string. **An unknown id is not an error**, it simply
    yields no key.
  - `interaction`: enum `transcribe` | `summarize` | `video`, default
    `transcribe`.
- **200:** `list[ModelInfo]` (`models.py:88-121`): `id` plus optional
  `context_length`, `realtime`, `inline_diarization`, `supports_reasoning`,
  `pricing`, `price_tiers`.
- **422** for an unknown `kind` or `interaction`.
- **No error branch for a dead endpoint.** An unusable probe falls back to the
  curated list for that kind and interaction (`stt/catalog.py:72-89`), so the UI
  shows something rather than "No options" in most cases.
- **Filtering:** narrowed by the stored `strict_model_filtering` default, which
  is why flipping "Only show compatible models" changes the list.

---

## 6. Glossary routes (`src/loreline/web/routes/glossary.py`)

All four sit behind `require_auth` at router level.

### `GET /api/glossary`
- **UI action:** Settings > Glossary, on mount. Terms are joined with newlines
  into the textarea.
- **200:** `Glossary` (`models.py:285-290`): `{"campaign_id": "_default", "terms": [...]}`.
- **No error branches.** Never saved returns `terms: []`.

### `PUT /api/glossary`
- **UI action:** Settings > Glossary, **blur of the Terms textarea**. There is
  no Save button: the value is written when the field loses focus, and "Saved"
  shows for 2.5 seconds (`settings/glossary/+page.svelte:17`).
- **Body:** `GlossaryWrite` (`schemas.py:49-52`): `{"terms": [str]}`, default `[]`.
  **No max length, no per-term length limit, no deduplication, no trimming.**
  The UI splits the textarea on newlines, trims each line and drops blanks, so
  blank and whitespace-only terms are `[api-only]`.
- **200:** the stored `Glossary`.
- **Side effect:** replaces the whole `_default` row. This list is merged into
  every session's glossary regardless of campaign (`repositories.py:106-119`).

### `GET /api/glossary/{campaign_id}` `[api-only]`
- **UI action:** none. `api.getGlossary` has no call site.
- **200:** the campaign's `Glossary`. **An unknown campaign returns 200 with
  `terms: []`, never 404** (`repositories.py:88-94`).

### `PUT /api/glossary/{campaign_id}` `[api-only]`
- **UI action:** none. `api.putGlossary` has no call site.
- **Body:** `GlossaryWrite`.
- **200:** the stored `Glossary`.
- **`campaign_id` is completely unvalidated** and the row is created on first
  write, so any string becomes a campaign. Writing to `_default` through this
  path is equivalent to `PUT /api/glossary`.

---

## 7. Session routes (`src/loreline/web/routes/sessions.py`)

All behind `require_auth` at router level (`sessions.py:61`).

### `POST /api/session/start`
- **UI action:** Dashboard capture card, the **Start session** button.
- **Body:** `StartSessionRequest` (`schemas.py:55-89`):
  - `primary_provider`: **required** string (a provider id).
  - `fallback_provider`: optional string or null.
  - `campaign_id`: optional string or null. **Unvalidated**, and never sent by
    the UI.
  - `device`: `int | str | null`. **When null, the saved default microphone is
    substituted server side** (`sessions.py:83-84`).
  - `model`: **required, `min_length=1`**.
  - `fallback_model`: optional, but **required as soon as `fallback_provider` is
    set** (model validator, `schemas.py:78-89`).
  - `diarization`: `DiarizationConfig` (`models.py:178-184`): `mode` in `inline`
    | `remote` | `openai` | `none` (default `none`), `endpoint`, `min_speakers`,
    `max_speakers` (both `int | null`, **no range check**).
  - `use_glossary`: bool, default true.
- **201:** the created `Session` (`models.py:296-312`) with `status: "capturing"`.
- **409 `a session is already running`** when a capture is active
  (`sessions.py:87-90`, raised at `session/manager.py:566`).
- **404 `unknown primary provider '<id>'`** / **`unknown fallback provider '<id>'`**
  (`manager.py:509, 521`).
- **409 `primary provider '<id>' is disabled`** / **`fallback provider '<id>' is
  disabled`** (`manager.py:512, 524`).
- **400** with `<role> provider '<name>' (<kind>) cannot drive a live capture - it
  is available for post-session re-processing only`, for OpenRouter, whose
  transcription API is batch only (`manager.py:475-483`,
  `capabilities.yaml:778`).
- **400** with `model '<model>' on '<name>' returns no speaker labels - inline
  diarization would produce an unlabelled transcript`, when `diarization.mode`
  is `inline` and the chosen model does not return speakers
  (`manager.py:485-500`).
- **400 `remote diarization requires an endpoint`** when mode is `remote` and
  `endpoint` is blank (`diarization/provider.py:80-83`, wrapped at
  `manager.py:557-560`).
- **400 `the default input device could not be opened for recording (<error>).
  Pick another microphone in Settings, or check that nothing else is using it.`**
  (or `input device '<x>' could not be opened...`) when the microphone preflight
  fails (`manager.py:728-748`). This runs **before** anything is persisted, so a
  failed start leaves no session row.
- **400** for a provider kind with no registered backend (`manager.py:545-552`).
- **422** when `model` is blank, when `primary_provider` is missing, when
  `fallback_provider` is set without `fallback_model`, or for an unknown
  `diarization.mode`.
- **Side effects on success:** creates the session row, opens the microphone,
  creates `<data>/audio/<id>.wav` and its index sidecar, starts the capture,
  persistence and router tasks, and starts the log file
  `<data>/logs/<id>/original.log`.
- **UI guards** (so several branches above are `[api-only]`): Start is disabled
  without a provider and a model; `remote` with a blank endpoint blocks Start
  inline; a fallback provider without a fallback model blocks Start; providers
  that cannot drive a live capture are filtered out of the dropdown; `inline` is
  hidden for models that return no speakers.

### `POST /api/session/stop`
- **UI action:** Dashboard capture card, the red **Stop session** button (shows
  `Finalizing…`).
- **Body:** none.
- **200:** the finalized `Session`, with `status` `completed` or `error` and
  `ended_at` set.
- **409 `no active session`** when nothing is running (`sessions.py:104`). The UI
  treats this specially: if it knows which session was ending it fetches that
  session and reports whether it failed, rather than showing a raw error
  (`CaptureControls.svelte:428-436`).
- **Side effects:** drains the STT queue (up to a 30 second timeout), finalizes
  the WAV and index, writes the final status, may fire a "Session error" alert.

### `GET /api/session`
- **UI action:** History page (`/sessions`), on mount.
- **200:** `list[Session]`, newest first.
- **No error branches.** Empty list renders "No sessions recorded."

### `GET /api/session/{session_id}`
- **UI action:** session page (`/sessions/<id>`) on mount and after every job
  completes; also the Dashboard live transcript pane's re-seed.
- **200:** `SessionDetail` (`sessions.py:66-76`): `session`, `transcript` (the
  **original** version, diarized relabeling applied when one exists), and
  `audio_duration_s` (`float | null`, read from the WAV header on every request).
- **404 `session not found`** for an unknown id (`sessions.py:120`).
- **Note:** `audio_duration_s` is null when there is no `audio_path` or no stored
  files. A value at or below 0.05 is what the UI calls "empty audio".

### `GET /api/session/{session_id}/transcript?version=<v>`
- **UI action:** session page, clicking a **re-transcription** row in the
  Transcriptions table. Selecting the `original` row does **not** call this: the
  original comes from `GET /api/session/{id}`.
- **Query:** `version`, string, **default `"original"`**.
- **200:** `list[TranscriptEvent]` for that version. Returns the version's
  diarized relabeling when one exists, its raw rows otherwise
  (`export.py:39-53`).
- **404 `session not found`** for an unknown session (`sessions.py:140`).
- **An unknown `version` is 200 with `[]`, not 404.** The UI renders that as an
  empty transcript.

### `DELETE /api/session/{session_id}/transcript?version=<v>`
- **UI action:** session page, Transcriptions table, the Delete button on a
  re-transcription row (confirms with the segment count).
- **Query:** `version`, **required, no default**.
- **200:** `{"ok": true}`.
- **422** when `version` is omitted.
- **404 `session not found`** for an unknown session (`sessions.py:154`).
- **409 `the original transcript cannot be deleted`** for `version=original`
  (`reprocess/jobs.py:300-302`). The UI does not render a Delete button on that
  row at all, so this is `[api-only]`.
- **404 `unknown transcript version '<v>'`** when no *transcribe* job owns that
  id (`jobs.py:303-309`). Note a **diarize** job id is deliberately not a
  deletable version.
- **409 `transcript version '<v>' is still being written`** when a queued or
  running transcribe job owns it, or a queued/running diarize job targets it
  (`jobs.py:311-321`). The UI disables Delete while a job is in flight, so this
  needs two tabs to reach.
- **Side effects:** deletes the version's rows, its `diarize:<v>` relabeling
  rows, its job rows, and its log file `<data>/logs/<session>/<v>.log`.

### `GET /api/session/{session_id}/logs?version=<v>`
- **UI action:** session page, the **Show logs** button on any Transcriptions
  row (opens `SessionLogsDialog`).
- **Query:** `version`, string, **default `"original"`**.
- **200:** `{"session_id": str, "version": str, "logs": str}`.
- **404 `session not found`** for an unknown session (`sessions.py:180`).
- **404 `no logs stored for this version`** when the file is absent or
  unreadable, **and also for a path-unsafe version string** such as `..` or
  `a/b`, which `_segment` rejects with a ValueError that is caught here
  (`sessions.py:183-189`, `persistence/log_store.py:107-111`). The UI shows
  "No logs were stored for this version."

### `PUT /api/session/{session_id}/speakers`
- **UI action:** session page, renaming a speaker in place in the transcript
  (Enter or blur commits), and the Rename speakers dialog.
- **Body:** `SpeakerNamesUpdate` (`schemas.py:98-101`): `{"names": {str: str}}`,
  default `{}`. **Unbounded, unvalidated, keys and values both free strings.**
- **200:** `{"ok": true}`.
- **404 `session not found`** for an unknown id (`sessions.py:200`).
- **Side effect:** replaces the whole map. Sending `{}` clears every rename. The
  map is applied on read to the transcript view, exports and summaries.

### `POST /api/session/{session_id}/summarize`
- **UI action:** session page, Summary section, the Summarize dialog's confirm
  button.
- **Body:** `SummarizeRequest` (`schemas.py:136-146`): `provider_id` (required),
  `model` (**required, `min_length=1`**), `reasoning_effort` (optional string,
  **unvalidated**).
- **200:** `{"summary": str}`.
- **404 `session not found`** (`sessions.py:213`).
- **404 `provider not found`** (`sessions.py:216`).
- **400 `provider is not an LLM provider`** when the kind does not declare the
  `summarize` interaction, that is `deepgram` and `assemblyai`
  (`sessions.py:218-221`, `capabilities.yaml`).
- **400 `session has no transcript`** when the original version has no final,
  non gap rows (`sessions.py:227`). Reachable on a session that captured audio
  but transcribed nothing.
- **502** with the vendor's own error text when the chat call fails: bad model
  id, rejected key, rate limit, no credits (`sessions.py:241-242`,
  `llm.py:56-57`).
- **422** when `model` is blank or `provider_id` is missing.
- **Effort resolution:** request value, else the stored
  `summarize_reasoning_effort` default, else nothing. System prompt: the stored
  `summarize_prompt` default, else the built in.
- **Side effect on success:** persists `summary`, `summary_provider` and
  `summary_model` on the session row.
- **Known limitation:** always summarizes the **original** version
  (`sessions.py:222-223`), never the version selected in the UI.

### `GET /api/session/{session_id}/export?fmt=<f>`
- **UI action:** session page, the Export menu: Text (.txt), Markdown (.md),
  Subtitles (.srt), Subtitles (.vtt), JSON (.json). Navigates the browser to the
  URL, so the response is a download, not a fetch.
- **Query:** `fmt`, string, **default `"txt"`**. Valid values are exactly
  `txt`, `md`, `srt`, `vtt`, `json` (`export.py:157-163`).
- **200:** the rendered body with the format's media type and
  `Content-Disposition: attachment; filename="<session_id>.<ext>"`.
- **404 `unknown format '<f>'`** for anything else (`sessions.py:255`). Because
  this check runs **first**, a request with both a bad session id and a bad
  format reports the format.
- **404 `session not found`** (`sessions.py:260`).
- **Content rule:** final rows only, gap markers and interims excluded, speaker
  renames applied (`export.py:61-78`). Always the **original** version.
- **Auth note:** this is a plain browser navigation, so with an expired cookie
  the browser renders the raw JSON 401 body in a new context rather than
  bouncing to `/login`.

### `GET /api/session/{session_id}/audio`
- **UI action:** session page, the docked player's `<audio src>` (fetched on
  page load because of `preload="metadata"`, so no click is needed), and the
  Export menu's "Audio (.wav)" entry. Neither goes through the fetch wrapper, so
  a 401 here does **not** redirect to `/login`.
- **200:** the WAV file, `audio/wav`, `filename="<session_id>.wav"`.
- **404 `session not found`** (`sessions.py:279`).
- **404 `no audio for session`** when the WAV or its index sidecar is missing
  (`sessions.py:280-281`, `audio_store.py:151-152`, which requires **both**).
- **Note:** an errored session can have a 44 byte header-only WAV that passes
  `exists()`. That is what `audio_duration_s` near zero and the disabled
  "Audio (empty)" menu entry are for.

### `POST /api/session/delete`
- **UI action:** History page, tick rows, **Delete selected**, confirm
  "Delete N session(s)? This also removes their audio."
- **Body:** `SessionIds` (`schemas.py:104-107`): `{"ids": [str]}`, default `[]`.
- **200:** `{"ok": true}`, **always**.
- **No 404 for unknown ids**, they are simply iterated over and delete nothing.
- **The currently running session is silently skipped** (`sessions.py:295-296`)
  and the caller is not told.
- **An empty `ids` list is a 200 no-op.**
- **Side effects per id:** deletes transcript rows, the WAV and index, the whole
  `<data>/logs/<id>/` directory, and the session row. Nothing checks for
  in-flight reprocess or video jobs on that session.

### `POST /api/session/merge`
- **UI action:** History page, tick two or more rows, **Merge selected**, confirm
  the dialog naming the parts oldest to newest. On success the app navigates to
  the new session.
- **Body:** `SessionIds`.
- **200:** the new merged `Session` with `status: "completed"`.
- **409 `merge needs at least 2 sessions`** when **fewer than two of the ids
  resolve to real sessions** (`sessions.py:315-317`). Unknown ids are filtered
  out silently first, so merging one real and one bogus id reports "needs at
  least 2", not "not found".
- **409 `cannot merge a running session`** when any source is the active capture
  (`sessions.py:320-321`).
- **Behaviour:** sources sorted oldest first by `started_at`; each part's
  timestamps offset so they run back to back; `turn_id` cleared on every copied
  row; speaker rename maps unioned with first-writer-wins; originals left intact.
- **Audio:** merged only when **every** source has stored audio at one shared
  sample rate. Mixed sample rates or any missing WAV degrades to a transcript
  only merge with `audio_path: null`, silently and still 200
  (`sessions.py:326-337`, `audio_store.py:243-262`).
- **Offsets:** with merged audio, parts advance by audio length; without it, by
  the last event's `end_ts`.
- **Side effects:** creates one session row, writes N transcript rows, may write
  a new WAV and index. **Not transactional.**

---

## 8. Reprocess routes (`src/loreline/web/routes/reprocess.py`)

All behind `require_auth` at router level.

### `POST /api/reprocess`
- **UI action:** session page, the **New transcription** row (`ReprocessPanel`),
  and the diarize control in `TranscriptPanel`.
- **Body:** `ReprocessRequest` (`schemas.py:155-180`):
  - `session_id`: required string.
  - `provider_id`: string, default `""`. Required in practice for `transcribe`,
    ignored for `diarize`.
  - `operation`: `Literal["transcribe","diarize"]`, default `transcribe`.
  - `diarization`: `DiarizationConfig`, same shape as on start. **Unlike the
    capture card, the Diarize control exposes free-form `min`/`max` speaker
    number inputs**, so out-of-range and inverted speaker counts are reachable
    from the UI here.
  - `model`: optional string, but **required for `transcribe`** (model validator,
    `schemas.py:175-180`); ignored for `diarize`.
  - `target`: string, default `"original"`. The version a `diarize` job relabels.
  - `use_glossary`: bool, default true, `transcribe` only.
- **202:** the queued `ReprocessJob` (`models.py:370-386`).
- **404 `unknown session '<id>'`** (`jobs.py:234-236`).
- **409 `session '<id>' has no stored audio`** (`jobs.py:237-239`). The UI
  replaces the whole panel with "No stored audio for this session..." in that
  case, so this is `[api-only]`.
- **404 `unknown provider '<id>'`** for a `transcribe` job, **including
  `provider_id: ""`**, which resolves to nothing (`jobs.py:243-245`).
- **404 `unknown transcript version '<target>'`** for a `diarize` job whose
  `target` is neither `original` nor a version with rows (`jobs.py:246-250`).
- **422** when `session_id` is missing, when `operation` is not one of the two
  literals, or when `operation` is `transcribe` with a blank `model`.
- **Side effects:** inserts the job row and **spawns a background asyncio task
  immediately**. The response is 202 before any work is done.
- **Deferred failures are not HTTP errors.** A `diarize` job with
  `mode: "remote"` and no endpoint is accepted with 202 and fails inside the
  runner, ending as `status: "error"`. The identical config on
  `POST /api/session/start` is a 400.

### `GET /api/reprocess/{job_id}` `[api-only]`
- **UI action:** none. `api.getReprocess` has no call site; the session page
  polls the list route instead.
- **200:** one `ReprocessJob`.
- **404 `job not found`** (`reprocess.py:42`).

### `GET /api/reprocess?session_id=<id>`
- **UI action:** session page, on mount and then **every 1.5 seconds while any
  job is queued or running**.
- **Query:** `session_id`, **required**.
- **200:** `list[ReprocessJob]`, newest first.
- **422** when `session_id` is omitted.
- **No 404 for an unknown session**, it returns `[]`.
- **Client note:** only the mount call is wrapped in a try/catch. A failure
  during the 1.5 second poll is a silent unhandled rejection and the interval
  keeps running.

---

## 9. Video routes (`src/loreline/web/routes/video.py`)

All behind `require_auth` at router level.

### `GET /api/video/models?provider_id=<id>`
- **UI action:** opening the model picker inside the Generate video dialog
  (`lib/modelCatalog.svelte.ts`).
- **Query:** `provider_id`, **required**.
- **200:** `list[VideoModelInfo]` (`models.py:315-337`): `id`, `name`,
  `description`, `supported_durations`, `supported_resolutions`,
  `supported_aspect_ratios`, `supported_sizes`, `generate_audio`, `seed`. A
  `null` list means "this model takes no such parameter", which is different from
  an empty list.
- **404 `provider not found`** (`video.py:42`).
- **400 `provider cannot generate video`** for any kind other than `openrouter`
  and `xai` (`video.py:43-46`).
- **422** when `provider_id` is omitted.
- **Best effort:** an unreachable provider returns `200 []`, not an error
  (`video/client.py:157-158`). xAI publishes no video catalogue at all, so `[]`
  is its ordinary answer and the dialog builds its controls from
  `capabilities.yaml` instead.

### `POST /api/video`
- **UI action:** session page, Summary section, Generate video dialog, the
  submit button.
- **Body:** `VideoGenerateRequest` (`schemas.py:183-204`): `session_id`,
  `provider_id`, `model`, `prompt` (all required strings), `duration`
  (`int | null`, **no range check**), `resolution`, `aspect_ratio`
  (`str | null`, **not checked against the model's supported list**),
  `generate_audio` (bool, default false), `seed` (`int | null`).
- **202:** the queued `VideoJob` (`models.py:339-368`).
- **404 `unknown session '<id>'`** (`video/jobs.py:117-119`).
- **404 `unknown provider '<id>'`** (`jobs.py:122-124`).
- **409 `provider '<name>' cannot generate video`** (`jobs.py:125-127`).
- **400 `prompt is empty`** when `prompt.strip()` is empty (`jobs.py:129-132`).
- **422** for a missing required field or a non-int `duration`/`seed`.
- **Side effects:** inserts the job row and spawns a background task that polls
  the vendor for minutes, then downloads the MP4 into
  `<data>/video/<job_id>.mp4`.

### `GET /api/video?session_id=<id>`
- **UI action:** session page, Summary section, on mount and then **polled
  every 5 seconds while any job is queued or running**. Only the mount call has
  error handling; a failure during the poll is a silent unhandled rejection.
- **Query:** `session_id`, **required**.
- **200:** `list[VideoJob]`, newest first. **No 404 for an unknown session.**
- **422** when `session_id` is omitted.

### `GET /api/video/{job_id}` `[api-only]`
- **UI action:** none. `api.getVideoJob` has no call site.
- **200:** one `VideoJob`. **404 `job not found`** (`video.py:75`).

### `GET /api/video/{job_id}/content`
- **UI action:** session page, the `<video src>` of a finished job.
- **200:** the MP4, `video/mp4`,
  `filename="<session_id>-<job_id>.mp4"`.
- **404 `job not found`** (`video.py:90`).
- **409 `video is not ready`** when the job is `queued`, `running` or `error`
  (`video.py:91-92`).
- **404 `video file is missing`** when the job says `done` but the file is gone
  (`video.py:93-94`).

### `DELETE /api/video/{job_id}`
- **UI action:** session page, the Delete button on a video job, then the
  confirm `Delete this video and its file?`. **The call has no error handling**:
  a failure shows nothing and the list is not refreshed.
- **200:** `{"ok": true}`.
- **404 `job not found`** (`video.py:108`).
- **Side effect:** removes the MP4 and the job row. **Deleting a running job does
  not cancel its background task**; the task keeps polling and will try to write
  a row that no longer exists.

---

## 10. WebSocket routes

### `WS /ws/transcript` and `WS /ws/transcript?session_id=<id>`
`src/loreline/web/routes/transcript_ws.py`.
- **UI action:** without `session_id`, the Dashboard live transcript pane
  (`LiveTranscriptPane.svelte:45`). With `session_id`, the session page
  (`sessions/[id]/+page.svelte:111`). Both use the reconnect-forever helper.
- **Auth:** cookie checked before `accept()`; failure closes with 1008.
- **Query:** `session_id`, optional, **unvalidated, no 404 for an unknown id**
  (the socket just stays silent).
- **Messages:** `TranscriptEvent` as JSON, one per frame.
- **Filtering (`transcript_ws.py:15-32`):**
  - With `session_id`: **every** event for that session, all versions, live
    capture and re-processing runs alike, interims and gap markers included.
    The client routes by `event.source`.
  - Without `session_id`: only the running capture's own events. Events whose
    source starts with `reprocess:` or `diarize:` are filtered out, and nothing
    at all is sent while idle.
- **No replay.** New subscribers get only what is published after they connect,
  which is why the Dashboard pane seeds itself from `GET /api/session/{id}`
  first.

### `WS /ws/logs`
`src/loreline/web/routes/logs_ws.py`.
- **UI action:** Dashboard live logs pane (`LiveLogsPane.svelte:178`).
- **Auth:** cookie checked before `accept()`; failure closes with 1008.
- **Query:** none.
- **Messages:** one rendered log line per text frame.
- **Replay:** the ring buffer's history (500 lines max, `logbus.py:50`) is sent
  first, then new lines, de-duplicated by sequence id.
- **Filtering:** only lines belonging to the capture running **right now** and
  carrying no `job_id` (`logbus.py:33-44`). Re-processing lines are excluded
  even when they replay the very session being captured, and nothing at all is
  sent while idle. This is why the pane shows "Logs appear here while a session
  is recording..." between sessions.

---

## UI TEST STEPS

Each numbered step is a browser gesture. `[neg]` marks an error branch,
`[api-only]` marks a branch that needs curl or DevTools rather than a click.
Groups follow the route sections above.

### A. Auth

1. Open `http://10.10.50.55/` while logged out. Expect a redirect to `/login`
   with no header or sidebar.
2. `[neg]` Submit the login form with an empty password. Expect the inline
   destructive paragraph reading `invalid password`, and the Sign in button to
   re-enable.
3. `[neg]` Submit a wrong password four more times (five failures total). On the
   sixth attempt, **even with the correct password**, expect the inline error
   `too many attempts, try again shortly`.
4. Wait 30 seconds, submit the correct password, expect a redirect to `/`.
5. `[obs]` In DevTools > Application > Cookies, confirm `loreline_token` is
   present, `HttpOnly`, `SameSite=Lax`, and that `Secure` is **not** set (plain
   HTTP LAN path).
6. `[neg]` Delete the `loreline_token` cookie in DevTools, then click any nav
   item. Expect the app to bounce to `/login` rather than render blank.
7. Log back in, click Logout in the header, confirm the cookie is cleared and
   `/` redirects to `/login`.
8. `[api-only]` `curl -X POST .../api/auth/logout` with no cookie. Expect 200
   `{"ok":true}`.
9. `[api-only]` `curl .../api/system/healthz` with **no cookie**. Expect 200 and
   a full payload, confirming the endpoint is unauthenticated.
10. `[api-only]` `curl .../api/capabilities` with no cookie. Expect 200.
11. `[api-only]` `curl .../api/providers` with no cookie. Expect 401
    `{"detail":"authentication required"}`.
12. `[api-only]` `curl .../api/nope`. Expect a real 404, not the SPA HTML.
    Then `curl .../nope` and confirm you get the SPA `index.html` with HTTP 200.

### B. Health and the header badge

13. `[obs]` Hover the header health dot. Confirm Service, Version, Capture,
    Uptime, Disk free `x.x GiB / y.y GiB` and Alerts on/off, and that Uptime
    increases between two hovers.
14. `[obs]` With no diarizer endpoint saved, confirm the popover shows no
    diarizer row (all four `diarizer_*` fields are null).
15. Save a working diarizer endpoint in Settings > Providers, then confirm within
    about 20 seconds that the health popover reports it reachable (the verdict is
    cached for 20 seconds, so an immediate change may lag).

### C. Settings > Client

16. Open Settings, confirm it redirects to `/settings/client` and the tab strip
    reads Client, Providers, Glossary, Alerting, Services.
17. `[obs]` Confirm the Microphone dropdown lists "System default" plus the
    detected devices. On a build without the `audio` extra, expect only
    "System default" and no error.
18. Pick a device in the Microphone dropdown. Confirm "Saved" appears beside it
    **without any Save button being clicked**: selection is the save gesture.
19. Reload the page, confirm the selection survived.
20. `[neg]` Set the device to one that no longer exists (unplug a USB mic after
    saving, then reload). Confirm the option renders as `<name> (not found)` with
    the "This device is no longer available" tooltip.
21. Click **Test** beside the Microphone dropdown. Confirm the level meter moves
    with sound and that the button reads Stop.
22. `[neg]` Click Test with a device that cannot be opened (or on a build without
    the `audio` extra). Confirm the meter stays flat and the socket closes rather
    than the page hanging (the server sends `{"error": ...}` then closes).
23. `[obs]` Confirm the current git commit renders next to Update now.
24. `[obs]` Confirm the Autostart row. On the Docker deployment the backend
    answers 503, so confirm the row shows an unavailable state and not a working
    toggle. `[neg]`
25. `[api-only]` On a source install only, `PUT /api/system/autostart` with
    `{"enabled":true}` while the sudoers rule is missing. Expect 409
    `failed to enable systemd unit 'loreline': ...`.
26. `[api-only]` `PUT /api/system/autostart` with `{}`. Expect 422.
27. Click **Update now**. Confirm the label becomes `Updating…` and disables.
    Confirm the outcome renders either "Update complete." or
    "Update failed (see output)." with an output pane for multi line output.
    Note the response is HTTP 200 in both cases.
28. `[api-only]` `POST /api/system/rollback` with
    `{"commit":"--upload-pack=x"}`. Expect 422 (the hex-only pattern). Then with
    `{"commit":"abc"}` (too short). Expect 422. Then with a real 7-40 hex commit
    on a source install. Expect 200 `UpdateResult`.

### D. Settings > Providers

29. Click Add provider, pick a vendor, leave Name blank. Confirm the Name
    placeholder shows a generated default such as `Deepgram`, and that Save is
    **enabled** because the blank falls back to that default.
30. `[api-only]` `POST /api/providers` with `{"name":"","kind":"deepgram"}`.
    Expect **201**, not a validation error: the backend does not require a name.
31. `[api-only]` `POST /api/providers` with `{"name":"x","kind":"not-a-kind"}`.
    Expect 422 listing the seven valid kinds.
32. `[api-only]` `POST /api/providers` with `{"name":"x","kind":"openai",
    "sample_rate":-1}`. Expect **201**: there is no range check.
33. Save a provider with a real API key. Confirm the row shows a masked hint such
    as `sk-1…9abc` and never the raw key.
34. Edit that provider without touching the key field (placeholder
    `•••• unchanged`), save, confirm the hint is unchanged.
35. `[neg]` Edit it and set the key field to empty, save. Confirm the key is
    **still set**: an empty string never clears a stored key.
36. Click **Test** on a provider with a good key. Expect a healthy badge.
37. `[neg]` Click Test on a provider with a deliberately wrong key. Expect an
    `unauthorized` badge whose tooltip carries the vendor's own message, and
    **HTTP 200**, not an error.
38. `[neg]` Point a self-hosted provider at a dead base URL and click Test.
    Expect an `unreachable` badge, again HTTP 200.
39. `[api-only]` `POST /api/providers/does-not-exist/test`. Expect 404
    `provider not found`.
40. `[api-only]` `POST /api/providers/<id>/secret` with `{"value":"abc"}`.
    Expect 200, then confirm the providers table shows a hint for that row.
41. `[api-only]` `POST /api/providers/<id>/secret` with `{"value":""}`. Expect
    200, then confirm the UI reports `secret_set: false` even though an empty
    secret is now stored.
42. `[api-only]` `DELETE /api/providers/does-not-exist`. Expect 404.
43. Delete a provider that a past session used. Confirm History renders that
    session's Primary column as an 8 char id prefix, not a blank cell.
44. Open any model picker for the first time. Confirm "Loading…" then a list, and
    that reopening does not refetch.
45. Flip "Only show compatible models" and reopen a picker. Confirm the list
    changes (this is `PUT /api/system/defaults` plus a cache invalidation).
46. `[api-only]` `POST /api/providers/models` with
    `{"kind":"openai","interaction":"nope"}`. Expect 422.
47. `[api-only]` `POST /api/providers/models` with
    `{"kind":"openai","provider_id":"does-not-exist"}`. Expect **200**: an
    unknown provider id is not an error, it just yields no key.

### E. Settings > Providers, the defaults block

48. Set a default STT provider and model, a default LLM provider and model, a
    default video provider and model, and a reasoning effort. Reload, confirm
    all survived.
49. Clear the summary prompt field entirely and save. Reload, confirm the field
    comes back filled with the **built in** default text (blank is stored, the
    default is served).
50. Edit the summary prompt to something custom, save, reload, confirm your text
    is what comes back.
51. Set the diarization endpoint default to a bogus URL, save, then hover the
    header health dot within 20 seconds and again after 20 seconds. Confirm the
    diarizer verdict eventually reports unreachable. `[neg]`
52. `[api-only]` `PUT /api/system/defaults` with `{"stt_provider":"x"}` only.
    Then `GET /api/system/defaults` and confirm **every other field was reset to
    blank**: this is a full replace, not a patch.

### F. Settings > Glossary

53. Type several terms, one per line, then **click outside the textarea**.
    Confirm "Saved" appears: blur is the save gesture, there is no Save button.
54. Reload, confirm the terms came back in order.
55. Clear the textarea and blur. Confirm an empty glossary round trips. Then
    type three blank lines plus a term with surrounding spaces, blur, reload, and
    confirm only the trimmed term came back: the client trims and drops blanks,
    so a blank term is `[api-only]`.
56. `[api-only]` `PUT /api/glossary/my-campaign` with `{"terms":["a"]}`. Expect
    200, then `GET /api/glossary/my-campaign` and confirm the row now exists.
    Any string becomes a campaign.
57. `[api-only]` `GET /api/glossary/never-used`. Expect 200 with `terms: []`,
    not 404.

### G. Settings > Alerting

58. Click Add channel, choose ntfy, fill Server and Topic, save. Confirm the row
    appears with the topic in the target column.
59. `[neg]` Choose ntfy and leave Topic blank. Confirm the Save button is
    disabled client side (`settings/alerts/+page.svelte:138-140`).
60. `[api-only]` `POST /api/system/alerts/channels` with `{"type":"ntfy"}` and no
    topic. Expect **201**, then click Test on that row and confirm it reports
    failure: the channel was created but can never deliver.
61. `[api-only]` `POST /api/system/alerts/channels` with `{"type":"sms"}`.
    Expect 422.
62. Click **Test** on a working ntfy channel. Confirm a real notification arrives
    on the device and the UI shows "Test sent".
63. `[neg]` Click Test on a channel with a wrong token. Confirm "Test failed" and
    HTTP 200 with `{"ok": false}`.
64. `[api-only]` `POST /api/system/alerts/channels/does-not-exist/test`. Expect
    **200 `{"ok": false}`**, not 404. Compare with step 65.
65. `[api-only]` `DELETE /api/system/alerts/channels/does-not-exist`. Expect
    **404 `alert channel not found`**. The inconsistency between 64 and 65 is a
    real finding.
66. Edit a Telegram channel and save without retyping the bot token. Confirm the
    row still shows a token as set.
67. Delete a channel. Confirm the row goes and the header popover's Alerts flag
    flips off if it was the only enabled one.
68. Untick a channel's Enabled checkbox in the `On` column. Confirm this issues a
    full `PUT` of the row and that the stored token survives it (the row still
    reports a token as set).
69. `[neg]` Delete a channel while the backend is stopped. Confirm **nothing at
    all** is shown: that call has no error handling. Repeat for deleting a
    provider and deleting a video job, same silence.

### H. Settings > Services

70. `[obs]` Open Settings > Services. On the Docker deployment confirm Core
    services (app, caddy) render **without** Start/Stop buttons and Additional
    services (speaches, diarization) render **with** them.
71. Click Stop on `diarization`. Confirm the row's state changes and the button
    flips to Start. Click Start again.
72. Click the Logs icon on any row. Confirm output appears, then click Refresh
    and Close. `[obs]` Note the error message, if any, is rendered **inside** the
    log body rather than as a banner.
73. `[obs]` Leave Settings > Services open for a minute with the network tab
    open. Confirm `GET /api/system/services` fires every 5 seconds, and that a
    persistent failure therefore re-renders its red banner every 5 seconds.
74. `[obs]` On a service producing nothing, confirm the panel shows `(no output)`.
75. `[api-only]` `POST /api/system/services/app` with `{"running":false}`.
    Expect **503 `service 'app' cannot be controlled from the UI`**. Note the
    status code is 503 for what is really a bad request.
76. `[api-only]` `POST /api/system/services/nope` with `{"running":true}`.
    Expect **503 `unknown service 'nope'`**, again not 404.
77. `[api-only]` `GET /api/system/services/diarization/logs?tail=-5`. Confirm the
    unvalidated value is accepted and passed through.
78. `[api-only]` `GET /api/system/services/diarization/logs?tail=abc`. Expect 422.
79. `[neg]` On a bare metal install with `LORELINE_DOCKER_API` unset, confirm
    Settings > Services renders empty (HTTP 200 with `[]`) rather than an error.

### I. Dashboard, starting a capture

80. `[neg]` With no model picked, confirm Start session is disabled and the hint
    "Pick a model to start ..." shows.
81. `[obs]` Open the Transcription provider dropdown and confirm an OpenRouter
    row is **absent** (batch only, cannot drive a live capture), while a
    chat-only vendor is absent too.
82. `[neg]` Open Edit, set a Fallback provider and no Fallback model. Confirm the
    summary reads `<name> - model missing` in red and Start is disabled.
83. `[api-only]` `POST /api/session/start` with `fallback_provider` set and
    `fallback_model` blank. Expect 422 with
    `fallback_model is required when a fallback_provider is set`.
84. `[neg]` Set Diarization to Remote and clear the endpoint. Confirm the inline
    error and a disabled Start.
85. `[api-only]` `POST /api/session/start` with
    `{"diarization":{"mode":"remote","endpoint":null}, ...}`. Expect **400
    `remote diarization requires an endpoint`**.
86. `[api-only]` `POST /api/session/start` with an OpenRouter `primary_provider`.
    Expect 400 `... cannot drive a live capture - it is available for
    post-session re-processing only`.
87. `[api-only]` `POST /api/session/start` with `primary_provider` set to a
    provider whose Enabled switch is off. Expect **409
    `primary provider '<id>' is disabled`**.
88. `[api-only]` `POST /api/session/start` with
    `{"primary_provider":"nope","model":"m"}`. Expect 404
    `unknown primary provider 'nope'`.
89. `[api-only]` `POST /api/session/start` with `{"model":""}`. Expect 422.
90. `[neg]` With no working microphone on the box, press Start. Expect the
    capture card to show the 400 message
    `the default input device could not be opened for recording (...)` and
    **no new session row in History** (the preflight runs before persistence).
91. Press Start with a valid setup. Confirm the card flips to Recording, the
    header shows the Capturing badge, and the elapsed clock ticks from about 0.
92. `[neg]` In a second tab, press Start again. Expect the error
    `a session is already running` inside the capture card.
93. `[obs]` While recording, confirm the level meter moves (that is
    `/ws/audio/live-level`) and `<m:ss> recorded` climbs.
94. `[obs]` While recording, confirm the live logs pane fills and the live
    transcript pane shows segments.
95. `[obs]` Navigate to History and back to the Dashboard mid-session. Confirm
    the transcript pane re-seeds with everything said so far (that is
    `GET /api/session/{id}` seeding, not the socket).
96. Press Stop. Confirm the amber `Finalizing…` state and a return to the idle
    form.
97. `[neg]` In a second tab, press Stop again after the first tab already
    stopped. Confirm the 409 path reports the ended session rather than a raw
    error.
98. `[api-only]` `POST /api/session/stop` with nothing running. Expect 409
    `no active session`.

### J. History page

99. `[obs]` Confirm the empty state reads "No sessions recorded." on a fresh
    install.
100. Tick one row. Confirm Delete enables and Merge stays disabled.
101. Tick a second row. Confirm Merge enables.
102. Click Merge selected, read the confirm text, cancel, confirm nothing changed.
103. Accept the merge. Confirm it navigates to a new session whose transcript
     contains both parts in order and whose audio player works.
104. `[neg]` Merge two sessions where only one has stored audio. Confirm the
     merge still succeeds but the merged session has **no** audio player: audio
     merging degrades silently.
105. `[neg]` Start a capture, then in another tab try to merge that running
     session with a finished one. Expect 409 `cannot merge a running session`.
106. `[api-only]` `POST /api/session/merge` with `{"ids":["<real>","bogus"]}`.
     Expect **409 `merge needs at least 2 sessions`**, not a 404 naming the bad
     id.
107. `[api-only]` `POST /api/session/merge` with `{"ids":[]}`. Expect the same 409.
108. Click Delete selected, read the confirm
     "Delete N session(s)? This also removes their audio.", cancel, confirm
     nothing was deleted.
109. Accept the delete. Confirm the rows disappear, and that opening the deleted
     session's URL shows the session page's error banner (backed by a 404).
110. `[api-only]` `POST /api/session/delete` with `{"ids":["bogus"]}`. Expect
     **200 `{"ok":true}`**: unknown ids are not reported.
111. `[neg]` Start a capture, then in another tab select that running session in
     History and delete it. Expect **200 ok**, and confirm the session is
     **still there**: the running session is silently skipped and you are not
     told.

### K. Session page, transcript and versions

112. `[neg]` Open `/sessions/does-not-exist`. Confirm a readable error banner
     (backed by 404 `session not found`), not a blank page.
113. Open a real session. Confirm Transcriptions is open and Transcript and
     Summary are folded on a first visit.
114. Click the `original` row. Confirm the transcript below switches to it.
115. Click **Show logs** on the `original` row. Confirm the dialog shows capture
     log lines.
116. `[neg]` Click Show logs on a version produced before log storage existed, or
     on one whose run emitted nothing. Expect
     "No logs were stored for this version." (backed by 404
     `no logs stored for this version`).
117. `[api-only]` `GET /api/session/<id>/logs?version=../etc`. Expect the same
     404, **not** a 500 and not a file outside the log directory.
118. `[api-only]` `GET /api/session/<id>/transcript?version=made-up`. Expect
     **200 `[]`**, not 404.
119. Rename a speaker in place: click the label, type, press Enter. Confirm every
     row with that label updates.
120. Click a speaker, press Escape. Confirm nothing was saved.
121. Rename to an empty string. Confirm the name clears back to the raw label.
122. `[api-only]` `PUT /api/session/<id>/speakers` with `{"names":{}}`. Expect
     200 and confirm every rename is gone: this is a full replace.
123. `[api-only]` `PUT /api/session/bogus/speakers`. Expect 404
     `session not found`.

### L. Re-processing and diarization

124. `[neg]` Open a session with no stored audio. Confirm the panel is replaced
     by "No stored audio for this session - re-processing and diarization are
     unavailable."
125. `[api-only]` `POST /api/reprocess` naming that session. Expect **409
     `session '<id>' has no stored audio`**.
126. `[neg]` Clear the model in the New transcription row. Confirm the button is
     disabled with the title "Pick a model to re-process with."
127. `[api-only]` `POST /api/reprocess` with
     `{"session_id":"<id>","operation":"transcribe","provider_id":"<p>"}` and no
     model. Expect 422 `model is required for a "transcribe" job`.
128. `[api-only]` `POST /api/reprocess` with `operation: "transcribe"` and
     `provider_id: ""`. Expect 404 `unknown provider ''`.
129. `[api-only]` `POST /api/reprocess` with `{"session_id":"bogus", ...}`.
     Expect 404 `unknown session 'bogus'`.
130. `[api-only]` `POST /api/reprocess` with `operation: "diarize"` and
     `target: "made-up"`. Expect 404 `unknown transcript version 'made-up'`.
131. In the Diarize control on the session page, set `min` speakers to 5 and
     `max` speakers to 2 and submit. Confirm the request is accepted: these are
     free-form number inputs with no client validation and no server range
     check. `[neg]`
132. `[api-only]` `POST /api/reprocess` with `operation: "diarize"`,
     `target: "original"` and `{"diarization":{"mode":"remote","endpoint":null}}`.
     Expect **202**, then poll the job and confirm it ends `status: "error"`.
     Compare with step 85, where the same config is a 400. `[neg]`
133. Queue a re-transcription from the UI. Confirm the button shows "Queuing…",
     a new row appears in the Transcriptions table without a refresh, and the
     Segments cell counts up about every 1.5 seconds.
134. Click the running job's row. Confirm segments stream into the transcript
     live (that is `/ws/transcript?session_id=<id>` carrying the reprocess
     version).
135. `[neg]` Queue a run against a provider with a bad key. Confirm the row ends
     `error` and the message appears under the table.
136. Click Delete on a finished re-transcription, read the confirm, cancel,
     confirm nothing changed; accept, confirm the row goes and the page falls
     back to `original`.
137. `[obs]` Confirm the `original` row has **no** Delete button.
138. `[api-only]` `DELETE /api/session/<id>/transcript?version=original`. Expect
     409 `the original transcript cannot be deleted`.
139. `[api-only]` `DELETE /api/session/<id>/transcript` with no `version`. Expect
     422.
140. `[api-only]` `DELETE /api/session/<id>/transcript?version=<a diarize job
     id>`. Expect 404 `unknown transcript version ...`.
141. `[neg]` Queue a re-transcription, then in a second tab issue
     `DELETE /api/session/<id>/transcript?version=<that job id>` while it runs.
     Expect 409 `transcript version '<v>' is still being written`.
142. `[api-only]` `GET /api/reprocess` with no `session_id`. Expect 422.
143. `[api-only]` `GET /api/reprocess?session_id=bogus`. Expect 200 `[]`.
144. `[api-only]` `GET /api/reprocess/bogus`. Expect 404 `job not found`.

### M. Export, audio, summary

145. Open the Export menu. Confirm Text (.txt), Markdown (.md), Subtitles (.srt),
     Subtitles (.vtt), JSON (.json), plus an audio entry when the session has a
     recording.
146. Download each format. Confirm the extension, that srt and vtt carry
     timecodes, and that json parses.
147. `[neg]` On a session that captured no audio, confirm the entry reads
     "Audio (empty)", is disabled, and shows the tooltip.
148. `[obs]` On a session with no `audio_path` at all, confirm the audio entry is
     absent.
149. `[api-only]` `GET /api/session/<id>/export?fmt=pdf`. Expect 404
     `unknown format 'pdf'`.
150. `[api-only]` `GET /api/session/bogus/export?fmt=pdf`. Expect **404
     `unknown format 'pdf'`**, not `session not found`: the format is checked
     first.
151. `[api-only]` `GET /api/session/bogus/audio`. Expect 404 `session not found`.
152. `[api-only]` `GET /api/session/<a session with no WAV>/audio`. Expect 404
     `no audio for session`.
153. `[neg]` Select a **re-transcription** version, then export. Confirm the
     downloaded file contains the **original** transcript, not the selected
     version. This is a real bug (see SUSPECTED ISSUES 1).
154. Open the Summarize dialog, pick an LLM provider and model, run it. Confirm
     the summary renders and survives a reload.
155. `[obs]` Pick a reasoning model, confirm the effort dropdown appears with the
     config's levels; pick a non-reasoning model, confirm it disappears.
156. `[neg]` Try to summarize with a Deepgram or AssemblyAI provider. Confirm the
     dialog does not offer them; `[api-only]` force it with
     `POST /api/session/<id>/summarize` and expect 400
     `provider is not an LLM provider`.
157. `[api-only]` `POST /api/session/<id>/summarize` with `{"provider_id":"x",
     "model":""}`. Expect 422.
158. `[api-only]` Summarize a session that captured audio but produced no
     transcript. Expect 400 `session has no transcript`.
159. `[neg]` Summarize with an LLM provider whose key is wrong. Expect **502**
     carrying the vendor's own message, surfaced in the dialog.
160. `[neg]` Select a re-transcription version and summarize. Confirm the summary
     was built from the **original** version. Same bug as step 153.

### N. Video generation

161. Open the Generate video dialog from the Summary section. Confirm the prompt
     is seeded from the stored summary and is editable.
162. `[obs]` Change the model. Confirm the Length, Resolution and Aspect ratio
     controls change with it, and that a model taking no duration shows no
     Length control.
163. Submit. Confirm a queued job row appears and the page polls until it becomes
     done or error.
164. `[neg]` Clear the prompt entirely and submit. Expect 400 `prompt is empty`.
165. `[api-only]` `GET /api/video/models` with no `provider_id`. Expect 422.
166. `[api-only]` `GET /api/video/models?provider_id=<an openai row>`. Expect 400
     `provider cannot generate video`.
167. `[api-only]` `GET /api/video/models?provider_id=bogus`. Expect 404
     `provider not found`.
168. `[api-only]` `GET /api/video/models?provider_id=<an xai row>`. Expect **200
     `[]`**: xAI publishes no video catalogue, and the dialog builds controls
     from `capabilities.yaml` instead.
169. `[api-only]` `POST /api/video` with `{"session_id":"bogus", ...}`. Expect 404
     `unknown session 'bogus'`.
170. `[api-only]` `POST /api/video` with a non video provider. Expect 409
     `provider '<name>' cannot generate video`.
171. `[api-only]` `POST /api/video` with `duration: 9999` on a model that only
     accepts 4 and 8 seconds. Expect **202**: the value is not checked against
     the model's list, and the job fails later upstream. `[neg]`
172. Play a finished video in the session page. Confirm it streams from
     `/api/video/<job>/content`.
173. `[api-only]` `GET /api/video/<a running job>/content`. Expect 409
     `video is not ready`.
174. `[api-only]` Delete the MP4 from disk, then `GET /api/video/<job>/content`.
     Expect 404 `video file is missing`.
175. `[api-only]` `GET /api/video/bogus`. Expect 404 `job not found`.
176. Delete a finished video job from the UI. Confirm the row and the file go.
177. `[neg]` Delete a **running** video job, then watch the logs. Confirm the
     background task keeps polling upstream for a job row that no longer exists.
178. `[api-only]` `GET /api/video` with no `session_id`. Expect 422.

### O. WebSockets

179. `[obs]` With the Dashboard open and nothing recording, confirm the transcript
     pane says "Waiting for transcript events…" and the logs pane says
     "Logs appear here while a session is recording..." Both sockets are open and
     correctly silent.
180. `[obs]` Start a capture. Confirm both panes fill, and that the logs pane's
     first burst is the replayed ring buffer history.
181. `[obs]` While a capture runs, queue a re-transcription of an **older**
     session. Confirm its lines do **not** appear in the Dashboard logs pane and
     its segments do **not** appear in the Dashboard transcript pane, but that
     both do appear on that older session's own page.
182. `[obs]` Open a session page. Confirm `/ws/transcript?session_id=<id>`
     carries interims and gap markers too (interims render dimmed, gaps render as
     an amber "Lost audio" line).
183. `[neg]` Stop the backend. Confirm both stream dots go amber
     ("reconnecting…") rather than straight to red, and recover to green on their
     own once it is back.
184. `[neg]` **Delete the `loreline_token` cookie without navigating**, then
     watch the Dashboard's stream dots. Expect them to stay amber and reconnect
     forever, because the server's 1008 rejection reaches the browser as a failed
     handshake. Confirm you are only bounced to `/login` once the 5 second health
     poll fires, and that nothing in the socket layer causes that bounce. This is
     SUSPECTED ISSUES 2.
185. `[api-only]` Open `ws://10.10.50.55/ws/logs` with no cookie using a WS
     client. Confirm the handshake is refused rather than a 1008 close frame
     being observable.
186. `[neg]` In Settings > Client click Test to open `/ws/audio/level`, then
     delete the auth cookie and click Test again. Confirm the button flips back
     from Stop to Test with **no message at all**: that socket is a raw
     `WebSocket` with no retry and no close-code inspection.
187. `[api-only]` Open `ws://10.10.50.55/ws/transcript?session_id=bogus`.
     Confirm the socket **accepts** and then stays silent forever: an unknown
     session id is not an error.
188. `[neg]` Open Settings > Client and click Test to open `/ws/audio/level`
     **while a capture is running on the same device**. Confirm what happens: on
     some hardware the second reader fails outright, which is exactly what the
     sibling route exists to avoid. Nothing in the backend prevents this. This is
     SUSPECTED ISSUES 6.

---

## SUSPECTED ISSUES

Ordered roughly by how much they matter to a GM using the app.

### 1. Export and Summarize silently ignore the selected transcript version
`src/loreline/web/routes/sessions.py:262` and `src/loreline/web/routes/sessions.py:223`

Both call `canonical_transcript(...)`, which is hard wired to
`ORIGINAL_VERSION` (`src/loreline/export.py:56-58`). Neither route accepts a
`version` parameter, and `frontend/src/lib/api.ts:210` builds the export URL with
`fmt` only. So a GM who re-transcribes a session with a better model, selects
that version on the page, and then exports or summarizes, gets the **original**
capture's text with no indication anything was substituted. The version selector
is the whole point of the Transcriptions table, and two of the three things you
would do with a better version ignore it.

### 2. WebSocket auth rejection is invisible to the client, and the client retries forever
`src/loreline/web/routes/transcript_ws.py:46`, `routes/logs_ws.py:30`,
`routes/audio.py:73`, `routes/audio.py:122`

All four call `ws.close(code=WS_1008_POLICY_VIOLATION)` **before** `ws.accept()`.
An ASGI close before accept is translated by the server into an HTTP 403
rejection of the handshake, so the browser never observes code 1008: it sees a
generic connection failure indistinguishable from the backend being down.
`frontend/src/lib/ws.ts:37-60` then reconnects forever with backoff. An expired
cookie therefore produces an endless reconnect loop against a server that will
never accept, and the only thing that rescues the user is the unrelated 5 second
health poll's 401 (`frontend/src/lib/api.ts:58-65`). Either accept and then close
with 1008, or have `connect()` stop retrying and signal auth failure.

### 3. `/healthz` is unauthenticated and leaks operational detail
`src/loreline/web/routes/system.py:116-150`

The route is deliberately open for external pollers, but the payload is not just
liveness: it includes the exact `version`, `disk_free_bytes` and
`disk_total_bytes`, the `active_session_id`, the configured
`diarizer_endpoint` (an internal URL the operator typed), `diarizer_detail`, and
`stt_error`, which is documented as carrying the vendor's own words verbatim
(`system.py:100-104`), for example "OpenAI: You have no credits remaining." On a
LAN-published port this is readable by anything on the network. A minimal
unauthenticated subset plus an authenticated full payload would keep the poller
working without publishing all of that.

### 4. Login rate limiting keys on the peer IP, which is the proxy
`src/loreline/web/routes/auth.py:26`, `src/loreline/web/auth.py:175-207`

`key = request.client.host`. In the bundled compose stack every request arrives
through Caddy, so **every** client shares one key, and five wrong guesses from
anywhere lock out the whole table for 30 seconds. `X-Forwarded-For` is never
consulted, even though this module already has the trusted-proxy machinery to do
it safely (`auth.py:66-89` is used for `X-Forwarded-Proto` and nothing else).
Separately, `self._attempts` (`auth.py:188`) is only ever pruned on a successful
login or an expired lockout that is subsequently checked, so a spray across many
source IPs grows the dict without bound.

### 5. Deleting or merging sessions is not guarded against in-flight jobs
`src/loreline/web/routes/sessions.py:289-301` and `sessions.py:304-374`

`delete_sessions` skips only the **live capture** (`sessions.py:295-296`). A
session with a queued or running reprocess job, or a running video job, is
deleted out from under the background task, which then keeps inserting transcript
rows (`reprocess/jobs.py` `_run`) and writing MP4 files for a session row that no
longer exists. `merge_sessions` has the same hole in the other direction: sources
are read one at a time (`sessions.py:315`) and then copied over many awaits with
no transaction, so a concurrent delete of a source mid-merge produces a partial
merge that no error reports. `delete_version` does have exactly this guard
(`reprocess/jobs.py:311-321`); session delete does not.

### 6. `/ws/audio/level` opens a second reader on the capture device with no guard
`src/loreline/web/routes/audio.py:66-103`

The route unconditionally constructs a `SoundDeviceSource` on the named device.
The sibling route's own docstring (`audio.py:106-117`) states that "a second
simultaneous reader on the same device fails outright on some hardware (see the
mic-resampling fix)", which is why `/ws/audio/live-level` exists. Nothing stops a
GM from opening Settings > Client and clicking Test while a capture is running,
which is precisely the scenario that motivated the other route. It should refuse
while `manager.current_session_id()` is set, or relay the live meter instead.

### 7. Alert channel test returns 200 for an unknown id, unlike every sibling
`src/loreline/web/routes/system.py:340-343` versus `system.py:317` and `system.py:333`

`test_alert_channel` returns `{"ok": false}` for a channel id that does not
exist (`monitoring/alerts.py:174-176` returns False for `channel is None`),
while PUT and DELETE on the same id both answer 404. A client cannot distinguish
"the channel is misconfigured" from "the channel is gone", and the UI renders
both as "Test failed".

### 8. Service routes answer 503 for what are really 400 and 404 conditions
`src/loreline/web/routes/system.py:371-377` and `src/loreline/services.py:120-131`

`DockerUnavailableError` is the only exception the route translates, and
`ServiceManager` reuses it for four unrelated causes: Docker not configured,
Docker unreachable, **unknown service name**, and **service not in the
controllable allowlist**. All four surface as 503, so "you asked for a service
that does not exist" reads to the client as "the Docker daemon is down". The
allowlist rejection in particular is a 403 or 400, not a service outage.

### 9. `tail` on the service logs route is completely unvalidated
`src/loreline/web/routes/system.py:381`

`tail: int = 200`, with no `Query(ge=..., le=...)`. Negative values and
arbitrarily large values are stringified and forwarded to the Docker API
(`services.py:142-152`). The frontend always sends 200, so this is only
reachable by hand, but a large value pulls an unbounded container log into
memory and into a JSON response.

### 10. Alert channels and action defaults are read-modify-write with no lock
`src/loreline/web/routes/system.py:295-305`, `:308-323`, `:326-337`, `:255-270`

Each of these loads the whole config from one `kv_settings` row, mutates the
in-memory list, and writes it back. Two concurrent requests (two browser tabs, or
a create racing a delete) lose one of the two writes entirely, with no error and
no version check. The same shape applies to `PUT /api/system/defaults`, where a
partial write from one tab clobbers another tab's fields because the body is a
full replace and every omitted field defaults to `""` (`schemas.py:110-133`).

### 11. `_diarizer_probe` is process-global mutable state
`src/loreline/web/routes/system.py:53` and `:56-66`

A module level tuple memo, written from a request handler with an explicit
`global` statement. It is keyed by endpoint so a changed endpoint is not served
stale, but it is not scoped to the app instance, so it survives across
`create_app()` calls in a single process, and it is written without any
synchronisation. Single event loop makes the race benign today; the lifetime
issue is real.

### 12. `ProviderCreate` has no constraints on any field
`src/loreline/web/schemas.py:24-40`

`name` accepts the empty string; `sample_rate` accepts 0 and negatives and is
then handed to the capture source and every backend
(`session/manager.py:571-573`); `language` accepts any string; `favorite_models`
is an unbounded list of unbounded strings; `base_url` is not validated as a URL.
The UI's Save button guards only the name (`settings/providers/+page.svelte:1058`),
and there is no `sample_rate` control at all, so the rest is reachable only by
API, but nothing on the server side would stop it.

### 13. An API key can be set but never cleared
`src/loreline/web/routes/providers.py:89-90` and `:109-110`

Both create and update write the secret only `if body.api_key:`, so an empty
string is a no-op rather than a clear. There is no `DELETE
/api/providers/{id}/secret`. The only way to remove a stored key is to delete the
provider row entirely (`providers.py:121-122`). Relatedly,
`POST /api/providers/{id}/secret` with `{"value":""}` (`SecretWrite` has no
`min_length`, `schemas.py:43-46`) writes an empty secret that `hint()` reports as
unset (`secrets.py:88-90`) while `get()` returns `""`, so the UI says "no key"
while connectors send an empty credential.

### 14. The same invalid diarization config is a 400 on start and a 202 on reprocess
`src/loreline/web/routes/sessions.py:95-96` versus `src/loreline/web/routes/reprocess.py:25-34`

`SessionManager.start` builds the diarizer inside the request and translates the
`ValueError` from `create_diarizer` (`diarization/provider.py:80-83`) into a 400
(`session/manager.py:552-560`). `ReprocessManager.enqueue` builds nothing: it
creates the row and spawns the task (`reprocess/jobs.py:257-263`), so
`{"mode":"remote","endpoint":null}` is accepted with 202 and surfaces minutes
later as a failed job. The validation should happen at enqueue time in both.

### 15. Alert channel creation does not validate the type's required field
`src/loreline/web/routes/system.py:295-305`, `src/loreline/web/schemas.py:221-233`

`AlertChannelWrite` makes `topic`, `chat_id` and `url` all optional and
independent of `type`. An `ntfy` channel with no topic, or a `webhook` with no
url, is created with 201 and then silently never delivers
(`monitoring/alerts.py:196-201` falls through to `return False`). The UI enforces
this client side (`settings/alerts/+page.svelte:138-140`); the schema does not.
A discriminated union or a model validator belongs here.

### 16. Bulk session delete reports nothing about what it did
`src/loreline/web/routes/sessions.py:289-301`

Always `{"ok": true}`. Unknown ids delete nothing and are not reported; the
running session is silently skipped and is not reported. From the History page a
GM who selects the running session plus two others sees the same success toast
and then finds one row still present with no explanation.

### 17. Post-auth SSRF surfaces
`src/loreline/web/routes/system.py:177-199`, `system.py:295-305`,
`src/loreline/web/routes/providers.py:164-184`

Three routes make the server issue outbound HTTP to a URL the caller chose, with
no scheme or host allowlist: the diarizer probe's `endpoint`, an alert channel's
`server` or `url`, and `POST /api/providers/models`'s `base_url`. All are behind
`require_auth`, and the diarizer probe's docstring argues it is the same exposure
the stored default already has. It is still worth recording that a single leaked
session cookie turns the app into an internal network scanner, and that the
probe's graded `status` and `detail` are a useful oracle for that.

### 18. Log history replay is not raced against disconnect
`src/loreline/web/routes/logs_ws.py:41-46`

`stream_until_disconnected` guards the live loop but not the history replay above
it, which is a plain `for record in history: await ws.send_text(...)`. Only 500
lines, so the practical exposure is small, but it is the same class of wedge that
`ws_util.py` exists to prevent.

### 19. Export sets `Content-Disposition` from a path parameter
`src/loreline/web/routes/sessions.py:269`

`filename="{session_id}.{ext}"` is interpolated without quoting. `session_id` is
validated by the preceding database lookup (`sessions.py:259-260`) and every
stored id is a 32 char uuid hex, so this is not exploitable today. It is one
schema change away from a header injection, and the surrounding code otherwise
validates path segments explicitly (`persistence/log_store.py:107-111`).

### 20. Deleting a running video job does not cancel its task
`src/loreline/web/routes/video.py:102-110`, `src/loreline/video/jobs.py:174-177`

`VideoManager.delete` removes the file and the row but never touches
`self._tasks[job_id]`. The background poller keeps running for the remaining
minutes and then calls `self._videos.update(job)` on a row that no longer exists.
Contrast `aclose` (`jobs.py:165-172`), which does cancel.

### 21. Several destructive calls have no client error handling at all
`frontend/src/routes/settings/providers/+page.svelte` `remove()`,
`frontend/src/routes/settings/alerts/+page.svelte` `deleteChannel()`,
`frontend/src/lib/SessionSummary.svelte` `deleteVideo()`,
`frontend/src/routes/+layout.svelte` `logout()`

Each awaits its API call outside any try/catch. A failure is an unhandled
promise rejection: nothing is rendered, no banner appears, and the list is not
reloaded, so the row the user just confirmed a destructive action on stays on
screen looking as though nothing happened. Every one of these sits behind a
confirm dialog, which makes the silence worse: the user has already been told
the action is irreversible. `logout()` failing leaves the user apparently
logged in, with `authed` never cleared and no redirect.

### 22. Poll loops swallow their own failures
`frontend/src/routes/sessions/[id]/+page.svelte` `refreshJobs()`,
`frontend/src/lib/SessionSummary.svelte` `refreshVideoJobs()`

Both are wrapped in a try/catch on the `onMount` call only. The `setInterval`
that re-runs them (1.5 s for reprocess jobs, 5 s for video jobs) calls them
bare, so a failure during polling is a silent unhandled rejection while the
interval keeps firing. A backend that starts refusing mid-job leaves the row
frozen at its last state with nothing indicating anything is wrong.

### 23. The device level meter socket reports nothing when auth fails
`frontend/src/routes/settings/client/+page.svelte` `startMeter()`

Unlike every other socket this one is a raw `new WebSocket(...)` with no
`ws.ts` retry and no close-code inspection: `onclose` just sets
`metering = false`. The server's 1008 rejection for a missing or expired cookie
therefore flips the Test button back to its resting label with no message, which
is indistinguishable from the user having clicked Stop. The route does have a
proper in-band error channel for device problems
(`src/loreline/web/routes/audio.py:96-99` sends `{"error": ...}`), so auth is the
one failure mode that is silent.
