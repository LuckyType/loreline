<div align="center">

<img src="assets/icon.png" alt="Loreline" width="140">

# Loreline

Headless tabletop session transcriber. Put it in the middle of the table, get a
speaker-attributed transcript of your session.

[![CI](https://github.com/LuckyType/loreline/actions/workflows/ci.yml/badge.svg)](https://github.com/LuckyType/loreline/actions/workflows/ci.yml)
[![License: AGPL v3](https://img.shields.io/badge/license-AGPL--3.0-blue)](./LICENSE)

</div>

Loreline records a Pen & Paper session through a connected microphone and gets
a transcript from a speech-to-text vendor: streamed continuously for a model
that streams, cut into utterances by Silero VAD for one that does not.
Speaker labels are optional and come either from the vendor or from a
diarization service you host yourself. A SvelteKit web UI shows the live
transcript and carries the configuration, the logs and the health view.

The box that runs it captures and orchestrates, nothing more. Every STT and
diarization model runs on a remote endpoint, a cloud API or a service on your
LAN, so the app needs no GPU and has no GPU code path at all. It runs on Linux
x86_64 and on a Raspberry Pi (ARM64). Python 3.12 and FastAPI on the server
side, Svelte 5 and Tailwind 4 in the browser, SQLite for storage.

## Quickstart

Loreline is one container. It serves the API and the web UI on port 8000 and
needs nothing else:

```bash
docker run -d --name loreline -p 8000:8000 -v loreline-data:/app/data \
  ghcr.io/luckytype/loreline:latest
```

Open <http://localhost:8000> and the app takes it from there: it asks for a
password, then for one transcription provider and its API key, then how you
want to record.

Before the password it asks for a **setup code**, and that code is printed by
the container rather than shown on the page:

```bash
docker logs loreline          # the docker run above
docker compose logs app       # the compose form below
```

Read it there and paste it in. It exists because the instance is claimed by
whoever answers that page first, and without the code that is anyone who can
reach the port: the code is the one thing a stranger on the LAN or the tailnet
cannot see. A restart prints it again, and it stops existing the moment the
instance is claimed. Run the container in the foreground (drop `-d`) and it is
simply on the terminal in front of you.

A forgotten password is recovered only from the host: set
`LORELINE_AUTH_PASSWORD` on the container and start it again, which wins over
the stored one.

Two things worth knowing, because neither is obvious and both are good news:

- **No GPU, no sound card, no model downloads.** Every STT and diarization
  model runs on an endpoint somewhere else, so the container wants CPU and a
  network connection and nothing more. The published image covers x86_64 and
  ARM64, which includes a Raspberry Pi.
- **`localhost` is a secure context**, which is the rule browsers use to decide
  whether a page may have a microphone. So on the machine you are sitting at,
  "This device's microphone" works immediately with no certificate and no TLS
  setup: the browser records and streams to the container. Reaching the same
  container from another machine over plain HTTP does not get a microphone, and
  [Recording from a laptop](#recording-from-a-laptop) is the way round that.

Everything the app owns lives in the named volume: the SQLite database, the
recorded audio and the stored API keys. `docker rm -f loreline` and the same
`docker run` again comes back to the same sessions; deleting them takes a
`docker volume rm loreline-data`.

If you would rather keep it in a file, this is the same deployment:

```yaml
# compose.yml
services:
  loreline:
    image: ghcr.io/luckytype/loreline:latest
    ports:
      - "8000:8000"
    volumes:
      - loreline-data:/app/data
    restart: unless-stopped

volumes:
  loreline-data:
```

Then `docker compose up -d`.

That is a complete deployment, not a demo mode. [Deployment](#deployment) below
is the same app dressed as an appliance: a reverse proxy on 80 and 443, a
microphone attached to the host, self-hosted transcription and diarization, and
updates on a timer. Reach for it when you want those, not to get started.

**What one container gives up**, said plainly, because each of these is a thing
you will eventually look for and not find:

- **Settings > Services is empty.** That page manages this stack's own
  containers through a Docker API, and one container has none to manage. The
  page says so rather than showing an error, and nothing else depends on it.
- **The Update button has nothing to hand the job to.** The app is refused the
  Docker socket on purpose, so it cannot replace itself. Updating here is
  `docker pull ghcr.io/luckytype/loreline:latest` and then the same `docker run`
  again: the data is in the volume, not in the container, so nothing is lost.
  The button says this too.
- **No TLS.** Plain HTTP means the login password crosses the LAN in the clear
  and the session cookie is not marked `Secure` - which is correct rather than
  broken, since the flag matches the connection. It also means no browser
  microphone from any machine except the one running the container, until a
  proxy with a certificate every device trusts is put in front. That is
  [Recording from a laptop](#recording-from-a-laptop), and it is worth reading
  [One setting that fails silently](#one-setting-that-fails-silently) at the
  same time.
- **No self-hosted STT or diarization.** Both are separate services with model
  downloads of their own, and both live in the appliance stack. Cloud
  transcription, cloud summaries, speaker labels from the STT vendor, import,
  campaigns, search and exports are all here.

## What it does

**Capture.** Continuous recording, from a microphone on the server or from the
browser you started the session in, which is the same session either way. Silero
VAD cuts it into utterances for re-processing and for any model that does not
stream; a model that streams gets the raw feed directly and decides its own
turns, with interim text as it goes. Recording from a browser needs HTTPS and
keeps the tab tied to the session, both of which are covered in
[Recording from a laptop](#recording-from-a-laptop).

**Transcription.** Deepgram, AssemblyAI, Gemini, OpenAI and any
OpenAI-compatible endpoint you host yourself, such as Speaches or whisper.cpp.
Deepgram, AssemblyAI, Gemini and OpenAI each have both a streaming and a batch
connector; the model you pick decides which one runs. A streaming model holds
one connection open for the whole session and posts interim text as it goes;
if that connection dies it reconnects, then falls back to another streaming
provider, then hands the rest of the session to the batch path rather than
losing it. A batch model goes through a router that sends one utterance at a
time to a primary provider and fails over to a fallback.

OpenRouter transcription (Whisper, Nova, Chirp, Voxtral) is batch only. Its API
has no streaming mode, so it cannot drive a live capture and is offered for
re-processing stored audio instead.

**One row per vendor.** A configured provider is an account, not a role: a
credential, an optional base URL, a language and a shortlist of favourite
models. What that vendor can do, and at which URL with which auth scheme, is
declared in `src/loreline/capabilities.yaml`, so one OpenRouter row transcribes,
summarizes and generates video without three separate entries.

**Capability-scoped pickers.** Every provider and model list is filtered to what
the model can actually do, so a chat model is never offered for transcription.
The "Only show compatible models" toggle under Settings, Providers turns that
off when you need a model too new to be listed, or a self-hosted server with its
own naming. It is on by default.

**Diarization.** Inline from the STT vendor's own speaker labels, a self-hosted
sherpa-onnx service that ships with this repo, OpenAI's batch diarization model,
or off. The self-hosted service remembers a session's voices, so a speaker
keeps one label across the whole session rather than just within one call.

**Sessions.** SQLite persistence with the audio kept per session. A stored
session can be re-transcribed or re-diarized later with a different provider or
model. A re-transcription fills its version in as it runs and can be stopped
part way through: what it wrote is kept, so the partial result can be read and
then deleted. Speakers can be renamed, and a summary can be generated by OpenAI,
OpenRouter, Gemini or any OpenAI-compatible chat endpoint such as Ollama, LM
Studio or vLLM. An OpenRouter row can also constrain its routing: sort upstream
providers by price, throughput or latency, refuse providers that store or train
on the transcript, and require Zero Data Retention.

**Campaigns.** A session belongs to a campaign, and the campaign is where the
value of a transcript collects: its sessions in order, its glossary, full-text
search across all of them, a player-facing recap per session, the characters,
places, quests and decisions each session named, and a "previously on" for the
next one. Recaps are a separate text from summaries because they are for the
players rather than the GM, and the instructions behind them can be set per
campaign. Search runs on SQLite's FTS5 and falls back to an unranked scan on a
build without it.

**Video.** Turn a session summary into a video prompt and generate a clip
through OpenRouter's video models, with the length, resolution and aspect ratio
each model offers. The job runs in the background and the finished file is
stored with the session and plays in the UI. Settings holds the default provider
and model.

**Import.** A recording you already have - a phone memo, a handheld recorder's
card, a Discord rip - is uploaded and stored as a session that is
indistinguishable from a captured one: m4a, mp3, ogg, opus, webm, flac or wav
in, and everything above works on it unchanged. The upload can start its first
transcription in the same request, or you can decide the model later. This is
what lets you use Loreline on a laptop with no box and no microphone. Decoding
anything but a 16-bit PCM WAV needs `ffmpeg` on the server;
`LORELINE_IMPORT_MAX_MB` and `LORELINE_IMPORT_MAX_HOURS` cap what one upload
may cost.

**Exports.** txt, md, srt, vtt, json.

**Web UI.** A SvelteKit SPA served by FastAPI: live transcript, session history,
campaigns, provider and glossary config, live logs, health and alerting. The dashboard's
transcript and log panels follow the running capture only. Every transcript
version, the live capture and each re-processing run, keeps its own log file,
readable from the session page.

**Ops.** JWT cookie auth, push alerts, an open `/api/system/livez` liveness
probe next to the authenticated `/api/system/healthz` snapshot, and
self-update on the source deployment.

## Configuration

Settings come from environment variables with the `LORELINE_` prefix, or from a
`.env` file. See [`.env.example`](./.env.example). API keys entered in the UI
are stored in `data/secrets.json` with mode `0600`; a `LORELINE_SECRET_<NAME>`
environment variable overrides the stored value.

## Deployment

Three paths, in order of how much of the host they touch.

- **One container**, the [Quickstart](#quickstart) above. Nothing to check out,
  nothing to configure, and it is the same image every path below runs.
- **Docker Compose**, the appliance. `deploy/install.sh` installs Docker if it
  is missing and brings the whole stack up, so nothing else needs installing on
  the host. This is the recommended path for a box that will sit on the table
  and keep running.
- **Source and systemd**, with no container runtime at all, for boxes where
  that is a hard requirement. See [Source and systemd](#source-and-systemd).

[Recording from a laptop](#recording-from-a-laptop) applies to all three, and
so does [Updating](#updating).

### Docker Compose

The appliance: the container from the quickstart plus a reverse proxy, a
read-only window onto the Docker API so Settings > Services works, and the
optional self-hosted services. One command installs the lot:

```bash
curl -fsSL https://raw.githubusercontent.com/LuckyType/loreline/main/deploy/install.sh | bash
```

That is the whole install. The script clones the repo to `/opt/loreline` and
re-runs itself from there, so there is nothing to check out first. Set `APP_DIR`
to install somewhere else. If that directory is already a Loreline checkout it
uses that one and clones nothing, which makes re-running the same line on a box
that is already set up safe: it keeps your existing `.env` unless you tell it
otherwise. If the directory exists and holds something else, it stops rather
than writing into it.

Piping a script straight into a shell is worth being able to opt out of, so the
long way is equally supported and behaves identically. Read it first, then run
the copy you read:

```bash
git clone https://github.com/LuckyType/loreline.git /opt/loreline
cd /opt/loreline
bash deploy/install.sh
```

The installer is interactive either way, the one-liner included: it reattaches
its prompts to your terminal, because piping means stdin is the script itself
and an installer that quietly took every default instead of asking would be a
poor trade for one line. Confirm once and it installs Docker Engine and the
Compose and Buildx plugins from apt if needed, generates a login password,
detects whether the host has a microphone, brings the stack up, and prints
where to reach it.
Answer "no" at the first prompt to choose the port, password, mic passthrough,
self-hosted STT and diarization, and auto-updates one at a time.

To skip every prompt, for scripted installs:

```bash
curl -fsSL https://raw.githubusercontent.com/LuckyType/loreline/main/deploy/install.sh | bash -s -- --defaults
bash deploy/install.sh --defaults   # or, from a checkout
```

Note the `-s --` in the piped form: `--defaults` has to reach the script rather
than bash. With no terminal to prompt on at all (CI, a systemd unit), the
installer takes the defaults whether or not you pass the flag.

The app, its dependencies and the built UI all live inside the image.

- The app answers directly on `http://<host>:8000`, and through Caddy on
  `https://<host>` and `http://<host>`. TLS is self-signed on demand, so expect
  a browser warning until you trust Caddy's local CA. The plain HTTP port does
  not redirect, so you are never locked out before trusting that CA.
- `./data` is bind-mounted, so the SQLite database and the secrets survive
  `docker compose down` and `up`.
- Recording on a microphone attached to the host needs `/dev/snd` passed into
  the container, and that is off unless this box has one, because the mount is
  an error on a host that does not. `deploy/install.sh` turns it on when it
  finds `/dev/snd`; by hand it is
  `cp deploy/mic-passthrough.override.yml docker-compose.override.yml` and
  `docker compose up -d`, and deleting that file turns it off again.
  `.gitignore` covers it, so the choice survives a `git pull`. Linux hosts
  only: Docker Desktop on macOS and Windows runs containers in a VM with no
  path to the host's audio devices. A box with no microphone is not a box that
  cannot record, though - see
  [Recording from a laptop](#recording-from-a-laptop).
- A Bluetooth mic, or anything else reachable only through the host's
  PipeWire/PulseAudio session rather than as a raw ALSA `hw:` device, needs
  more setup. `deploy/setup-bluetooth-audio.sh` handles the pairing and wires
  the host session into the container. A wired or USB mic needs none of it. See
  [`docs/DEPLOYMENT-NOTES.md`](./docs/DEPLOYMENT-NOTES.md) for why.
- The image is CPU-only, no CUDA, and runs on any x86_64 or ARM64 host.
- Self-hosted STT and diarization are opt-in, since each pulls a multi-GB model
  download or manual ONNX files: `docker compose --profile local-stt up -d` and
  `--profile diarization`.

### Source and systemd

For a box where a container runtime is a hard no. This needs `uv` and Node on
the host. `deploy/install-source.sh` installs the system packages, but `uv`
itself has to be there first:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
sudo mv ~/.local/bin/uv ~/.local/bin/uvx /usr/local/bin/
```

Then, from the cloned repo:

```bash
git clone https://github.com/LuckyType/loreline.git /opt/loreline
cd /opt/loreline
bash deploy/install-source.sh
```

`APP_DIR` defaults to `/opt/loreline` and `SERVICE_USER` to `loreline`; both are
overridable through environment variables. The script creates the system user in
the `audio` group, installs `libportaudio2` and `libgomp1` for the `audio`
extra, `ffmpeg` for importing recordings, plus `nodejs` and `npm`, runs
`uv sync --extra audio --extra providers`,
builds the SvelteKit UI into `frontend/build`, and installs the systemd unit and
`deploy/sudoers.d/loreline`. Without that frontend build the app serves nothing
at `/`. The sudoers rule grants the service user passwordless `systemctl` for
`start`, `stop`, `restart`, `enable`, `disable`, `is-enabled` and `is-active` on
the `loreline` unit and nothing else, which is what the autostart toggle and the
self-update need.

Copy `.env.example` to `/opt/loreline/.env` and adjust it:

```bash
sudo -u loreline cp /opt/loreline/.env.example /opt/loreline/.env
sudo -u loreline "$EDITOR" /opt/loreline/.env
```

- `LORELINE_HOST=0.0.0.0`. The default `127.0.0.1` accepts local connections
  only, so without this the app is unreachable from the rest of the LAN.
- `LORELINE_AUTH_PASSWORD`. Set this before exposing the box beyond a trusted
  LAN; an empty value disables login entirely. Leave `LORELINE_JWT_SECRET`
  unset, since a random one is generated and persisted to `data/secrets.json` on
  first start.
- `LORELINE_ENVIRONMENT=prod` and `LORELINE_LOG_JSON=true` for structured logs.

Start it:

```bash
sudo systemctl start loreline
curl http://127.0.0.1:8000/api/system/livez
```

Autostart on boot is installed but disabled. Enable it from Settings, System in
the web UI, or with `sudo systemctl enable loreline`.

To move an existing `data/` directory, say to carry providers and sessions over
from a dev machine, stop the service, checkpoint the WAL so the database file is
self-consistent, copy both files, fix ownership, restart:

```bash
# on the source machine
sqlite3 data/loreline.db "PRAGMA wal_checkpoint(TRUNCATE);"
scp data/loreline.db data/secrets.json <host>:/tmp/

# on the target machine
sudo systemctl stop loreline
sudo mv /tmp/loreline.db /tmp/secrets.json /opt/loreline/data/
sudo chown loreline:loreline /opt/loreline/data/loreline.db /opt/loreline/data/secrets.json
sudo chmod 600 /opt/loreline/data/secrets.json
sudo systemctl start loreline
```

Here the web UI's update button does work. It runs `deploy/update-source.sh` as
the service user: `git pull --ff-only`, `uv sync`, a frontend rebuild, then a
restart through the sudoers rule above. Once the checkout has been updated at
least once, a Roll back button appears beside it. It resets to the commit the
checkout was on before it last moved, read from git's own reflog rather than
from a record of the last update, then re-syncs and restarts the same way, so
like an update it ends a recording running at the time. The confirm names the
commit it is going back to. A Docker deployment cannot roll back; there the
button is greyed out and the line beneath it says why, and the updater service
under [Updating](#updating) below is the way round it.

On LXC, a container that captures audio itself needs the host's sound device
passed through explicitly, through an `lxc.mount.entry` or a Proxmox `dev0:`
line for `/dev/snd`, and its `loreline` user added to the `audio` group, the
same as on bare metal. A container that only orchestrates remote STT needs no
audio passthrough.

### Recording from a laptop

The box running Loreline does not have to be the box that hears the table. Pick
"This device's microphone" in the capture card and the browser records: the
audio is streamed to the server frame by frame and the session runs exactly as
a session from a local microphone does, with the same live transcript, the same
VAD, the same stored WAV and the same re-processing afterwards. A server with no
sound card, no `/dev/snd` passthrough and no Bluetooth setup can then record a
whole evening off the laptop that is already open on the table. See
[`docs/adr/0010`](./docs/adr/0010-the-client-can-be-the-microphone.md).

The trade is worth knowing before the evening starts: **that tab has to stay
open and that machine has to stay awake for the whole session.** Loreline takes
a screen wake lock while it records and says on the card whether the browser
granted it, but a closed lid, a killed tab or a browser that sleeps the page
ends the recording about 45 seconds later. Everything captured up to that point
is a complete, re-transcribable WAV. A short disconnect is survivable: the tab
reconnects by itself, the gap is filled with silence so the recording stays
aligned, and the log records when audio stopped and resumed.

#### The prerequisite: a trusted certificate

Browsers hand out the microphone only in a **secure context**, which means
HTTPS or `localhost`. On `http://<lan-ip>/` every browser refuses, and nothing
in the app can change that; the capture card says so in as many words rather
than showing a picker that cannot work. The bundled Caddy's `tls internal`
certificate is not enough either, because it is trusted only on devices where
you have installed Caddy's local CA by hand.

Tailscale is the way in with the least to install: the box gets a real,
publicly trusted certificate on its `*.ts.net` name, nothing has to be trusted
by hand on the laptop or the phone, and it keeps working from outside the
house.

**Do this first, or nothing else here works.** In the Tailscale admin console,
under DNS, enable **MagicDNS** and then **HTTPS Certificates**. Both, in that
order. Without them no certificate is ever issued, and the failure is opaque:
`tailscale serve` reports nothing useful and the browser simply refuses the
microphone as before.

Then, on the box, with Tailscale installed and logged in, run
`deploy/tailscale-https.sh` from the checkout: it checks the box, waits for the
admin console toggles, finds the name, enables the proxy and verifies the
result over HTTPS before handing you to the laptop. By hand, it is:

```bash
tailscale status --json | grep -i certdomains   # the *.ts.net name to use
sudo tailscale serve --bg --https=443 http://127.0.0.1:8000
tailscale serve status
```

That proxies `https://<box>.<tailnet>.ts.net/` to the app's published port. The
certificate is `tailscaled`'s own and renews itself. `sudo tailscale serve
--https=443 off` undoes it.

Open that HTTPS name on the laptop, allow the microphone once, and
"This device's microphone" becomes selectable.

If you would rather Caddy held the certificate, it can: mount
`/var/run/tailscale/tailscaled.sock` into the `caddy` service and use a Caddy
build with Tailscale certificate support (`tls { get_certificate tailscale }`).
The official `caddy:2-alpine` image does not include it, so that route means
building your own image for the same result, which is why `tailscale serve` is
the one documented here.

#### One setting that fails silently

The app marks the login cookie `Secure` based on the browser's own connection,
and it believes a proxy's `X-Forwarded-Proto` only from a peer listed in
`LORELINE_TRUSTED_PROXIES`. With Tailscale terminating TLS in front, the app
sees a plain HTTP connection from the proxy, so if that proxy's address is not
trusted, a genuinely HTTPS session gets a cookie that is not marked `Secure`,
and nothing anywhere reports it.

- **Docker Compose:** already correct. `docker-compose.yml` sets
  `LORELINE_TRUSTED_PROXIES: 172.16.0.0/12`, and `tailscale serve` is a host
  process reaching the published port, so it arrives from the compose bridge
  gateway inside that range. If you have moved Compose onto a custom address
  pool, add that pool.
- **Source and systemd:** set `LORELINE_TRUSTED_PROXIES=127.0.0.1/32,::1/128`
  in `.env` and restart. `tailscaled` connects over the loopback; a LAN client
  arrives from a LAN address and so cannot claim to be it.

The same list governs the only other header the app reads about a client,
`X-Forwarded-For`, which decides who the login backoff counts against. Behind a
proxy without it, five wrong passwords from one machine lock out everyone;
with it, the backoff is per client again.

#### Plain HTTP is unaffected

TLS is a requirement for this one feature, not for Loreline. `http://<lan-ip>/`
and `http://<lan-ip>:8000` keep working exactly as before, including the
server's own microphone, and nothing else on any page changes. If you have no
use for recording from a browser, none of this section applies to you.

### Updating

Every Docker path runs the same published image. From the quickstart's single
container, updating is a pull and a recreate, and the named volume is what
carries the sessions across:

```bash
docker pull ghcr.io/luckytype/loreline:latest
docker rm -f loreline
docker run -d --name loreline -p 8000:8000 -v loreline-data:/app/data \
  ghcr.io/luckytype/loreline:latest
```

The rest of this section is about the Compose stack, which has more ways to do
it because it has more to keep in step. The source deployment updates itself
from the web UI, as described above.

The web UI's update button cannot update a Docker deployment by itself. Handing
the container the Docker socket access it would need to restart itself is
effectively root on the host, and that is not a trade this project makes for you
silently. That still holds, and nothing below changes it; what the last of the
options below adds is a way for the button to hand the job to something that
already has that access. By default, update from the host:

```bash
deploy/update.sh                                   # git pull + compose pull + up -d
sudo systemctl enable --now loreline-update.timer  # or: daily, automatic
```

This is the default and it stays the default. It rebuilds the image from your
own checkout, and the only thing holding any privilege is a systemd unit on the
host, where you can read it. What it costs you is that the box does the build,
which on a Raspberry Pi is slow, and that it needs a git checkout and a shell.
It rebuilds the diarization service's image too, but only while its compose
profile is recorded in `.env`: `deploy/install.sh` writes
`COMPOSE_PROFILES=diarization` there for you when you enable diarization at
install time, and this script prints the manual rebuild command instead when
a diarization container is running without that line.

[`docker-publish.yml`](.github/workflows/docker-publish.yml) publishes
`ghcr.io/luckytype/loreline` for amd64 and arm64 on every push to `main`, so
there is now a second option that skips the build:

```bash
sudo docker compose pull && sudo docker compose up -d
```

Same result, no compiler on the device, and still nothing automatic and no
container holding the socket. Note what it does not do: it updates the image and
only the image. Changes to `docker-compose.yml`, the Caddyfile or anything under
`deploy/` still arrive by `git pull`, which is why `deploy/update.sh` does both.

**`deploy/update-fast.sh`, if you want that as one command.** The two-liner
above leaves the `git pull` to you, and forgetting it is how a box ends up
running a new image against last month's `docker-compose.yml`. This script does
both halves, in that order, and nothing else:

```bash
deploy/update-fast.sh
```

That is `git pull --ff-only`, then `docker compose pull app`, then
`docker compose up -d --no-build app`. It never builds anything, and
`--no-build` is load-bearing rather than decorative: the `app` service defines
both `build:` and `image:`, so an image Compose cannot pull would otherwise fall
back to the from-source build this whole path exists to skip. With the flag it
fails and says why instead. Only the `app` service is touched, so Caddy, the
docker proxy and any enabled profile services keep running, and the script tells
you afterwards if the pull brought compose-file changes that need a wider
`up -d`.

Its prerequisite is real and not optional: **the GHCR package has to be
pullable from this box.** A GHCR package is private the first time it is
published, even from a public repository, so until someone switches
`ghcr.io/luckytype/loreline` to public in its package settings, or you run
`sudo docker login ghcr.io` here with a `read:packages` token, this path pulls
nothing at all. It says exactly that, and what to do about it, rather than
leaving you with a bare 401 to interpret.

What it costs against `deploy/update.sh` is freshness and self-containment. It
deploys whatever `docker-publish.yml` last published from `main`, so a commit
that landed ten minutes ago is not here yet, and one that broke the image build
is not here at all, whereas `update.sh` builds whatever you have checked out. On
a Raspberry Pi, where that build is the slow part by a wide margin, the trade is
usually worth making. On a box that builds in two minutes it buys much less.

**The `updater` service, if you want the update button to work.** The button in
Settings > Client cannot update a Docker deployment on its own, for the reason at
the top of this section. What it can do is hand the job to a container that
already holds that access, and that container is in this repo:
`services/updater/`, a single Python file of a couple of hundred lines, most of
them comments. It is off by default; opt in by name:

```bash
sudo docker compose --profile updater up -d
```

It answers exactly one route, `POST /update`, gated by a bearer token, and that
route takes no arguments at all - no image, no tag, no container id, no command.
The most a leaked token buys is the same update the button performs. What it runs
is `deploy/update-fast.sh`, the same script documented above from the same
checkout, so there is one copy of the update logic on the box rather than two
that can drift apart.

**It reimplements nothing about registries, deliberately.** This is the third
answer this feature has had and the first that is ours. Watchtower was archived
upstream and its last release cannot talk to a current Docker Engine at all: its
bundled client negotiates below the API 1.44 floor that Engine 29 enforces, so on
the real box every call it made was refused. WUD replaced it, started cleanly,
and then could not see `ghcr.io/luckytype/loreline` even after the package was
made public - its GHCR provider never performs the anonymous OCI token exchange,
sending a placeholder where a bearer token belongs, so it demands an access token
for a package that needs none. Both faults were the same fault: a partial
reimplementation of something Docker's own client already does correctly. This
one shells out to the real `docker` and `docker compose` CLIs and has no registry
code of its own.

**What it needs.** Two entries in `.env`, both written by `deploy/install.sh`
whether or not you enabled the profile:

```bash
UPDATER_TOKEN=<random string; the app sends it, the service checks it>
UPDATER_REPO_DIR=/opt/loreline   # this checkout's path on this host
```

On a box installed before this existed, `deploy/install.sh` leaves an existing
`.env` alone, so add those two lines by hand and delete the three stale
`WUD_AUTH_*` ones sitting next to them, then `sudo docker compose up -d app` so
the app picks the token up. Until you do, the button reports what it always
reported: that updates run from the host.

`UPDATER_REPO_DIR` is not decoration. The updater container is given the checkout
at that same absolute path inside itself, because it drives the *host's* Docker
daemon: the relative bind mounts in `docker-compose.yml` (`./data`,
`./Caddyfile`) are resolved against the project directory inside the container
and then handed to the host to interpret. Mount the repo anywhere else and the
recreated app container would bind a host path that does not exist, which Docker
would helpfully create, empty - and the app would come back appearing to have
lost every session. The same equality is also what makes Compose derive the same
project name on both sides, so the update reaches the running stack instead of
starting a second one beside it. If you cloned to `/opt/loreline` the default is
already right; the service refuses to run, naming the variable, rather than
guess.

**The trade, plainly:** this container gets the Docker socket, and the Docker
socket is root on the host. It is the same access the app container is refused at
the top of this section. It cannot go behind the socket proxy the app uses,
because that proxy exists to refuse `POST /containers/create` and creating
containers is the entire job here. The socket mount is not marked `:ro`, and that
is not an oversight: a read-only bind mount of a unix socket does not make the
socket read-only, since the kernel refuses writes to files, directories and
symlinks on a read-only mount but not the connect-and-send path a socket is
actually used through. The flag would only imply a restriction that was never
there. For the same reason the repo mount is not trimmed down to a handful of
files: a container holding the socket can start another container with any mount
it likes, so narrowing its own would be decoration rather than a boundary.

What does narrow it is the shape of the endpoint and the scope of what it runs.
`docker compose up -d --no-build app` names one service, so Caddy, the socket
proxy, the self-hosted STT server and the diarizer are never candidates. That is
tighter than either predecessor managed: both scoped themselves with a container
label, which is host-wide, so any other container on the box carrying that label
was fair game too. This one cannot be asked about another container at all.

Also worth knowing before you enable it:

- **Do not run it alongside `loreline-update.timer`.** The timer rebuilds the
  image from source, this replaces it with the registry one, and each undoes the
  other on its own schedule. Pick one.
- **It applies an update only when you press the button.** There is no scheduler
  in it, which is the other half of the previous point. Unattended updates are
  the systemd timer's job.
- **It only moves forward.** Roll back is greyed out on a Docker deployment:
  the service pulls whatever the registry publishes and takes no argument that
  could name anything else, and the container holds no git checkout to reset.
  To run an older release, pin its tag (`ghcr.io/luckytype/loreline:<version>`)
  in `docker-compose.yml` and recreate the app container.
- **An update recreates the app container**, which ends a recording running at
  the time, and it needs the GHCR package to be pullable from this box - the same
  prerequisite `deploy/update-fast.sh` has above, reported the same way.
- **It does not update itself.** That is what makes an honest answer possible,
  below; the flip side is that a release changing `services/updater/` is not
  applied by the button. Run
  `sudo docker compose --profile updater up -d --build updater` for that.
- **git runs in the checkout as root**, since that is what the container is, so
  files it writes there end up root-owned. A later host-side `git pull` still
  works, because git creates and renames rather than writing existing files in
  place, but `ls -l` will show a mixture of owners.

**What the button reports, and why it can be believed.** The update runs in two
halves. The first, `git pull` then `docker compose pull app`, touches nothing
that is running, so the app is alive to hear how it went; the answer goes out,
and only then does the second half recreate the app container. So the button
tells you one of:

- *Already up to date.* Nothing was pulled, nothing applied, nothing restarted.
  This is the common case, and it is now a real answer rather than a guess.
- *A newer image was pulled and the app is being recreated onto it.* The page
  loses its connection a moment later and comes back on its own.
- *The update script's own output*, verbatim, if it failed - including the
  specific explanation `deploy/update-fast.sh` writes for the GHCR-visibility
  case, rather than a generic error in its place.

That split is not decorative. Run in one piece, `docker compose up -d` stops the
app container while the app container is still waiting for the answer: Compose
waits for it to exit, uvicorn waits for the in-flight request, and that request
waits on Compose. The cycle breaks only when Docker kills the app at the end of
its stop grace period, so a single-shot version can never report to the caller it
is about to replace. Both previous attempts had exactly that problem and had to
answer "triggered, no idea how it went".

**The socket boundary is untouched.** The app makes one authenticated request to
a sibling container and holds no Docker access before or after. The updater will
also appear under Settings, Services as a container the UI cannot start or stop,
since only the STT and diarization services are controllable from there.

None of the registry paths work until that workflow has actually run on GitHub
and the package it publishes has been switched to public. A GHCR package is
private on first publish, and a private one needs `docker login ghcr.io` before
any of these pulls succeed.

## Contributing

Bug reports and pull requests are welcome. See
[`docs/CONTRIBUTING.md`](./docs/CONTRIBUTING.md) for local setup, the checks CI
runs, a map of the codebase, and how to add an STT backend.

## License

[GNU AGPL-3.0](./LICENSE). Free to use, modify and distribute, including
commercially. If you modify Loreline and make it available to others over a
network, AGPL section 13 requires you to offer those users the corresponding
source of your modified version.
