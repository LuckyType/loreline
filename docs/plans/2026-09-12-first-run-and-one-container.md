# Plan: a one-container default, and a first run that configures itself

Goal: someone who has never seen Loreline gets it running and transcribing
without editing a file, reading 371 lines of deployment prose, or knowing what
Caddy, a docker socket proxy or an updater profile are. One command, then a
browser that asks for the three things it actually needs.

Two halves, buildable in parallel:

* **A, the app:** a first run that claims the instance with a password and then
  walks the GM through a provider and a capture source.
* **B, the deployment:** a minimal path that runs the app alone, with the
  current six-service stack kept as the appliance it is, and a README that
  leads with the short way in.

## What is actually wrong today

Nothing here is broken; it is heavy. `deploy/install.sh` already installs
Docker, clones, generates a password, writes `.env` and brings the stack up in
one piped command, and it prints the generated password at the end. The costs
are that it is the only documented way in, that it brings six services for a
job the app can do alone, that it needs a checkout on the host, and that the
README spends 371 of its 483 lines on deployment before a reader has seen the
app work.

Two facts constrain every decision below, both checked in the code:

1. **Today's default is secure.** `install.sh` generates a password when the
   operator does not choose one (`gen_password`, line 92, printed at line 558).
   A `docker run` path that leaves the instance open would be a regression, not
   a simplification. This is the reason half A is a claim flow and not merely a
   friendly onboarding screen.
2. **`auth_password=""` means "auth disabled" to 1200 tests.** The shared
   fixture in `tests/conftest.py:19` builds `Settings(auth_password="")`, so a
   gate that blocks an unconfigured instance would fail most of the suite. The
   unclaimed state must therefore be its own setting, off in that fixture,
   rather than inferred from an empty password.

---

## Half A: first run

### The state machine

Three states, and the app must be able to tell them apart without guessing:

| State | How it is reached | What the API does |
| --- | --- | --- |
| **Claimed** | `LORELINE_AUTH_PASSWORD` set, or a password in the secret store | Exactly what it does today |
| **Unclaimed** | `first_run_setup` on, and no password from either source | Only the setup routes and `livez` answer; everything else is 403 |
| **Open** | `first_run_setup` off and no password | Exactly today's behaviour, which is what the tests and a dev box rely on |

`first_run_setup: bool = True` on `Settings`, so a real deployment is secure by
default and `LORELINE_FIRST_RUN_SETUP=false` is the deliberate way back to an
open dev box. `tests/conftest.py` sets it False in the shared fixture, one line,
and the tests that exercise setup build their own settings.

### Claiming

`ensure_jwt_secret` in `src/loreline/web/auth.py` is the precedent to copy: it
mints a secret, persists it through `SecretStore`, and assigns it back onto the
live `Settings`. The password follows the same shape, with one addition, a
**setup code**:

* At startup, while unclaimed, mint a setup code if the store has none, persist
  it, and log it at every startup so a restart shows it again. Delete it on
  claim.
* `POST /api/setup/claim` takes the code and the chosen password, refuses a
  wrong code with the same backoff the login route uses, writes the password to
  the store, updates the live settings and issues the session cookie, so the
  browser lands signed in.
* Without the code, anyone else on the LAN or the tailnet could claim the
  instance first and own the transcripts and the API keys. `docker run` shows
  the log on the terminal that started it; for a detached stack it is
  `docker compose logs app`. Both go in the README.
* `GET /api/setup/state` is unauthenticated and says only whether the instance
  is claimed and which steps remain. It must never reveal the code.

Password rules: a minimum length, and the same field on screen twice, since a
typo here locks the instance out with no recovery but a shell on the host.
Document the recovery: set `LORELINE_AUTH_PASSWORD` and restart, which wins
over the store.

### The wizard

`/setup` in the SPA, rendered bare like `/login` with no shell chrome, reached
by a redirect from anywhere when the instance is unclaimed.

1. **Password.** Claim, as above. Skipped when `LORELINE_AUTH_PASSWORD` is set.
2. **A provider.** The vendor list, a key, and the existing health probe to say
   whether the key works before the GM leaves the page. Reuse the provider
   wizard's own components rather than a second copy; if they cannot be reused
   as they stand, say so in the report rather than forking them. Seed the
   transcription defaults from this row so the capture card is usable
   immediately.
3. **How to record.** The three routes now exist and a new user knows none of
   them: a server microphone when the host has one, this device's microphone
   when the context is secure, and importing a recording. Offer what is
   actually available, explain what is not, and link the Tailscale section for
   the browser path.

Every step after the claim is skippable and resumable, and the same steps stay
reachable from Settings afterwards. A GM who skips everything lands on a
dashboard that works.

### Tests

The three states and the transitions between them; a wrong code refused and
rate limited; the code absent from `GET /api/setup/state`; a claim writing a
password that a subsequent login accepts; `LORELINE_AUTH_PASSWORD` winning over
a stored one; and the whole existing suite still green with the one fixture
line.

---

## Half B: one container

### The minimal path

The app alone, with SQLite in a volume, is a complete Loreline: cloud STT,
cloud summaries, import, campaigns, exports. What the other five services add
is a reverse proxy, a restricted docker socket for Settings > Services, the
in-app Update button, and self-hosted models. All four are genuinely optional
and three already degrade on their own: `docker_api` blank disables the
Services page by design (`settings.py:98`), and the Update button already
explains a Docker deployment without the updater profile.

Deliver:

* A documented `docker run` one-liner: a published port, a `data` volume, and
  nothing else. No checkout, no `.env`, no password argument, because half A
  asks for it in the browser.
* A minimal compose file for people who prefer one, small enough to read in
  full, with the same two facts in it.
* **Verify the degradation rather than assume it.** Run the app with no
  `docker_api`, no updater and no Caddy, and check each affected surface says
  what is missing and why instead of failing: Settings > Services, the Update
  and Roll back buttons, the Secure cookie note, and the client microphone's
  insecure-context message. Fix what reads badly. This is the part most likely
  to contain the real work.
* Keep the six-service stack exactly as it is, named as the appliance path, and
  keep `install.sh` working unchanged.

### The README

It is the front door and it currently opens onto deployment. Restructure so a
reader meets, in order: what it does, the shortest way to see it running, then
the appliance stack, then everything else. Nothing is deleted; the long-form
Docker Compose, source and systemd, Bluetooth and Tailscale sections all stay,
further down. The quick path names the setup code and where to read it.

### Tests

An integration test that the app starts and serves with no optional service
configured, and that the surfaces above answer with an explanation rather than
a 500.

---

## Out of scope, worth naming

* A published release tag or a versioned image for the quick path; `latest` is
  what the deploy path already follows.
* Any change to `install.sh` beyond keeping it working.
* Recovering a lost password without host access.
