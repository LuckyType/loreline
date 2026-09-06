"""Self-update for the source+systemd deployment.

Drives ``deploy/update-source.sh`` (``git pull --ff-only && uv sync &&
systemd-run ... systemctl restart``), captures its output, and records the
previous/new commit so the UI can offer a rollback. ``rollback`` resets to a
prior commit, re-syncs, and restarts. The subprocess runner is injectable for
offline tests.

None of this applies to a Docker deployment: there's no systemd unit inside
the container for it to restart, and giving the container the Docker-socket
access it would need to update *itself* is effectively root on the host -
not a trade this makes for you silently. ``update``/``rollback`` detect that
case (``/.dockerenv``, standard Docker marker) and point at the host-side
``deploy/update.sh`` instead of attempting the source-only mechanics.

There is one way out of that for ``update``, and it does not move the socket
boundary an inch: the optional ``updater`` service in docker-compose.yml holds
that access for its own audited reason, and answers one HTTP route that runs
``deploy/update-fast.sh``. So when a URL and a token are configured, ``update``
posts to it over the compose network and lets the container that already has
the socket do the work. This app still never sees it, and the request carries
no arguments at all - nothing in it names an image, a tag or a container, so it
cannot ask for anything but this one update.

What comes back is a real result rather than a guess, which is new. The two
third-party updaters this replaces both applied the update from inside the
container being replaced, so neither could report how it went. This one is a
separate container that ``docker compose up -d --no-build app`` does not touch,
and it answers after the pull and before the recreate. See ``_trigger_updater``.
"""

from __future__ import annotations

from http import HTTPStatus
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
from pydantic import BaseModel

from loreline.logging import get_logger
from loreline.updater.process import run_command

if TYPE_CHECKING:
    from loreline.updater.process import CommandRunner

log = get_logger(__name__)

_MAX_OUTPUT = 8000
_DOCKER_MARKER = Path("/.dockerenv")
# The updater service runs the whole pull half of deploy/update-fast.sh before
# it answers, and on a first run that is the entire image over whatever
# connection the box has - so the read timeout is minutes, not seconds, and it
# sits just above that service's own subprocess cap (UPDATER_TIMEOUT_SECONDS,
# 1800s) so an overrun is reported by the side that knows why. Connecting, by
# contrast, either happens at once or is not going to: a short connect timeout
# is what tells "the updater profile isn't running" apart from "this is taking
# a while", and only the first of those should fall back to the host message.
_UPDATE_TIMEOUT = httpx.Timeout(1830.0, connect=2.0)
_CONTAINER_MESSAGE = (
    "Running in a Docker deployment - self-update from the web UI isn't "
    "available here (there's no systemd unit inside the container to "
    "restart, and granting that access would mean handing the container "
    "the Docker socket, i.e. effectively root on the host). Update from the "
    "host instead: deploy/update.sh - or enable automatic updates with "
    "`sudo systemctl enable --now loreline-update.timer`."
)
_APPLYING_MESSAGE = (
    "A newer image was pulled. The app container is being recreated onto it now, "
    "which takes a moment and ends any recording - this page loses its connection "
    "until the app comes back."
)
_UP_TO_DATE_MESSAGE = (
    "Already up to date. The registry was checked just now and the running image is "
    "the newest one published, so nothing was applied and nothing was restarted."
)
_REJECTED_MESSAGE = (
    "The updater service rejected this app's token. LORELINE_UPDATER_TOKEN here and "
    "UPDATER_TOKEN there are both filled from the one UPDATER_TOKEN entry in .env, "
    "which deploy/install.sh generates - check they still agree, and that the app "
    "was recreated after .env last changed. Update from the host meanwhile: "
    "deploy/update.sh"
)
_BUSY_MESSAGE = (
    "An update is already running. Wait for that one to finish rather than starting a second one."
)
_FAILED_MESSAGE = (
    "The updater service reported a failed update without saying why. "
    "`docker compose logs updater` has the detail; update from the host meanwhile: "
    "deploy/update.sh"
)


class _UpdaterReply(BaseModel):
    """What the updater service answers with; see services/updater/updater.py.

    Every field has a default so a trimmed or unexpected body still parses: a
    reply this app cannot read is reported as an unexpected answer, and a reply
    missing a field it did not send is not the same thing.
    """

    ok: bool = False
    changed: bool = False
    returncode: int = 0
    output: str = ""


class UpdateResult(BaseModel):
    """Result of an update or rollback attempt (safe to return via API)."""

    ok: bool
    previous_commit: str | None = None
    new_commit: str | None = None
    returncode: int = 0
    output: str = ""


class Updater:
    """Run the self-update script and report status."""

    def __init__(
        self,
        *,
        app_dir: Path,
        unit: str = "loreline",
        runner: CommandRunner | None = None,
        in_container: bool | None = None,
        updater_url: str = "",
        updater_token: str = "",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._app_dir = app_dir
        self._unit = unit
        self._run: CommandRunner = runner or run_command
        self._script = app_dir / "deploy" / "update-source.sh"
        self._in_container = in_container if in_container is not None else _DOCKER_MARKER.exists()
        self._updater_url = updater_url.rstrip("/")
        self._updater_token = updater_token
        self._client = client

    async def current_revision(self) -> str | None:
        """Return the current git HEAD commit, or None if unavailable."""
        result = await self._run(["git", "rev-parse", "HEAD"], cwd=str(self._app_dir))
        return result.stdout.strip() if result.ok else None

    async def _message_result(self, message: str, *, ok: bool = False) -> UpdateResult:
        """Report ``message`` against the current revision, having changed nothing."""
        revision = await self.current_revision()
        return UpdateResult(ok=ok, previous_commit=revision, new_commit=revision, output=message)

    async def _trigger_updater(self) -> UpdateResult | None:
        """Ask the sibling updater container to update this stack.

        ``None`` means there is no trigger to use - unconfigured, or nothing
        answered - and the caller falls back to the host-side message.

        One request, and it can be waited on, which is the whole difference from
        what came before. The updater service runs the pull half of
        ``deploy/update-fast.sh``, answers with what that found, and only then
        recreates this container. So the answer is a fact rather than a guess
        read off a timeout: either a newer image was pulled and the recreate is
        under way, or this box was already current and nothing was touched.
        """
        if not (self._updater_url and self._updater_token):
            return None
        client = self._client or httpx.AsyncClient()
        try:
            return await self._ask_updater(client)
        finally:
            if self._client is None:
                await client.aclose()

    async def _ask_updater(self, client: httpx.AsyncClient) -> UpdateResult | None:
        """Post the update request, or report that nothing answered."""
        try:
            response = await client.post(
                f"{self._updater_url}/update",
                # Bearer rather than Basic: no third-party auth scheme
                # constrains the choice any more, and a bearer token is what the
                # rest of this project carries. The far side compares it with
                # hmac.compare_digest. The request has no body on purpose -
                # there is nothing to name, so there is nothing to abuse.
                headers={"Authorization": f"Bearer {self._updater_token}"},
                timeout=_UPDATE_TIMEOUT,
            )
        except httpx.HTTPError as exc:
            # Connect errors land here: nothing is listening, so the updater
            # profile is not running (or not reachable), and this deployment is
            # in exactly the position it was in before any of this existed. A
            # run that outlasts the read timeout lands here too, and the same
            # fallback is the honest answer - this app genuinely does not know
            # how it ended, and the updater's own log does.
            log.info("update.updater.unreachable", error=str(exc))
            return None
        return await self._read_update(response)

    async def _read_update(self, response: httpx.Response) -> UpdateResult:
        """Say what the updater service said, in this UI's own words."""
        if response.status_code == HTTPStatus.UNAUTHORIZED:
            log.warning("update.updater.unauthorized")
            return await self._message_result(_REJECTED_MESSAGE)
        if response.status_code == HTTPStatus.CONFLICT:
            log.info("update.updater.busy")
            return await self._message_result(_BUSY_MESSAGE)
        try:
            # ValueError covers both halves: a body that isn't JSON, and JSON
            # that isn't the shape this expects (pydantic's ValidationError is a
            # ValueError).
            reply = _UpdaterReply.model_validate(response.json())
        except ValueError:
            log.warning("update.updater.unreadable_response", status=response.status_code)
            return await self._message_result(self._unexpected(response.status_code))
        if not response.is_success:
            # 503 (no token configured over there, or the checkout is not
            # mounted where it expects) and 404 both arrive here carrying a
            # one-line explanation from the side that knows which it is. Repeat
            # that rather than translate it into something vaguer.
            log.warning("update.updater.refused", status=response.status_code)
            return await self._message_result(
                reply.output or self._unexpected(response.status_code)
            )
        if not reply.ok:
            # The update script failed, and its transcript is by far the most
            # useful thing to hand back, so it goes back whole. Several lines is
            # also the signal Settings > Client uses to show the output pane
            # rather than a one-line message. deploy/update-fast.sh writes a
            # specific explanation for the GHCR-visibility case that would be a
            # waste to replace with a generic one here.
            log.warning("update.updater.failed", returncode=reply.returncode)
            revision = await self.current_revision()
            return UpdateResult(
                ok=False,
                previous_commit=revision,
                new_commit=revision,
                returncode=reply.returncode,
                output=reply.output[-_MAX_OUTPUT:] or _FAILED_MESSAGE,
            )
        log.info("update.updater.done", changed=reply.changed)
        return await self._message_result(
            _APPLYING_MESSAGE if reply.changed else _UP_TO_DATE_MESSAGE, ok=True
        )

    @staticmethod
    def _unexpected(status: int) -> str:
        """One-line report for an answer this app can't read as a result."""
        return (
            f"The updater service answered with HTTP {status}, which this app can't read "
            "as a result. `docker compose logs updater` has the detail; update from the "
            "host meanwhile: deploy/update.sh"
        )

    async def update(self) -> UpdateResult:
        """Update this deployment, by whichever route it has.

        A source deployment runs ``deploy/update-source.sh`` and reports the
        before/after commits. A Docker one has no such route of its own and asks
        the updater service instead, falling back to the host-side message when
        that isn't configured or isn't there.
        """
        if self._in_container:
            triggered = await self._trigger_updater()
            if triggered is not None:
                return triggered
            return await self._message_result(_CONTAINER_MESSAGE)
        before = await self.current_revision()
        result = await self._run(["bash", str(self._script)], cwd=str(self._app_dir))
        after = await self.current_revision()
        if result.ok:
            log.info("update.done", previous=before, new=after)
        else:
            log.warning("update.failed", returncode=result.returncode)
        return UpdateResult(
            ok=result.ok,
            previous_commit=before,
            new_commit=after,
            returncode=result.returncode,
            output=result.combined[-_MAX_OUTPUT:],
        )

    async def rollback(self, commit: str) -> UpdateResult:
        """Reset to ``commit``, re-sync dependencies, and restart the service."""
        if self._in_container:
            # No updater-service equivalent here on purpose. That service runs
            # deploy/update-fast.sh, which only ever moves forward to whatever
            # the registry currently publishes; rolling back means naming an
            # older image, and naming anything at all is exactly what that
            # endpoint refuses to accept.
            return await self._message_result(_CONTAINER_MESSAGE)
        before = await self.current_revision()
        chunks: list[str] = []
        returncode = 0
        for argv in (
            ["git", "reset", "--hard", commit],
            ["uv", "sync", "--frozen"],
            # Detached via systemd-run, not a plain restart: this runs from
            # inside the unit being restarted (see deploy/update-source.sh
            # for the same fix and why) - a direct restart kills this
            # request before it can respond, even though rollback succeeded.
            [
                "sudo",
                "systemd-run",
                "--on-active=2",
                "--unit=loreline-restart",
                "--collect",
                "systemctl",
                "restart",
                self._unit,
            ],
        ):
            result = await self._run(argv, cwd=str(self._app_dir))
            chunks.append(f"$ {' '.join(argv)}\n{result.combined}")
            if not result.ok:
                returncode = result.returncode
                break
        after = await self.current_revision()
        log.info("rollback.done" if returncode == 0 else "rollback.failed", commit=commit)
        return UpdateResult(
            ok=returncode == 0,
            previous_commit=before,
            new_commit=after,
            returncode=returncode,
            output="\n".join(chunks)[-_MAX_OUTPUT:],
        )
