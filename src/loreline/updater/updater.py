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
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel

from loreline.logging import get_logger
from loreline.updater.process import run_command

if TYPE_CHECKING:
    from loreline.updater.process import CommandRunner

log = get_logger(__name__)

_MAX_OUTPUT = 8000
_DOCKER_MARKER = Path("/.dockerenv")
_CONTAINER_MESSAGE = (
    "Running in a Docker deployment - self-update from the web UI isn't "
    "available here (there's no systemd unit inside the container to "
    "restart, and granting that access would mean handing the container "
    "the Docker socket, i.e. effectively root on the host). Update from the "
    "host instead: deploy/update.sh - or enable automatic updates with "
    "`sudo systemctl enable --now loreline-update.timer`."
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
    ) -> None:
        self._app_dir = app_dir
        self._unit = unit
        self._run: CommandRunner = runner or run_command
        self._script = app_dir / "deploy" / "update-source.sh"
        self._in_container = in_container if in_container is not None else _DOCKER_MARKER.exists()

    async def current_revision(self) -> str | None:
        """Return the current git HEAD commit, or None if unavailable."""
        result = await self._run(["git", "rev-parse", "HEAD"], cwd=str(self._app_dir))
        return result.stdout.strip() if result.ok else None

    async def _container_result(self) -> UpdateResult:
        revision = await self.current_revision()
        return UpdateResult(
            ok=False, previous_commit=revision, new_commit=revision, output=_CONTAINER_MESSAGE
        )

    async def update(self) -> UpdateResult:
        """Run ``deploy/update-source.sh`` and report the before/after commits."""
        if self._in_container:
            return await self._container_result()
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
            return await self._container_result()
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
