# The container that holds the Docker socket so the web UI's Update button can
# work in a Docker deployment. Opt-in, behind the `updater` compose profile.
#
# `docker:29-cli` and not `docker:29`: the unsuffixed tags of this image are the
# dind variant, which ships a whole daemon this has no use for. The `-cli`
# suffix is the entire difference and it is easy to lose.
#
# What the base already provides, and why it was chosen rather than a bare
# Alpine plus packages: a current Docker CLI (29.8.0) and the Compose v5 plugin,
# both installed and smoke-tested by the image's own build, plus git and
# ca-certificates. The currency is the point. The archived watchtower this
# feature started on failed on the real box for precisely one reason - a bundled
# client that negotiated below the API 1.44 floor Engine 29 enforces - and
# tracking the `29` line here means the client stays in step with the daemon
# instead of aging in place.
FROM docker:29-cli

# The two things that image does not have. Alpine's /bin/sh is busybox ash,
# which cannot parse deploy/update-fast.sh at all (`[[ ]]`, `${BASH_SOURCE[0]}`,
# `set -o pipefail`), and there is no python3 for the service itself.
RUN apk add --no-cache bash python3

# The base entrypoint exports this itself when it finds a socket, but its
# fallback when it does not is to assume a host literally named "docker" on port
# 2375 - a leftover from Compose's links era. That turns a missing socket mount
# into a slow failure against a name that will never resolve. Naming the socket
# here makes the same mistake fail immediately, and says out loud what this
# container talks to.
ENV DOCKER_HOST=unix:///var/run/docker.sock

COPY updater.py /usr/local/bin/updater.py

# Documentary only, as in docker-compose.yml: nothing is published to the host.
EXPOSE 8080

# No USER: this runs as root, and pretending otherwise would be theatre. A
# container with the Docker socket is root on the host whatever uid it reports,
# and it additionally has to run git against a checkout owned by whoever cloned
# it. What limits the blast radius is the single argument-free endpoint, not a
# uid. See services/updater/updater.py and the README's "Updating" section.
#
# Left as CMD so the base image's entrypoint still runs: it is what would export
# DOCKER_HOST from a mounted socket, and replacing it would mean reimplementing
# that here for nothing.
CMD ["python3", "/usr/local/bin/updater.py"]
