#!/usr/bin/env python3
"""Loreline's update trigger: one HTTP route that runs deploy/update-fast.sh.

The web UI's "Update now" button cannot update a Docker deployment by itself.
Doing so means stopping and recreating this stack's ``app`` container, which
needs the Docker socket, and the socket is root on the host - not access the app
container gets. So it is held here instead, by a container that does one thing,
answers one route, and takes no argument at all from the caller.

What this is not is a registry client. Both third-party updaters this replaces
reimplemented parts of the registry protocol themselves, and the second was
dropped for exactly that: its GHCR provider skips the anonymous OCI token
exchange and sends a placeholder where a bearer token belongs, so it cannot see
a public package at all. Nothing here talks to a registry. It runs
``deploy/update-fast.sh``, which runs the real ``docker`` and ``docker compose``
CLIs, which do the real thing. One script holds the update logic, on the host
and in here both; this file is the doorbell.

Two stages, and the split is not cosmetic - it is the only arrangement that can
answer honestly. ``docker compose up -d --no-build app`` stops the app
container, and the app container is the one waiting on this response: Compose
waits for it to exit, it waits for uvicorn to finish the in-flight request, and
that request is waiting on Compose. The cycle breaks when Docker SIGKILLs the
app at the end of its stop grace period, so one synchronous run can never report
back to the caller it is about to kill. Instead:

  1. ``UPDATE_STAGE=pull`` - git pull, then ``docker compose pull app``. Slow (a
     couple of GB the first time) but it touches nothing that is running, so the
     caller is alive to hear the result, including whether the image changed.
  2. Answer.
  3. ``UPDATE_STAGE=apply`` - ``docker compose up -d --no-build app``, on a
     background thread, and only if step 1 found something to apply. Pressing
     the button on an up-to-date box therefore restarts nothing at all.

Standard library only, deliberately. This container holds the Docker socket, so
"short enough to read in one sitting" is a security property here, not tidiness.
"""

from __future__ import annotations

import hmac
import json
import os
import subprocess
import sys
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

# The checkout, at the same absolute path it has on the host. That equality is
# load-bearing rather than tidy: `docker compose` here talks to the host's
# daemon, so a relative bind mount in docker-compose.yml (./data, ./Caddyfile)
# is resolved against this project directory and then handed to the host to
# interpret. Mount the repo at /srv and the recreated app container would bind
# /srv/data on the host - a path Docker would helpfully create, empty, and the
# app would come back having apparently lost every session. Compose also derives
# the project name from this directory's basename, so the same equality is what
# keeps this from creating a second, parallel stack instead of updating the one
# that is running. docker-compose.yml mounts ${UPDATER_REPO_DIR} at
# ${UPDATER_REPO_DIR} for that reason; _repo_problem() below refuses to run if
# the two ever come apart.
REPO_DIR = Path(os.environ.get("UPDATER_REPO_DIR", "/opt/loreline"))
SCRIPT = REPO_DIR / "deploy" / "update-fast.sh"

# Empty means "no token configured", and every request is refused. Compose can
# deliver this either absent or present-but-empty depending on how .env spells
# it, which is why this reads as a plain falsy string rather than testing
# whether the name is in the environment at all.
TOKEN = os.environ.get("UPDATER_TOKEN", "")

PORT = int(os.environ.get("UPDATER_PORT", "8080"))
# A first pull is the whole image, a couple of GB, over whatever connection the
# box has. Generous on purpose; the app's own read timeout sits just above it.
TIMEOUT_SECONDS = float(os.environ.get("UPDATER_TIMEOUT_SECONDS", "1800"))
# Enough of the transcript to be worth reading, capped so a pull's progress
# spam cannot become the response body.
MAX_OUTPUT = 16000
# Nothing is meant to send a body at all; this is only how much of an
# unexpected one is drained rather than left in the socket.
MAX_REQUEST_BODY = 8192
# Not for our own write ordering - the response is already flushed by the time
# the apply stage starts. This is for the app: it has to receive that response
# and relay it to the browser before Compose stops it, and that is the whole
# reason this stage is deferred rather than run inline.
SETTLE_SECONDS = 2.0

_STAGE_PULL = "pull"
_STAGE_APPLY = "apply"

# One update at a time. Held across the answer and the background apply, so a
# second press during the recreate is refused rather than racing it.
_update_lock = threading.Lock()


def log(event: str, **fields: object) -> None:
    """One JSON line per event on stdout, which is where `docker logs` looks."""
    print(json.dumps({"event": event, **fields}), flush=True)


def _repo_problem() -> str | None:
    """Explain why the mounted checkout is unusable, or None if it is fine.

    Checked per request rather than once at startup, and answered with a message
    instead of a crash: a container that exits here would restart-loop, and the
    operator would be reading Docker's backoff messages instead of this one.
    """
    if not REPO_DIR.is_dir():
        return (
            f"{REPO_DIR} is not mounted in the updater container. Set UPDATER_REPO_DIR "
            "in .env to the absolute path of this checkout on the host and run "
            "`docker compose --profile updater up -d` again."
        )
    if not (REPO_DIR / ".git").exists():
        return (
            f"{REPO_DIR} is mounted but is not a git checkout, so there is nothing here "
            "to update from. UPDATER_REPO_DIR in .env has to name this repository's "
            "directory on the host, by its absolute path."
        )
    if not SCRIPT.is_file():
        return f"{SCRIPT} is missing from the mounted checkout, so there is no script to run."
    return None


def _run_stage(stage: str) -> tuple[int, str]:
    """Run one stage of the update script, returning its exit code and transcript."""
    env = dict(os.environ)
    env["UPDATE_STAGE"] = stage
    # This runs as root against a checkout owned by whoever cloned it, and git
    # refuses that outright: its ownership check applies to root too, which gets
    # a pass only when the repo is root-owned or when SUDO_UID happens to name
    # the owner, and neither is true of a container entrypoint. The GIT_CONFIG_*
    # triple is git's documented env form of `git config --add safe.directory`,
    # scoped to this one path rather than the `*` that would waive the check
    # everywhere.
    env["GIT_CONFIG_COUNT"] = "1"
    env["GIT_CONFIG_KEY_0"] = "safe.directory"
    env["GIT_CONFIG_VALUE_0"] = str(REPO_DIR)
    log("updater.stage.start", stage=stage)
    try:
        proc = subprocess.run(
            ["bash", str(SCRIPT)],
            cwd=str(REPO_DIR),
            env=env,
            # Merged rather than captured apart: the script writes its
            # explanations (the GHCR-visibility one especially) to stderr and
            # its progress to stdout, and they only make sense interleaved.
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        # `text=True` above makes this a str, but TimeoutExpired.stdout is None
        # when nothing was captured before the clock ran out.
        partial = exc.stdout if isinstance(exc.stdout, str) else ""
        log("updater.stage.timeout", stage=stage, seconds=TIMEOUT_SECONDS)
        return -1, f"{partial}\nTimed out after {TIMEOUT_SECONDS:.0f}s and was stopped."
    log("updater.stage.done", stage=stage, returncode=proc.returncode)
    return proc.returncode, proc.stdout


def _field(output: str, key: str) -> str:
    """Read a ``key=value`` line the script prints for machine consumption."""
    prefix = f"{key}="
    for line in reversed(output.splitlines()):
        if line.startswith(prefix):
            return line[len(prefix) :].strip()
    return ""


def _pull() -> tuple[dict[str, object], bool]:
    """Run the pull stage and decide whether an apply stage is worth running.

    Returns the response payload and that decision. "Nothing to apply" has to be
    provable, not merely likely: only a definite ``image_changed=0`` together
    with an unmoved commit skips the apply. Anything the script could not
    determine falls through to applying, because `up -d` on an unchanged service
    is a no-op, whereas skipping a real update is a silent failure.
    """
    code, output = _run_stage(_STAGE_PULL)
    output = output[-MAX_OUTPUT:]
    if code != 0:
        return {"ok": False, "changed": False, "returncode": code, "output": output}, False
    previous, new = _field(output, "previous_commit"), _field(output, "new_commit")
    settled = _field(output, "image_changed") == "0" and bool(previous) and previous == new
    return {
        "ok": True,
        "changed": not settled,
        "returncode": 0,
        "output": output,
    }, not settled


def _apply_and_release() -> None:
    """Recreate the app container, then let the next update through.

    Runs on its own thread with the answer already sent, which is the point: the
    container this recreates is the one that asked for the update.
    """
    try:
        time.sleep(SETTLE_SECONDS)
        code, output = _run_stage(_STAGE_APPLY)
        # Nobody is left to tell. The caller was replaced by this very stage, so
        # the log is the only record there can be of how it went.
        log("updater.applied" if code == 0 else "updater.apply_failed", returncode=code)
        if code != 0:
            log("updater.apply_output", output=output[-MAX_OUTPUT:])
    finally:
        _update_lock.release()


class Handler(BaseHTTPRequestHandler):
    """POST /update, bearer token, nothing else."""

    # Definite framing on every answer: httpx gets a Content-Length and a close
    # rather than having to wait out a keep-alive window it will never reuse.
    protocol_version = "HTTP/1.1"
    server_version = "loreline-updater"
    sys_version = ""

    def log_message(self, format: str, *args: Any) -> None:
        """Drop the default per-request stderr line; log() covers what matters."""

    def _reply(self, status: HTTPStatus, payload: dict[str, object]) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()

    def _refuse(self, status: HTTPStatus, detail: str) -> None:
        self._reply(status, {"ok": False, "changed": False, "returncode": 0, "output": detail})

    def _authorized(self) -> bool:
        """Check the bearer token in constant time, or refuse and say why."""
        if not TOKEN:
            log("updater.unconfigured")
            self._refuse(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "The updater service has no UPDATER_TOKEN configured, so it cannot accept "
                "an update request. deploy/install.sh generates one in .env.",
            )
            return False
        header = self.headers.get("Authorization", "")
        scheme, _, presented = header.partition(" ")
        # compare_digest over ==, so a wrong token cannot be narrowed down by
        # timing how long the comparison took. Small blast radius, but this is
        # the only thing standing between the compose network and a container
        # that holds the Docker socket.
        if scheme.lower() != "bearer" or not hmac.compare_digest(presented.strip(), TOKEN):
            log("updater.unauthorized")
            self._refuse(HTTPStatus.UNAUTHORIZED, "Bad or missing bearer token.")
            return False
        return True

    def _discard_body(self) -> None:
        """Read whatever the caller sent so the socket is drained before closing.

        Nothing here reads a request body - the endpoint takes no arguments at
        all, which is most of why a leaked token is worth so little - but an
        unread body would sit in the socket when the connection closes.
        """
        length = int(self.headers.get("Content-Length") or 0)
        if 0 < length <= MAX_REQUEST_BODY:
            self.rfile.read(length)

    # do_POST, not do_post: BaseHTTPRequestHandler dispatches on the method name
    # spelled exactly this way.
    def do_POST(self) -> None:
        self._discard_body()
        if self.path.rstrip("/") != "/update":
            self._refuse(
                HTTPStatus.NOT_FOUND, "This service answers POST /update and nothing else."
            )
            return
        if not self._authorized():
            return
        problem = _repo_problem()
        if problem is not None:
            log("updater.repo_unusable", repo=str(REPO_DIR))
            self._refuse(HTTPStatus.SERVICE_UNAVAILABLE, problem)
            return
        if not _update_lock.acquire(blocking=False):
            self._refuse(
                HTTPStatus.CONFLICT,
                "An update is already running. Wait for it to finish before starting another.",
            )
            return
        apply_after = False
        try:
            payload, apply_after = _pull()
            self._reply(HTTPStatus.OK, payload)
        finally:
            if apply_after:
                # Deliberately not a daemon thread: the recreate has to finish
                # even if the process is asked to stop while it runs.
                threading.Thread(target=_apply_and_release, name="apply").start()
            else:
                _update_lock.release()


def main() -> int:
    problem = _repo_problem()
    log(
        "updater.listening",
        port=PORT,
        repo=str(REPO_DIR),
        token_configured=bool(TOKEN),
        repo_problem=problem,
    )
    if not TOKEN:
        # Not fatal, on purpose. Refusing to boot would leave the operator
        # reading a restart loop instead of this line, and every request is
        # refused with the same explanation anyway.
        log("updater.warning", detail="UPDATER_TOKEN is empty; every request will be refused")
    ThreadingHTTPServer(("", PORT), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
