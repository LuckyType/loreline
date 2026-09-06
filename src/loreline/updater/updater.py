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
boundary an inch: the optional ``watchtower`` service in docker-compose.yml
already holds that access, for its own audited reason, and 1.7.1 can expose
an HTTP endpoint that triggers the same update its daily schedule triggers.
So when a URL and token are configured, ``update`` sends one token-gated POST
over the compose network and lets the container that already has the socket
do the work. This app still never sees it. See ``_trigger_watchtower`` for
why it deliberately does not wait for the answer.
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
# Connect generously enough to tell "watchtower isn't running" apart from a
# busy host, then give up on *reading* fast: see _trigger_watchtower.
_TRIGGER_TIMEOUT = httpx.Timeout(3.0, connect=2.0)
_CONTAINER_MESSAGE = (
    "Running in a Docker deployment - self-update from the web UI isn't "
    "available here (there's no systemd unit inside the container to "
    "restart, and granting that access would mean handing the container "
    "the Docker socket, i.e. effectively root on the host). Update from the "
    "host instead: deploy/update.sh - or enable automatic updates with "
    "`sudo systemctl enable --now loreline-update.timer`."
)
_TRIGGERED_MESSAGE = (
    "Update triggered. Watchtower is checking the registry for a newer image; "
    "if it finds one it pulls it and restarts the app, which takes a few "
    "minutes and ends any recording. No result to report from here - the "
    "container answering you is the one being replaced."
)
_REJECTED_MESSAGE = (
    "Watchtower rejected the update trigger as unauthorized. Its "
    "WATCHTOWER_HTTP_API_TOKEN and this app's LORELINE_WATCHTOWER_TOKEN have "
    "to be the same value; docker-compose.yml reads both from one .env entry. "
    "Update from the host meanwhile: deploy/update.sh"
)


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
        watchtower_url: str = "",
        watchtower_token: str = "",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._app_dir = app_dir
        self._unit = unit
        self._run: CommandRunner = runner or run_command
        self._script = app_dir / "deploy" / "update-source.sh"
        self._in_container = in_container if in_container is not None else _DOCKER_MARKER.exists()
        self._watchtower_url = watchtower_url
        self._watchtower_token = watchtower_token
        self._client = client

    async def current_revision(self) -> str | None:
        """Return the current git HEAD commit, or None if unavailable."""
        result = await self._run(["git", "rev-parse", "HEAD"], cwd=str(self._app_dir))
        return result.stdout.strip() if result.ok else None

    async def _message_result(self, message: str, *, ok: bool = False) -> UpdateResult:
        """Report ``message`` against the current revision, having changed nothing."""
        revision = await self.current_revision()
        return UpdateResult(ok=ok, previous_commit=revision, new_commit=revision, output=message)

    async def _trigger_watchtower(self) -> UpdateResult | None:
        """Ask the sibling watchtower container to update this stack.

        ``None`` means there is no trigger to use - unconfigured, or nothing
        answered - and the caller falls back to the host-side message.

        The awkward part is that watchtower's ``/v1/update`` is synchronous:
        1.7.1's handler calls the update inline and writes nothing itself, so
        the implicit empty 200 lands only once it is over. That update recreates
        *this* container, so waiting for the response means waiting to be
        killed, and the browser gets nothing. Hence the short read timeout: a
        ``ReadTimeout`` here is the ordinary success path, not a failure. It
        proves the request was delivered, which is all this app can honestly
        know. Dropping the connection does not cancel anything either - the
        handler never consults the request context, it just runs.
        """
        if not (self._watchtower_url and self._watchtower_token):
            return None
        client = self._client or httpx.AsyncClient(timeout=_TRIGGER_TIMEOUT)
        try:
            # POST for the semantics; 1.7.1 registers the path for every method
            # and checks none of them. The header form is the one its
            # RequireToken middleware actually compares against - docs at that
            # tag also mention a bare `Token:` header, which its code ignores.
            response = await client.post(
                self._watchtower_url,
                headers={"Authorization": f"Bearer {self._watchtower_token}"},
            )
        except httpx.ReadTimeout:
            log.info("update.watchtower.triggered", detail="no answer within the read window")
            return await self._message_result(_TRIGGERED_MESSAGE, ok=True)
        except httpx.HTTPError as exc:
            # Connect errors land here: nothing is listening, so the watchtower
            # profile is not running (or not reachable), and this deployment is
            # in exactly the position it was in before any of this existed.
            log.info("update.watchtower.unreachable", error=str(exc))
            return None
        finally:
            if self._client is None:
                await client.aclose()
        if response.is_success:
            # A prompt 200 means the check finished before the read window was
            # up, so there was nothing new to pull (or another update was
            # already running, which 1.7.1 also answers 200 to). Same wording:
            # from here the two are indistinguishable.
            log.info("update.watchtower.triggered", status=response.status_code)
            return await self._message_result(_TRIGGERED_MESSAGE, ok=True)
        if response.status_code == HTTPStatus.UNAUTHORIZED:
            log.warning("update.watchtower.unauthorized")
            return await self._message_result(_REJECTED_MESSAGE)
        log.warning("update.watchtower.unexpected", status=response.status_code)
        return await self._message_result(
            f"Watchtower answered the update trigger with HTTP {response.status_code}, which "
            "this app can't read as a result. Update from the host instead: deploy/update.sh"
        )

    async def update(self) -> UpdateResult:
        """Update this deployment, by whichever route it has.

        A source deployment runs ``deploy/update-source.sh`` and reports the
        before/after commits. A Docker one has no such route of its own and
        asks watchtower instead, falling back to the host-side message when
        that isn't configured or isn't there.
        """
        if self._in_container:
            triggered = await self._trigger_watchtower()
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
            # No watchtower equivalent here on purpose: it only ever moves an
            # image forward to whatever the registry tag points at, and with
            # WATCHTOWER_CLEANUP the image being rolled back *to* is gone.
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
