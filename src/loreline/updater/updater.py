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
boundary an inch: the optional ``wud`` service in docker-compose.yml already
holds that access, for its own audited reason, and WUD ("What's Up Docker")
exposes an HTTP API that can re-check the registry and apply a newer image on
demand. So when a URL and credentials are configured, ``update`` drives that
API over the compose network and lets the container that already has the
socket do the work. This app still never sees it. See ``_trigger_wud`` for
the two-step flow, and for why the second step does not wait for an answer.
"""

from __future__ import annotations

from http import HTTPStatus
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
from pydantic import BaseModel, Field, TypeAdapter

from loreline.logging import get_logger
from loreline.updater.process import run_command

if TYPE_CHECKING:
    from loreline.updater.process import CommandRunner

log = get_logger(__name__)

_MAX_OUTPUT = 8000
_DOCKER_MARKER = Path("/.dockerenv")
# The trigger this app drives, as `<type>/<name>`. Not configurable because the
# other half of the pair is docker-compose.yml's WUD_TRIGGER_DOCKER_LOCAL_*,
# in this same repo: the two are edited together or not at all.
_WUD_TRIGGER = "docker/local"
# Step one asks WUD to poll the registry, so it has to allow for a round trip
# to GHCR (auth handshake, then a manifest fetch) on a slow home connection.
_WATCH_TIMEOUT = httpx.Timeout(20.0, connect=2.0)
# Step two connects generously enough to tell "wud isn't running" apart from a
# busy host, then gives up on *reading* fast: see _trigger_wud.
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
    "Update triggered. WUD is pulling the newer image and recreating the app "
    "container, which takes a few minutes and ends any recording. No result to "
    "report from here - the container answering you is the one being replaced."
)
_UP_TO_DATE_MESSAGE = (
    "Already up to date. WUD re-checked the registry just now and the running "
    "image is the newest one published, so there was nothing to apply."
)
_REJECTED_MESSAGE = (
    "WUD rejected the update trigger as unauthorized. This app's "
    "LORELINE_WUD_USER and LORELINE_WUD_PASSWORD have to be the username and "
    "the plaintext password behind WUD's WUD_AUTH_BASIC_LORELINE_USER and "
    "WUD_AUTH_BASIC_LORELINE_HASH; deploy/install.sh writes a matching set. "
    "Update from the host meanwhile: deploy/update.sh"
)
_TRIGGER_LOST_MESSAGE = (
    "WUD answered the update check but stopped answering before the update "
    "itself was asked for, so nothing has been applied. Try again, or update "
    "from the host: deploy/update.sh"
)
_NOT_WATCHED_MESSAGE = (
    "WUD is running but isn't watching this app's container, so there is "
    "nothing for it to update. Check that the app service still carries the "
    "`wud.watch=true` label from docker-compose.yml, and that LORELINE_WUD_IMAGE "
    "names its image. Update from the host meanwhile: deploy/update.sh"
)


class _WudImage(BaseModel):
    """The one field of WUD's image object this app matches on."""

    name: str = ""


class _WudContainer(BaseModel):
    """The few fields this app reads out of an entry in WUD's watch list.

    WUD sends a great deal more per container - registry credentials, digests,
    labels, its own update-kind breakdown. Naming only what is read here keeps
    that shape free to grow without breaking this, and every field has a
    default so a trimmed or unexpected entry parses instead of raising.
    """

    id: str = ""
    image: _WudImage = Field(default_factory=_WudImage)
    update_available: bool = Field(default=False, alias="updateAvailable")


_WUD_CONTAINERS = TypeAdapter(list[_WudContainer])


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
        wud_url: str = "",
        wud_user: str = "",
        wud_password: str = "",
        wud_image: str = "",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._app_dir = app_dir
        self._unit = unit
        self._run: CommandRunner = runner or run_command
        self._script = app_dir / "deploy" / "update-source.sh"
        self._in_container = in_container if in_container is not None else _DOCKER_MARKER.exists()
        self._wud_url = wud_url.rstrip("/")
        self._wud_user = wud_user
        self._wud_password = wud_password
        self._wud_image = wud_image
        self._client = client

    async def current_revision(self) -> str | None:
        """Return the current git HEAD commit, or None if unavailable."""
        result = await self._run(["git", "rev-parse", "HEAD"], cwd=str(self._app_dir))
        return result.stdout.strip() if result.ok else None

    async def _message_result(self, message: str, *, ok: bool = False) -> UpdateResult:
        """Report ``message`` against the current revision, having changed nothing."""
        revision = await self.current_revision()
        return UpdateResult(ok=ok, previous_commit=revision, new_commit=revision, output=message)

    def _find_own_container(self, containers: list[_WudContainer]) -> _WudContainer | None:
        """Pick this app's own container out of WUD's watch list.

        Matched on the image name rather than the id, because WUD's container
        id *is* the Docker container id: it changes every time the container is
        recreated, which is precisely what an update does. The name would work
        too, but it carries the compose project name, so it breaks for anyone
        who renamed the directory. The image is pinned in docker-compose.yml.
        """
        for container in containers:
            if container.id and container.image.name == self._wud_image:
                return container
        return None

    async def _wud_watch(
        self, client: httpx.AsyncClient, auth: httpx.Auth
    ) -> list[_WudContainer] | UpdateResult | None:
        """Make WUD re-check the registry now, and return what it reports.

        One call does both: it runs every watcher and answers with the
        resulting container list. ``None`` means nothing answered, so the
        caller falls back to the host-side message; an ``UpdateResult`` means
        WUD answered something only worth reporting verbatim.
        """
        try:
            response = await client.post(
                f"{self._wud_url}/api/containers/watch", auth=auth, timeout=_WATCH_TIMEOUT
            )
        except httpx.HTTPError as exc:
            # Connect errors land here: nothing is listening, so the wud profile
            # is not running (or not reachable), and this deployment is in
            # exactly the position it was in before any of this existed. A slow
            # registry lands here too, as a ReadTimeout - same answer, because
            # nothing has been applied either way.
            log.info("update.wud.unreachable", error=str(exc))
            return None
        if response.status_code == HTTPStatus.UNAUTHORIZED:
            log.warning("update.wud.unauthorized")
            return await self._message_result(_REJECTED_MESSAGE)
        if not response.is_success:
            log.warning("update.wud.watch_failed", status=response.status_code)
            return await self._message_result(self._unexpected(response.status_code))
        try:
            # ValueError covers both halves: a body that isn't JSON, and JSON
            # that isn't the list of containers this expects (pydantic's
            # ValidationError is a ValueError).
            return _WUD_CONTAINERS.validate_python(response.json())
        except ValueError:
            log.warning("update.wud.unreadable_watch_response")
            return await self._message_result(self._unexpected(response.status_code))

    async def _wud_fire(
        self, client: httpx.AsyncClient, auth: httpx.Auth, container: _WudContainer
    ) -> UpdateResult:
        """Apply the update WUD just reported, without waiting to be killed.

        This recreates *this* container, so waiting for the response means
        waiting to be killed and the browser gets nothing. Hence the short read
        timeout: a ``ReadTimeout`` here is the ordinary success path, not a
        failure. It proves the request was delivered, which is all this app can
        honestly know. Dropping the connection cancels nothing either - WUD
        runs the trigger to completion without consulting the request.
        """
        # WUD looks the container up in its own store from this id, so the
        # request carries no body: nothing here has to reserialize a container
        # faithfully enough for its trigger to accept, and nothing here can name
        # an image or a tag, so this cannot ask for anything but this update.
        url = f"{self._wud_url}/api/containers/{container.id}/triggers/{_WUD_TRIGGER}"
        try:
            response = await client.post(url, auth=auth, timeout=_TRIGGER_TIMEOUT)
        except httpx.ReadTimeout:
            log.info("update.wud.triggered", detail="no answer within the read window")
            return await self._message_result(_TRIGGERED_MESSAGE, ok=True)
        except httpx.HTTPError as exc:
            log.warning("update.wud.trigger_unreachable", error=str(exc))
            return await self._message_result(_TRIGGER_LOST_MESSAGE)
        if response.is_success:
            # A prompt 200 means the pull and recreate finished inside the read
            # window, which would be unusually fast but is not an error. Same
            # wording either way - this container is on its way out regardless.
            log.info("update.wud.triggered", status=response.status_code)
            return await self._message_result(_TRIGGERED_MESSAGE, ok=True)
        if response.status_code == HTTPStatus.UNAUTHORIZED:
            log.warning("update.wud.unauthorized")
            return await self._message_result(_REJECTED_MESSAGE)
        log.warning("update.wud.trigger_failed", status=response.status_code)
        return await self._message_result(self._unexpected(response.status_code))

    async def _trigger_wud(self) -> UpdateResult | None:
        """Ask the sibling WUD container to update this stack.

        ``None`` means there is no trigger to use - unconfigured, or nothing
        answered - and the caller falls back to the host-side message.

        Two steps, and the first one is not optional. WUD splits detection from
        application: its watcher cron decides whether an update *is available*,
        and the trigger applies whatever that decided. Firing the trigger alone
        would act on however stale that verdict is, and worse, the docker
        trigger builds the tag to pull out of ``updateKind.remoteValue``, which
        is ``undefined`` while no update is known - so triggering a container
        WUD believes is current doesn't no-op, it tries to pull a tag named
        "undefined" and fails. So: ask for a fresh watch, read the verdict, and
        only then apply.

        The second step is the one that cannot be waited on; see ``_wud_fire``.
        """
        if not (self._wud_url and self._wud_user and self._wud_password):
            return None
        # Per-request rather than on the client, so an injected client (tests)
        # authenticates the same way the real one does.
        auth = httpx.BasicAuth(self._wud_user, self._wud_password)
        client = self._client or httpx.AsyncClient()
        try:
            watched = await self._wud_watch(client, auth)
            if not isinstance(watched, list):
                # Either nobody answered (None, and the caller falls back to the
                # host-side message) or WUD said something worth reporting as is.
                return watched
            container = self._find_own_container(watched)
            if container is None:
                log.warning("update.wud.not_watched", image=self._wud_image)
                return await self._message_result(_NOT_WATCHED_MESSAGE)
            if not container.update_available:
                # Worth saying plainly rather than firing a trigger that would
                # have nothing to do: this is the common case.
                log.info("update.wud.up_to_date")
                return await self._message_result(_UP_TO_DATE_MESSAGE, ok=True)
            return await self._wud_fire(client, auth, container)
        finally:
            if self._client is None:
                await client.aclose()

    @staticmethod
    def _unexpected(status: int) -> str:
        """One-line report for a WUD answer this app can't read as a result."""
        return (
            f"WUD answered the update trigger with HTTP {status}, which this app can't read "
            "as a result. `docker compose logs wud` has the detail; update from the host "
            "meanwhile: deploy/update.sh"
        )

    async def update(self) -> UpdateResult:
        """Update this deployment, by whichever route it has.

        A source deployment runs ``deploy/update-source.sh`` and reports the
        before/after commits. A Docker one has no such route of its own and
        asks WUD instead, falling back to the host-side message when that
        isn't configured or isn't there.
        """
        if self._in_container:
            triggered = await self._trigger_wud()
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
            # No WUD equivalent here on purpose: its docker trigger only ever
            # moves an image forward to whatever its watcher detected, and with
            # PRUNE on, the image being rolled back *to* is already gone.
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
