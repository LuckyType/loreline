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
sudo docker compose up -d --build --remove-orphans

NEW_COMMIT="$(git rev-parse HEAD)"
echo "new_commit=${NEW_COMMIT}"
echo "Update complete."
}
