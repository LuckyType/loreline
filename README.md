<div align="center">

<img src="assets/icon.png" alt="Loreline" width="140">

# Loreline

**Headless tabletop session transcriber** - put it in the middle of the table,
get a speaker-attributed transcript of your session.

[![CI](https://github.com/LuckyType/loreline/actions/workflows/ci.yml/badge.svg)](https://github.com/LuckyType/loreline/actions/workflows/ci.yml)
[![License: AGPL v3](https://img.shields.io/badge/license-AGPL--3.0-blue)](./LICENSE)

[![Python](https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![Svelte](https://img.shields.io/badge/Svelte%205-FF3E00?logo=svelte&logoColor=white)](https://svelte.dev/)
[![Tailwind CSS](https://img.shields.io/badge/Tailwind%20CSS%204-06B6D4?logo=tailwindcss&logoColor=white)](https://tailwindcss.com/)
[![SQLite](https://img.shields.io/badge/SQLite-003B57?logo=sqlite&logoColor=white)](https://www.sqlite.org/)
[![Docker](https://img.shields.io/badge/Docker-2496ED?logo=docker&logoColor=white)](https://www.docker.com/)
[![uv](https://img.shields.io/badge/uv-DE5FE9?logo=uv&logoColor=white)](https://docs.astral.sh/uv/)
[![Ruff](https://img.shields.io/badge/Ruff-D7FF64?logo=ruff&logoColor=black)](https://docs.astral.sh/ruff/)

</div>

Captures the full dialog of a Pen & Paper session through a connected microphone and
transcribes it via pluggable cloud or self-hosted speech-to-text backends, with optional
speaker diarization. A web UI provides the live transcript, configuration, logs,
monitoring and updates.

Runs on Linux x86 and Raspberry Pi ARM64. The device is a **capture + orchestrator only** -
all STT and diarization run on remote endpoints (cloud APIs or self-hosted LAN services).

## Features

- **Capture** - continuous recording with Silero VAD utterance chunking.
- **Pluggable STT** - Deepgram, AssemblyAI, Google STT v2 (gRPC), OpenAI Realtime, and any
  OpenAI-compatible endpoint (Speaches, whisper.cpp). A primary/fallback router handles
  failover, and can fan out to several backends at once to compare them.
- **Speaker diarization** - inline (from the STT provider), or a self-hosted sherpa-onnx
  service (included), or off.
- **Sessions** - SQLite persistence with per-session audio, post-session re-processing
  (re-transcribe or re-diarize with a different provider/model), speaker renaming, and
  LLM-generated summaries.
- **Exports** - txt, md, srt, vtt, json.
- **Web UI** - SvelteKit SPA served by FastAPI: live transcript, session history,
  provider/glossary config, live logs, health and alerting.
- **Ops** - JWT-cookie auth, push alerts, health endpoint, and self-update.

## Configuration

Settings come from environment variables (prefix `LORELINE_`) or a `.env` file. See
[`.env.example`](./.env.example). API keys entered via the UI are stored in
`data/secrets.json` (`0600`); environment variables override stored secrets.

## Deployment

**Docker Compose is the recommended path** - `deploy/install.sh` installs Docker if it's
missing and brings the whole stack up; nothing else needs installing on the host. A
source+systemd deployment (no container runtime at all) is also supported for boxes where
that's a hard requirement - see [Source + systemd](#source--systemd) below.

### Docker Compose

```bash
git clone https://github.com/LuckyType/loreline.git /opt/loreline
cd /opt/loreline
bash deploy/install.sh
```

The installer is interactive with sensible defaults - confirm once and it installs Docker
Engine + the Compose plugin (via apt) if needed, generates a login password, detects
whether the host has a microphone, brings the stack up, and prints where to reach it.
Answer "no" at the first prompt to choose the port, password, mic passthrough, self-hosted
STT/diarization, and auto-updates individually. `bash deploy/install.sh --defaults` skips
all prompts, for scripted installs.

The app, its dependencies, and the built UI all live inside the image; nothing else goes
on the host.

- The app is reachable directly at `http://<host>:8000`, or via Caddy at
  `https://<host>` (self-signed on-demand TLS - expect a browser warning until you trust
  Caddy's local CA) / `http://<host>` (plain, no redirect, so you're never locked out
  before trusting that CA).
- `./data` is bind-mounted in, so the SQLite DB and secrets persist across
  `docker compose up`/`down`.
- Real microphone capture needs `/dev/snd` passed through - already on by default in
  `docker-compose.yml`. **Linux Docker host only**: Docker Desktop on macOS/Windows runs
  containers in a VM with no path to the host's native audio devices, so mic capture can't
  work there regardless - comment that line out there (or on a Linux box with no local mic
  at all, e.g. one only orchestrating remote STT).
- A Bluetooth mic (or anything else only reachable via the host's PipeWire/PulseAudio
  session, not a raw ALSA `hw:` device) needs its own setup -
  `deploy/setup-bluetooth-audio.sh` walks through pairing and wiring the host session into
  the container. Not needed for a wired/USB mic.
- The image is CPU-only (no CUDA) and works on any x86_64/ARM64 host, including an
  AMD-GPU or GPU-less mini PC - nothing in this app has a GPU-accelerated code path.
- Self-hosted STT/diarization are opt-in (a multi-GB model download or manual ONNX files
  otherwise), not part of the default `up`:
  `docker compose --profile local-stt up -d` / `--profile diarization`.

**Updating:** self-update from the web UI isn't available in a Docker deployment - handing
the container the Docker-socket access it'd need to restart itself is effectively root on
the host, not a trade this project makes for you silently. Update from the host instead:

```bash
deploy/update.sh                                          # git pull + compose pull + up -d
sudo systemctl enable --now loreline-update.timer          # or: daily, automatic
```

### Source + systemd

For a box where a container runtime is a hard no. Needs `uv` and Node on the host directly
(`deploy/install-source.sh` installs the system packages; `uv` itself needs installing
first):

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

`APP_DIR` (default `/opt/loreline`) and `SERVICE_USER` (default `loreline`) are overridable
via env vars. The script creates the system user (in the `audio` group), installs
`libportaudio2`/`libgomp1` (native deps for the `audio` extra) and `nodejs`/`npm`, runs
`uv sync --extra audio --extra providers`, builds the SvelteKit UI (`frontend/build` - the
app serves nothing at `/` without this), and installs the systemd unit +
`deploy/sudoers.d/loreline` (grants the service user passwordless `systemctl {start,stop,
restart,enable,disable,is-enabled,is-active} loreline` only - nothing broader - for the
web UI's autostart toggle and self-update).

**Configure.** Copy `.env.example` to `/opt/loreline/.env` and adjust at least:

```bash
sudo -u loreline cp /opt/loreline/.env.example /opt/loreline/.env
sudo -u loreline "$EDITOR" /opt/loreline/.env
```

- `LORELINE_HOST=0.0.0.0` - the default `127.0.0.1` only accepts local connections;
  without this the app won't be reachable from elsewhere on the LAN.
- `LORELINE_AUTH_PASSWORD` - set this before exposing the box beyond a trusted LAN; empty
  disables login entirely. Leave `LORELINE_JWT_SECRET` unset - a random one is generated
  and persisted to `data/secrets.json` on first start.
- `LORELINE_ENVIRONMENT=prod`, `LORELINE_LOG_JSON=true` for structured logs.

See [Configuration](#configuration) for the full variable list, including
`LORELINE_SECRET_<NAME>` overrides for provider API keys.

**Start it:**

```bash
sudo systemctl start loreline
curl http://127.0.0.1:8000/api/system/healthz
```

Autostart-on-boot is installed but disabled by default - enable it from the web UI
(Settings → System) or with `sudo systemctl enable loreline`.

**Migrating an existing `data/` dir** (e.g. moving providers/sessions from a dev machine
so they don't need to be recreated): stop the service, checkpoint the WAL so the DB file is
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

**Self-update:** the web UI's Update button runs `deploy/update-source.sh` as the service
user - `git pull --ff-only`, `uv sync`, rebuild the frontend, then restart itself via the
sudoers rule above. A rollback to any prior commit is available from the same page.

**LXC-specific note:** if the container needs to capture audio itself, the host's sound
device has to be passed through explicitly (e.g. an `lxc.mount.entry` / Proxmox `dev0:`
line for `/dev/snd`) and the container's `loreline` user added to the `audio` group, same
as on bare metal. A container used purely as an orchestrator against remote STT - no local
mic - needs no audio passthrough at all.

## Contributing

Bug reports and pull requests are welcome - see
[`docs/CONTRIBUTING.md`](./docs/CONTRIBUTING.md) for local setup, the checks CI runs, a
map of the codebase, and how to add a new STT backend.

## License

[GNU AGPL-3.0](./LICENSE) - free to use, modify and distribute, including commercially.

If you modify Loreline and make it available to others over a network, AGPL section 13
requires you to offer those users the corresponding source of your modified version.
