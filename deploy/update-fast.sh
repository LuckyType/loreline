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
#
# Run by hand it does the whole job and needs no arguments. It is also what the
# optional `updater` service runs (services/updater/updater.py), and that
# service needs the two halves separately, which is what UPDATE_STAGE is for:
#
#   all    the default, and the only value a person should ever need: both
#          halves in order, exactly as this script has always behaved
#   pull   git pull and `docker compose pull app`. Fetches everything, changes
#          nothing that is running
#   apply  `docker compose up -d --no-build app`, the half that replaces the
#          running container
#
# The split exists because the app container is what asks the updater service
# for an update, and the apply half is what stops the app container. Run in one
# piece, the result would be owed to a process that is already dead. Split, the
# answer goes out after the pull and the recreate follows it.
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

STAGE="${UPDATE_STAGE:-all}"
case "${STAGE}" in
  all | pull | apply) ;;
  *)
    echo "UPDATE_STAGE must be all, pull or apply (got '${STAGE}')." >&2
    exit 2
    ;;
esac

# Every docker call goes through here so the privilege decision is made once.
# On a host this is a normal user with sudo, which is what it has always been.
# In the updater container there is no sudo installed and none needed: it is
# root already, and the socket is bind-mounted. Deciding on EUID rather than an
# env var means neither side has to be told which one it is, and it also stops a
# root-run invocation on the host from becoming `sudo sudo docker`.
if [[ ${EUID} -eq 0 ]]; then
  run_docker() { docker "$@"; }
else
  run_docker() { sudo docker "$@"; }
fi

PREV_COMMIT="$(git rev-parse HEAD)"
echo "previous_commit=${PREV_COMMIT}"

# The pull stage. Its body is left at column 0, like the `{ }` wrapper's above
# and for a related reason: the operator-facing messages below are heredocs,
# whose bodies are printed exactly as written, so indenting the block would
# either indent every line of those messages or leave them visibly detached
# from the code they belong to. Neither is worth the two spaces.
if [[ ${STAGE} != apply ]]; then

# The image is the only thing this script skips building. Everything needed to
# interpret it correctly still arrives by git: docker-compose.yml itself, the
# Caddyfile, new env var names, new services. Note that the commits printed
# here and at the end therefore describe this *checkout*, not the image that
# ends up running, which is whatever CI last published to main.
git fetch --quiet origin
git pull --ff-only origin main

# The /dev/snd passthrough used to be a line in docker-compose.yml and is now
# an override file, because the unconditional mount failed outright on every
# host without a sound card. That inversion has one casualty if nothing is done
# here: a box that *was* recording locally has no override file, so this update
# would take its microphone away without saying so.
#
# Decided from git rather than by probing /dev/snd, which means the answer is
# the same whether this runs on the host or inside the updater container (where
# no sound device is mounted and a probe would say no): if the compose file
# this box was running mounted the device, and nothing overrode it, then it was
# recording locally and it still should be. Written once - after this the
# override file exists, so the condition is false forever.
#
# Captured into a variable rather than piped into `grep -q`, which would
# SIGPIPE `git show` and, under `set -o pipefail`, fail the update.
PREV_COMPOSE="$(git show "${PREV_COMMIT}:docker-compose.yml" 2>/dev/null || true)"
if [[ ${PREV_COMPOSE} == *"/dev/snd:/dev/snd"* ]]; then
  if [[ ! -f docker-compose.override.yml ]]; then
    cp deploy/mic-passthrough.override.yml docker-compose.override.yml
    echo "note: this box passed the host microphone through, and that setting has moved"
    echo "      out of docker-compose.yml. Wrote docker-compose.override.yml to keep it."
    echo "      Delete that file and re-run this script to record from a browser instead."
  elif grep -q 'Written by deploy/install.sh' docker-compose.override.yml &&
    grep -q 'reset' docker-compose.override.yml; then
    # The other side of the same move: the override deploy/install.sh used to
    # write on a box with no microphone exists only to take the mount away
    # again. There is nothing left for it to take, and an override that resets
    # a key the base file no longer has is a line nobody should have to reason
    # about later. The box keeps exactly the behaviour it had - no device.
    rm -f docker-compose.override.yml
    echo "note: removed the docker-compose.override.yml that disabled the /dev/snd"
    echo "      passthrough. docker-compose.yml no longer mounts it, so that file had"
    echo "      nothing left to switch off. Nothing about this box changes."
  fi
fi

# Which image `app` resolves to, asked of Compose rather than read out of the
# YAML, so interpolation and any docker-compose.override.yml are already
# applied. Recorded before and after the pull, because comparing the two image
# IDs is the whole of "did anything actually change" - and knowing that is what
# lets the updater service skip the recreate entirely on an up-to-date box, and
# say so, rather than restarting the app to discover there was nothing to do.
#
# `config --images` prints one line per image and appends a service's dependent
# images after the first, so only the first line is the answer. Taken with
# parameter expansion and not `| head -1`, which would SIGPIPE Compose and,
# under `set -o pipefail`, fail the script (see have_pkg in deploy/install.sh
# for the same trap).
IMAGE_REF=""
if IMAGE_LIST="$(run_docker compose config --images app 2>/dev/null)"; then
  IMAGE_REF="${IMAGE_LIST%%$'\n'*}"
fi
IMAGE_BEFORE=""
if [[ -n ${IMAGE_REF} ]]; then
  IMAGE_BEFORE="$(run_docker image inspect --format '{{.Id}}' "${IMAGE_REF}" 2>/dev/null || true)"
fi

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
if ! run_docker compose pull app 2>&1 | tee "${PULL_LOG}"; then
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

# The same reference, re-resolved. The pull replaces what that tag points at, so
# a different image ID here means a genuinely newer image and an identical one
# means this box was already current.
IMAGE_AFTER=""
if [[ -n ${IMAGE_REF} ]]; then
  IMAGE_AFTER="$(run_docker image inspect --format '{{.Id}}' "${IMAGE_REF}" 2>/dev/null || true)"
fi
if [[ -z ${IMAGE_REF} ]]; then
  # Compose could not name the image, so this cannot answer the question.
  # "unknown" is not "no": whoever reads it should apply anyway, because
  # `up -d` on an unchanged service is a no-op while a skipped update is a
  # silent failure.
  echo "image_changed=unknown"
elif [[ ${IMAGE_AFTER} == "${IMAGE_BEFORE}" ]]; then
  echo "image_changed=0"
else
  echo "image_changed=1"
fi

fi # end of the pull stage

NEW_COMMIT="$(git rev-parse HEAD)"
echo "new_commit=${NEW_COMMIT}"

# Only `app` is recreated, so a compose-file change reaching any other service
# is still sitting unapplied. Say so rather than let it be discovered later.
# Printed here, by the half that knows both commits, rather than after the
# recreate where it used to sit: split into stages, the apply half is a separate
# run of this script and never sees the pull's before and after. In that half
# the two commits are the same one, so the diff is empty and this stays quiet.
if ! git diff --quiet "${PREV_COMMIT}" "${NEW_COMMIT}" -- docker-compose.yml Caddyfile; then
  echo
  echo "note: docker-compose.yml or the Caddyfile changed in this pull, and only the"
  echo "      'app' service is recreated. Run 'docker compose up -d' if that change"
  echo "      touched another service."
fi

# The same argument again, for the services this repo builds rather than pulls.
# `docker compose pull app` fetches one image from a registry; the diarization
# service and the updater are built from this checkout and published nowhere, so
# a release that changes them arrives with the git pull above and then simply
# sits there. That was found the hard way: a deploy of a diarization fix left
# the container running month-old code while the checkout beside it was current,
# and nothing said so.
#
# Printed here rather than after the recreate, for the reason the note above
# gives: the apply half is a separate run of this script, it never sees the
# pull's before and after, and its output reaches nobody because the caller has
# already been answered by then. Conditional on the diff, so a box whose
# diarizer is untouched by a release stays quiet, and on the service actually
# existing, so nobody is told to rebuild something they do not run. `docker ps`
# and not `compose ps`, which filters by active profile.
for svc in diarization updater; do
  if git diff --quiet "${PREV_COMMIT}" "${NEW_COMMIT}" -- "services/${svc}"; then
    continue
  fi
  present="$(run_docker ps -a --filter "label=com.docker.compose.service=${svc}" \
    --format '{{.Names}}' 2>/dev/null || true)"
  if [[ -n ${present} ]]; then
    echo
    echo "note: this pull changed services/${svc}, which is built from this checkout"
    echo "      rather than pulled, so this path does not update it. To rebuild it:"
    echo "        docker compose --profile ${svc} up -d --build ${svc}"
  fi
done

if [[ ${STAGE} == pull ]]; then
  echo "Pull complete. Nothing has been applied yet."
  exit 0
fi

# `--no-build` is the load-bearing flag here, and the reason this is not just
# `docker compose up -d app`.
#
# The `app` service defines both `build:` and `image:`, and Compose resolves
# that pair through pull_policy: with no pull_policy set, per the Compose Build
# Specification, "Compose attempts to pull the image first and then builds from
# source if the image isn't found in the registry or platform cache". The pull
# stage already put it in the local cache, so a plain `up -d` would in fact use
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
run_docker compose up -d --no-build app

# What actually got deployed, which the commits above deliberately do not tell
# you. Non-fatal: it is a report, and failing the update over a failed report
# would be silly.
run_docker compose images app || true

# The diarization service is built from this checkout and published to no
# registry, so this path cannot update it at all: there is nothing to pull, and
# the recreate above is scoped to `app` whether or not the diarization profile
# is active. That is not a reason to change what this script does - the fast
# path exists precisely to touch one service - but it is a reason to say so
# when that service is actually running, instead of leaving an operator to
# assume "Update complete" covered it. `docker ps` and not `compose ps`, which
# filters by active profile; captured and matched rather than piped into
# `grep -q`, which would SIGPIPE the producer under `set -o pipefail`.
# Nothing here about the services built from this checkout: that note moved into
# the pull stage above, where it is conditional on the release actually having
# changed one and where the caller is still listening. Printed here it reached
# no one, because the app has already been answered and is about to be replaced.

echo "Update complete."
}
