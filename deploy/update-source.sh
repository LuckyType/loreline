#!/usr/bin/env bash
# Self-update for the source deployment. Invoked by the web-UI "Update" button.
# Pulls latest main, syncs deps, and restarts the service. Records the previous
# commit so the UI can offer a rollback.
set -euo pipefail

# The whole body is wrapped in one `{ }` block: bash reads a running script
# incrementally from its file descriptor, and `git pull` below rewrites this
# very file partway through - without the wrapper, everything after the pull
# can read back truncated/corrupted bytes once the file underneath it
# changes, silently cutting the update short right after the pull's own
# output (exactly what "Update failed" looked like before this fix). Wrapping
# the body makes bash read the whole block into its parse buffer up front.
{
APP_DIR="${APP_DIR:-/opt/loreline}"
cd "${APP_DIR}"

PREV_COMMIT="$(git rev-parse HEAD)"
echo "previous_commit=${PREV_COMMIT}"

git fetch --quiet origin
git pull --ff-only origin main

uv sync --frozen --extra audio --extra providers

(cd frontend && npm ci && npm run build)

# This script runs inside the loreline unit itself, so restarting it
# directly (even with --no-block) sends SIGTERM to this script's own cgroup
# moments later, before the echoes below can run - the update still
# succeeds, but the API sees this script killed rather than exiting 0.
# systemd-run detaches the restart into its own transient unit, decoupling
# it entirely from this process (see deploy/sudoers.d/loreline).
sudo systemd-run --on-active=2 --unit=loreline-restart --collect systemctl restart loreline

NEW_COMMIT="$(git rev-parse HEAD)"
echo "new_commit=${NEW_COMMIT}"
echo "Update complete."
}
