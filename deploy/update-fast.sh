#!/usr/bin/env bash
# Update a Docker Compose deployment from the image CI already built, instead
# of rebuilding it on this box: git pull for the compose file, `compose pull
# app` for the image, recreate. The fast path, and on a Raspberry Pi the
# difference between a couple of minutes and the better part of an hour.
#
# It is not a replacement for deploy/update.sh, and that one stays the default.
# update.sh builds from this checkout, so it works on every deployment there
# has ever been, including one where the GHCR package does not exist yet or is
# still private. This script can only ever be as fresh as the last successful
# run of .github/workflows/docker-publish.yml on main, and it needs that
# package to be pullable from here. See the README's "Updating" section for the
# choice between the two.
#
# A separate script rather than a flag on update.sh, deliberately, and for the
# same reason update-source.sh is its own file rather than a mode of this one:
# the two operations differ in what they need, not just in what they do.
# update.sh needs the checkout to be current because it compiles it. This one
# needs the checkout only to read docker-compose.yml correctly and never looks
# at the source at all, so folding them together would mean one script whose
# preconditions depend on which half you asked for.
set -euo pipefail

# Same `{ }` wrapper as update.sh and update-source.sh, for the same reason:
# bash reads a running script incrementally from its file descriptor, and the
# `git pull` below can rewrite this very file partway through, after which
# everything not yet read comes back as truncated or shifted bytes. Being a
# shorter script is not protection - it makes it likelier, not less likely,
# that the pull lands inside the part bash has not reached yet. Wrapping the
# body forces bash to parse the whole block up front.
{
APP_DIR="${APP_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
cd "${APP_DIR}"

PREV_COMMIT="$(git rev-parse HEAD)"
echo "previous_commit=${PREV_COMMIT}"

# The image is the only thing this script skips building. Everything needed to
# interpret it correctly still arrives by git: docker-compose.yml itself, the
# Caddyfile, new env var names, new services. Note that the commits printed
# here and at the end therefore describe this *checkout*, not the image that
# ends up running, which is whatever CI last published to main.
git fetch --quiet origin
git pull --ff-only origin main

# One service by name, not the whole project, which also sidesteps the problem
# update.sh has to work around with --ignore-buildable: a bare
# `docker compose pull` would additionally try the buildable, image-less
# `diarization` service and re-fetch Caddy and the multi-GB STT image for
# nothing. Pulling a service that carries both `build:` and `image:` is
# supported and does fetch the image, which is what that `image:` line in
# docker-compose.yml is there for.
#
# Failures are caught rather than left to `set -e`, because the failure this
# path actually hits in practice is a permissions one with a specific fix, and
# a bare Docker 401 does not tell anyone what that fix is.
PULL_LOG="$(mktemp)"
trap 'rm -f "${PULL_LOG}"' EXIT

# `tee` and not a captured variable, so a multi-GB pull shows progress while it
# runs. Safe under `set -o pipefail`: tee reads to EOF, so it cannot SIGPIPE
# the producer the way an early-exiting `grep -q` can (see the shell gotchas in
# docs/DEPLOYMENT-NOTES.md).
if ! sudo docker compose pull app 2>&1 | tee "${PULL_LOG}"; then
  echo >&2
  echo "Pulling ghcr.io/luckytype/loreline failed. Nothing on this box was changed." >&2
  echo >&2
  if grep -Eqi 'denied|unauthori[sz]ed|not found|manifest unknown|401|403|404' "${PULL_LOG}"; then
    cat >&2 <<'MSG'
That reads like the registry refusing the request rather than a network
problem, and this path has a prerequisite that fails exactly this way: a GHCR
package is private the first time it is published, even from a public
repository, and a private package answers an anonymous pull with 401 or 404
rather than admitting it exists.

Either fix works:

  - Make the package public, once. GitHub -> the repository -> Packages ->
    ghcr.io/luckytype/loreline -> package settings -> change visibility.
  - Or log this box in: `sudo docker login ghcr.io` with a GitHub personal
    access token carrying the read:packages scope.

The README's "Updating" section states the prerequisite in full, under the
deploy/update-fast.sh path.

If you want the update now and neither of those is convenient, deploy/update.sh
rebuilds from this checkout and needs no registry access at all.
MSG
  else
    cat >&2 <<'MSG'
This does not look like a registry-permissions problem, so the pull output
above is the real story - a network or disk issue, most likely.

deploy/update.sh rebuilds from this checkout instead, if you need the update
now and the registry stays unreachable.
MSG
  fi
  exit 1
fi

# `--no-build` is the load-bearing flag here, and the reason this is not just
# `docker compose up -d app`.
#
# The `app` service defines both `build:` and `image:`, and Compose resolves
# that pair through pull_policy: with no pull_policy set, per the Compose Build
# Specification, "Compose attempts to pull the image first and then builds from
# source if the image isn't found in the registry or platform cache". The pull
# above already put it in the local cache, so a plain `up -d` would in fact use
# it. But the build fallback is precisely the thing this script exists to
# avoid, and on a Pi "quietly started a from-source build" is a long silence,
# not an error anyone can act on. --no-build ("Don't build an image, even if
# it's policy") converts that fallback into an immediate failure, which is the
# outcome worth having. It is not the opposite of --pull: nothing re-pulls
# here, the image is already local by this line.
#
# No --build for the obvious reason, and no `--pull always` either: the pull
# already happened above, where its failure could be explained properly.
#
# No --remove-orphans, unlike update.sh: that is evaluated against the whole
# project, and this invocation is deliberately scoped to one service.
#
# `app` by name, so Caddy, the docker proxy and any enabled profile services
# are left running untouched. Compose starts the named services plus whatever
# they depend on; `app` depends on nothing, and caddy's depends_on points the
# other way, so exactly one container is recreated.
#
# That recreate is not something this script asks for. Compose does it because
# the image changed: a freshly pulled image has a different ID than the one the
# running container was created from, and "if you change a service's
# configuration or image, docker compose up picks up the changes by stopping
# and recreating the containers". If CI has published nothing new since the
# last run, nothing is recreated and this is a no-op, which is the right
# behaviour for something that might end up on a timer.
sudo docker compose up -d --no-build app

NEW_COMMIT="$(git rev-parse HEAD)"
echo "new_commit=${NEW_COMMIT}"

# What actually got deployed, which the commit above deliberately does not tell
# you. Non-fatal: it is a report, and failing the update over a failed report
# would be silly.
sudo docker compose images app || true

# Only `app` was recreated, so a compose-file change reaching any other service
# is still sitting unapplied. Say so rather than let it be discovered later.
if ! git diff --quiet "${PREV_COMMIT}" "${NEW_COMMIT}" -- docker-compose.yml Caddyfile; then
  echo
  echo "note: docker-compose.yml or the Caddyfile changed in this pull, and this"
  echo "      script recreated only the 'app' service. Run 'sudo docker compose up -d'"
  echo "      if that change touched another service."
fi

echo "Update complete."
}
