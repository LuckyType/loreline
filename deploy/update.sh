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

sudo docker compose pull
sudo docker compose up -d --build --remove-orphans

NEW_COMMIT="$(git rev-parse HEAD)"
echo "new_commit=${NEW_COMMIT}"
echo "Update complete."
}
