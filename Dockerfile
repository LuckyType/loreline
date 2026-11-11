# The production image - full UI + backend in one container, which is the
# whole of a minimal deployment:
#
#   docker run -d --name loreline -p 8000:8000 -v loreline-data:/app/data \
#     ghcr.io/luckytype/loreline:latest
#
# docker-compose.yml adds the appliance around it (a reverse proxy, the Docker
# API window, the optional self-hosted services); deploy/install.sh installs
# that. A source+systemd deployment (no Docker at all) is also supported - see
# deploy/install-source.sh - for boxes where that's a hard requirement.
#
# Capturing from a microphone attached to the *host* additionally needs
# /dev/snd passed into the container, which is off by default because the
# mount is an error on a host that has no sound card: see
# deploy/mic-passthrough.override.yml. Docker Desktop on macOS/Windows runs
# containers in a Linux VM with no path to the host's native audio devices, so
# host mic capture only ever works on a Linux host. Recording through the
# browser works everywhere and needs none of it.

# --- Stage 1: build the SvelteKit static frontend -----------------------
FROM node:22-slim AS frontend-builder

# node:22-slim currently bundles npm 10.9.x. frontend/.npmrc's min-release-age
# needs npm >= 11.10.0 or it is silently ignored (older npm just warns and
# installs anyway), so pin an explicit upgrade rather than trust the base
# image's bundled version.
RUN npm install -g npm@11.10.0

WORKDIR /app/frontend

COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci

COPY frontend/ ./
RUN npm run build

# --- Stage 2: Python app + the built frontend ----------------------------
# ubuntu:26.04, not python:3.12-slim, specifically for PortAudio: Debian's
# libportaudio2 (what the python:*-slim images are built on) is compiled
# with ALSA + JACK only - no PulseAudio host API at all. That's fine for a
# wired/USB mic on /dev/snd, but a Bluetooth mic is *only* reachable through
# the host's PipeWire/PulseAudio session, so on Debian it simply never
# appears in the device list. Verified by `ldd libportaudio.so.2`: Debian
# and Ubuntu 24.04/25.10 don't link libpulse; Ubuntu 26.04 does. Python
# comes from uv here rather than the base image.
FROM ubuntu:26.04 AS base

# Pinned rather than `latest`: this binary resolves and installs everything
# below, so an unpinned tag is the one input to this image that the lockfile
# and the 14-day cooldown in pyproject.toml say nothing about. Dependabot
# bumps it (see .github/dependabot.yml).
COPY --from=ghcr.io/astral-sh/uv:0.11.32 /uv /uvx /usr/local/bin/

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# libportaudio2: the native shared library sounddevice (the `audio` extra)
# links against at import time - its wheel doesn't bundle it on Linux.
# libgomp1: onnxruntime's OpenMP runtime, needed by silero-vad.
# libpulse0: PortAudio's PulseAudio host API, for PipeWire-routed sources
# (Bluetooth) - see the base-image note above.
# ca-certificates: outbound HTTPS to the cloud STT/LLM providers.
# ffmpeg: decodes an imported recording (m4a, mp3, ogg, opus, webm, flac) into
# the 16 kHz mono WAV a capture produces. A PCM WAV import needs no ffmpeg;
# everything a phone actually records does.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates libportaudio2 libgomp1 libpulse0 ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Install dependencies first (cached layer). The audio extra matches what a
# real device install gets (see deploy/install-source.sh).
#
# The cache mount keeps uv's download cache out of the image. With
# UV_LINK_MODE=copy every wheel is copied into .venv, so a cache left in the
# layer is a second, never-read copy of torch and onnxruntime.
COPY .python-version pyproject.toml uv.lock ./
RUN uv python install
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev --extra audio

# Install the project.
COPY . .
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --extra audio

# Built SPA from the frontend stage - create_app() only mounts the web UI at
# "/" when frontend/build/index.html exists (see loreline.web.spa).
COPY --from=frontend-builder /app/frontend/build ./frontend/build

# The revision this image was built from, and deliberately the last thing in
# the file. Everything above is cached on content that changes rarely; these
# change on every single commit, and a cache miss here is not cheap - this
# image installs torch, torchaudio and onnxruntime and byte-compiles all of it
# (UV_COMPILE_BYTECODE above), and CI builds the arm64 half under QEMU
# emulation, so busting anything earlier costs tens of minutes per push. A
# build arg only invalidates from the first instruction that *uses* it, so
# declared here they cost the metadata layers below and nothing else.
#
# Baked because git cannot answer from inside: .dockerignore excludes .git, so
# `git rev-parse` in this container fails every time (see
# loreline.updater.Updater.current_revision, which therefore does not run it
# here at all). Filled by .github/workflows/docker-publish.yml, and by
# deploy/install.sh and deploy/update.sh via docker-compose.yml's `args:`.
# Empty is a valid value, and is what a plain `docker build` with no
# --build-arg gets: the UI reads it as an unknown revision and shows a dash.
ARG LORELINE_BUILD_COMMIT=""
ARG LORELINE_BUILD_DESCRIBED=""
ENV LORELINE_BUILD_COMMIT=${LORELINE_BUILD_COMMIT} \
    LORELINE_BUILD_DESCRIBED=${LORELINE_BUILD_DESCRIBED}

EXPOSE 8000
ENV LORELINE_HOST=0.0.0.0

# Asks the one route that says "the process is serving" and nothing about the
# deployment, so an unclaimed instance and one with a provider down are both
# healthy here. Python rather than curl because the image has no curl.
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD ["/app/.venv/bin/python", "-c", "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('LORELINE_PORT', '8000') + '/api/system/livez', timeout=4)"]

# The venv's own entry point, not `uv run`: `uv run` syncs the environment
# before it starts, dev group included, so every new container downloaded
# ruff, pyright and pytest from PyPI and could not start without a network.
# Same command deploy/loreline.service runs.
CMD ["/app/.venv/bin/loreline", "run"]
