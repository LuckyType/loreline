---
status: accepted
date: 2026-09-12
---

# The first run claims the instance

## Context

Getting Loreline running today means `deploy/install.sh`: it installs Docker,
clones the repo, generates a password, writes `.env` and brings six services
up, and prints the password at the end. Nothing about that is broken. It is
simply the only documented way in, it needs a checkout on the host, and it
brings a reverse proxy, a docker socket proxy and an updater for a job the app
does alone.

The obvious simplification is a `docker run` with a published port and a
volume. The moment that exists, a new question does too: what is the password
of an instance nobody wrote a `.env` for? Two answers were available and both
are wrong.

**"No password until the operator sets one"** is a regression, not a
simplification. `install.sh` generates one precisely so that the default is
secure (`gen_password`, line 92, printed at line 558). An app that comes up
open on a LAN holds a table's transcripts, its recordings and its provider API
keys, and the failure is silent: everything works, and somebody else can read
it.

**"Ask for a password in the browser on first visit"** is better and still not
enough on its own. Whoever reaches the box first would own it, and on a LAN or
a tailnet that is not a thought experiment - it is any device on the network,
including one that is not yours.

A second fact constrains the shape of the fix. `auth_password=""` has meant
"auth is disabled" since the first commit, and `tests/conftest.py` builds
`Settings(auth_password="")` for a suite of about 1300 tests that all depend on
it. So an unconfigured instance cannot be recognised by its empty password: a
box with no password is either a dev machine that wants none or a brand new one
nobody has claimed, and those two want opposite treatment.

## Decision

### 1. Three states, named, and none of them inferred

`first_run_setup: bool` on `Settings`, defaulting to **True**, is the fact the
empty password cannot carry:

| State | How it is reached | What the API does |
| --- | --- | --- |
| **claimed** | `LORELINE_AUTH_PASSWORD` set, or a password in the secret store | Exactly what it did before |
| **unclaimed** | `first_run_setup` on, no password from either source | Only the setup routes and `livez` answer; everything else is 403 |
| **open** | `first_run_setup` off, no password | Today's auth-disabled behaviour |

Defaulting to on is what makes a bare `docker run` secure. `false` is the
deliberate way back to an open dev box, and it is one line in
`tests/conftest.py` - set in the environment rather than in the shared fixture,
because about a dozen test modules build their own `Settings` and the
environment covers all of them at once while an explicit argument wins over it
for the tests that exercise the first run.

### 2. The setup code is the whole security argument

Without it, the claim is "whoever gets here first". So while unclaimed, startup
mints a code if the store has none, persists it through `SecretStore` beside
the JWT secret, and logs it at **every** startup, so a restart shows it again
rather than leaving the operator with a code that no longer works. It is
deleted on a successful claim, and also at the startup of an instance that was
claimed by setting `LORELINE_AUTH_PASSWORD`, which never goes through the claim
route at all.

It exists in exactly one place a human can read: that log line. It is not in
`GET /api/setup/state`, not in any error body, not in any log line after the
claim, and the refusal for a wrong code says nothing about the right one - no
length, no prefix, no "close". Tests assert each of those.

The alphabet is 31 symbols with no `0`, `O`, `1`, `I` or `L`, eight of them,
printed as `ABCD-EFGH`. Just under 40 bits, which against the backoff below is
not guessable, and readable aloud from a terminal into a phone. Case and the
grouping hyphen are presentation and are folded away before anything is
compared, because telling somebody their code is wrong when they left the
hyphen out is a bug.

### 3. The claim reuses the login route's backoff, object and all

`POST /api/setup/claim` takes the code and the password and is rate limited by
`state.login_limiter` - the same instance the login route uses, so guessing
cannot win a fresh budget by alternating between the two routes - keyed by
`client_address`, which reads `X-Forwarded-For` only from a peer inside
`LORELINE_TRUSTED_PROXIES`. Behind the bundled Caddy every browser at the table
arrives from one container address, and a limiter keyed on that would be global:
five wrong codes from anyone in range would hold the claim shut for everybody.

The password is checked **after** the code, so mistyping the password does not
spend an attempt from a budget that exists to slow down guessing the code.

### 4. A claim leaves the browser signed in

The claim issues the session cookie itself. Bouncing to a login form to retype
a password chosen one second earlier is a step that exists only because the
code was written in the other order. The cookie's attributes come from
`set_session_cookie`, extracted from the login route so the two cannot drift:
`Secure` is decided by `client_uses_https` rather than set unconditionally,
which is what keeps the plain-HTTP LAN path working.

### 5. The password is asked for twice, with a floor, because there is no recovery

It is stored and never shown again, so a typo locks the instance out with
nothing but host access to get back in. Both rules are enforced on the server
as well as on the form, so the guarantee does not depend on which client is
calling. The way back in is `LORELINE_AUTH_PASSWORD` on the host plus a
restart, which wins over the stored one - the same precedence every other
secret in this app already has, and it is documented on the wizard's own
password field rather than only in a README nobody is reading at that moment.

### 6. The gate is one middleware, not a dependency per route

An unclaimed instance has no password, so `require_auth` is a no-op on every
route: a route that forgot to add a first-run dependency would serve
transcripts to the LAN. `FirstRunGate` is raw ASGI rather than a
`BaseHTTPMiddleware` because the WebSocket routes need refusing too, and
`BaseHTTPMiddleware` never sees them - and those sockets carry a session's
audio and its live transcript. It holds the live `Settings` object, which is
the one the claim mutates, so the gate opens on the request that claims the
instance rather than at the next restart.

`/api/setup/*`, `/api/system/livez` and the SPA are what it lets through. The
SPA because the wizard is a page of it, and an instance that would not serve
its own front end could never be claimed from a browser. `/docs` and
`/openapi.json` are refused, not because they leak anything, but because "only
the setup routes answer" is a rule worth being able to state without
exceptions.

`POST /api/setup/complete` is behind the gate's allowance and behind
`require_auth`, which is a no-op while unclaimed, so it refuses on its own
until the instance is claimed.

### 7. The wizard is skippable, resumable and made of the pages it replaces

`/setup` renders bare, like `/login`, and the root layout sends a visitor there
from anywhere while the instance is unclaimed. Three steps: the claim, a
provider, and which of the three recording routes this deployment offers.

Only the first is compulsory. The wizard reads where it got to from
`GET /api/setup/state` rather than remembering, so it opens on the claim, or on
the provider step, or on the recording step, depending on what is already true.
A GM who skips everything lands on a working dashboard, and Settings grows a
"Finish setup" tab that disappears once the wizard has been finished or skipped
through. Saving a provider seeds the transcription and summary defaults where
nothing is set yet, so the capture card is usable immediately rather than
opening on an empty picker, and it never overwrites a default somebody chose.

The provider step reuses the vendor list rather than copying it:
`$lib/providerCatalog` and `ProviderChoiceList.svelte` were lifted out of
Settings > Providers and both wizards now render the same vendors, in the same
order, with the same copy. The settings page's dialog itself was **not**
reusable - it is bound to that page's editing model, its favourites table, its
routing panel and its action defaults - and forking it would have produced
exactly the second copy this extraction avoids.

## Consequences

* One new setting, two new public routes, one new kv key, one middleware and
  two new secret-store entries. No migration: `kv_settings` and `SecretStore`
  already existed, so v26 is still the last migration.
* Two routes are unauthenticated by construction and are listed as such in
  `PUBLIC_OPERATIONS`. They have to be: an unclaimed instance has nobody to
  authenticate. What keeps them safe is the setup code and the backoff.
* One line of test configuration, in `tests/conftest.py`, and one existing test
  updated (the OpenAPI security list, which is a deliberate allowlist). No test
  module had to be edited to accommodate the gate.
* `deploy/install.sh` is untouched and still generates and prints a password,
  which lands the instance in the claimed state with no setup code ever minted.
* A restart of an unclaimed instance re-prints the same code. A crash loop
  prints it repeatedly, which is noisy and correct: the alternative is a code
  that changes faster than anyone can type it.

## What the deployment docs still have to say

Half B owns `README.md`, `docker-compose.yml`, the minimal compose file and
`deploy/`. Four facts belong there and are not written anywhere a reader of
those files would find them:

1. **Where the setup code is.** `docker run` shows it on the terminal that
   started it; a detached stack needs `docker compose logs app`. Both, because
   a reader who guesses wrong concludes the code does not exist.
2. **That it is reprinted at every restart** while the instance is unclaimed,
   so a lost code is a `docker restart` away.
3. **The recovery path.** `LORELINE_AUTH_PASSWORD` on the host plus a restart
   wins over the stored password, and it is the only way back into an instance
   whose password was mistyped at the claim.
4. **That the quick path needs no password argument**, because the browser asks
   for one, and that this is not less secure than `install.sh`: the instance
   refuses everything until it is claimed with a code only somebody who can read
   its log has.

## Follow-ups, deliberately not in this pass

* **Rotating the password from the UI.** The store can hold a new one and
  `_password_fingerprint` already invalidates every old cookie; what is missing
  is a form and a "confirm the current one" check. Out of scope here because
  the first run is about getting in, not about changing what you got in with.
* **More than one account.** One shared password is the whole auth model
  (`docs/adr` has never claimed otherwise) and the claim does not change it.
  Per-person sessions would be a different decision with a schema behind it.
* **A code with an expiry.** An unclaimed instance on a hostile network is
  already a mistake; a code that expires would mostly lock out the operator who
  set the box up on Friday and came back on Sunday.
