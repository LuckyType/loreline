#!/usr/bin/env bash
# Update a Docker Compose deployment: pull latest source, rebuild/pull
# images, recreate. Run manually (over SSH, or via `deploy/loreline-update.timer`
# - installed but disabled by default, see loreline-update.timer) - this
# runs on the *host*, not inside a container, so there's no in-app "Update"
# button here the way the source+systemd deployment has one: giving the app
# container the access it'd need to restart itself (the Docker socket) is
# effectively root on the host, and that's not a trade a script should make
# for you silently.
set -euo pipefail

# Wrapped in one `{ }` block: git pull below rewrites this very file while
# bash is still reading it - without the wrapper, everything after the pull
# can read back truncated/corrupted bytes once the file underneath it
# changes (see the equivalent fix + explanation in update-source.sh).
{
APP_DIR="${APP_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
cd "${APP_DIR}"

PREV_COMMIT="$(git rev-parse HEAD)"
echo "previous_commit=${PREV_COMMIT}"

git fetch --quiet origin
git pull --ff-only origin main

# Pull the images this repo does not build (Caddy, the docker proxy, the STT
# server) and skip the one it does, which is exactly what this line did before
# the app service grew an `image:` reference.
#
# It matters because a pull that fails on a *buildable* service still exits
# non-zero: without this, `set -e` would abort the update right here, before
# the rebuild below, on every box where ghcr.io/luckytype/loreline does not
# exist yet or is still a private package. It is not free when it succeeds
# either - a couple of GB fetched and then set aside by the `--build` on the
# next line, on a device that may be a Raspberry Pi. Updating from the registry
# instead of from source is the other update path, not this one; see the
# README's "Updating" section.
#
# --ignore-buildable only landed in Compose v2.15.0 (January 2023), and
# install.sh takes Compose from the distro (docker-compose-v2), which on a Pi
# can sit well behind that. So ask this box's Compose what it supports rather
# than assume, the same way install.sh picks between the two Compose package
# names. The fallback has shipped in every Compose v2 there has ever been: it
# pulls more than it needs to, but it cannot fail the update.
if sudo docker compose pull --help 2>/dev/null | grep -q -- '--ignore-buildable'; then
  sudo docker compose pull --ignore-buildable
else
  sudo docker compose pull --ignore-pull-failures
fi
# The revision the rebuilt image reports in Settings > Client, read after the
# pull above so it is the release being deployed rather than the one being
# replaced. git cannot be asked from inside the container (the image has no
# .git - see the Dockerfile), so it is baked in here.
#
# `--always` guarantees an answer: on a box installed by deploy/install.sh the
# checkout is shallow, no tag is reachable, and describe degrades to the short
# SHA rather than failing. `|| true` covers the rest - an empty value is read
# as an unknown revision and shown as a dash, which beats aborting an update
# over a cosmetic string.
#
# Passed through `env` because sudo does not carry an exported variable across.
BUILD_COMMIT="$(git rev-parse HEAD 2>/dev/null || true)"
BUILD_DESCRIBED="$(git describe --tags --always 2>/dev/null || true)"
sudo env "LORELINE_BUILD_COMMIT=${BUILD_COMMIT}" "LORELINE_BUILD_DESCRIBED=${BUILD_DESCRIBED}" \
  docker compose up -d --build --remove-orphans

NEW_COMMIT="$(git rev-parse HEAD)"
echo "new_commit=${NEW_COMMIT}"

# The diarization service is built from this checkout exactly like the app is,
# but only when its compose profile is active for this project: the `up` above
# passes no --profile and takes COMPOSE_PROFILES from .env, which
# deploy/install.sh writes there when the profile is chosen at install time. A
# box that started that service some other way - by hand, or from Settings >
# Services - has it running with the profile recorded nowhere, and this update
# would leave it on the image it was first built with while updating everything
# around it. Say so, with the command that fixes it, rather than let a stale
# diarizer look updated.
#
# `docker ps` rather than `compose ps`, because the latter filters by active
# profile and that is the very thing being tested here. Both answers are
# captured and then matched with bash, not piped into `grep -q`: grep exits at
# the first match, SIGPIPEs the producer, and `set -o pipefail` turns that into
# a failed update (same trap as have_pkg in deploy/install.sh).
DIAR_RUNNING="$(sudo docker ps --filter label=com.docker.compose.service=diarization --format '{{.Names}}' 2>/dev/null || true)"
DIAR_ACTIVE="$(sudo docker compose config --services 2>/dev/null || true)"
if [[ -n ${DIAR_RUNNING} && $'\n'${DIAR_ACTIVE}$'\n' != *$'\n'diarization$'\n'* ]]; then
  echo
  echo "note: the diarization service is running, but its compose profile is not"
  echo "      active here, so it was not rebuilt. To update that service too:"
  echo "        sudo docker compose --profile diarization up -d --build diarization"
  echo "      To have every update rebuild it, add this line to ${APP_DIR}/.env:"
  echo "        COMPOSE_PROFILES=diarization"
fi

echo "Update complete."
}
