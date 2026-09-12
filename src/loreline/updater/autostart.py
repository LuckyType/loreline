"""Systemd autostart toggle for the loreline unit.

Wraps ``systemctl is-enabled/enable/disable loreline``. The privileged
enable/disable calls go through ``sudo`` and rely on the narrow rule installed
by ``deploy/sudoers.d/loreline``. The runner is injectable for offline tests.

Like ``Updater``, none of this applies to a Docker deployment: there is no
systemd unit inside the container to enable, so every call would otherwise
fail in a way indistinguishable from "installed but disabled". Detected via
the same ``/.dockerenv`` marker, so a caller can tell "not available here"
apart from "available and currently off".
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from loreline.updater.process import run_command

if TYPE_CHECKING:
    from loreline.updater.process import CommandRunner

_DOCKER_MARKER = Path("/.dockerenv")


class AutostartUnavailableError(RuntimeError):
    """Systemd autostart isn't available on this platform (e.g. inside Docker)."""


class AutostartToggleError(RuntimeError):
    """The unit exists, but the enable/disable call itself failed."""


class Autostart:
    """Query and toggle whether the systemd unit starts at boot."""

    def __init__(
        self,
        *,
        unit: str = "loreline",
        runner: CommandRunner | None = None,
        in_container: bool | None = None,
    ) -> None:
        self._unit = unit
        self._run: CommandRunner = runner or run_command
        self._in_container = in_container if in_container is not None else _DOCKER_MARKER.exists()

    def _ensure_available(self) -> None:
        if self._in_container:
            # The second half is what makes this an answer rather than a
            # refusal. Settings > Client shows this sentence where the toggle
            # would be, and "not available" on its own leaves a reader thinking
            # a Docker deployment cannot start at boot at all, when in fact it
            # already does and the setting simply lives on the container.
            msg = (
                f"systemd unit {self._unit!r} is not available in a Docker deployment. "
                "A container starts at boot through its restart policy instead: "
                "`--restart unless-stopped` on the docker run command line, or "
                "`restart: unless-stopped` in docker-compose.yml, which the bundled "
                "stack already sets."
            )
            raise AutostartUnavailableError(msg)

    async def is_enabled(self) -> bool:
        """Return True if the unit is enabled (starts at boot).

        Raises AutostartUnavailableError in a Docker deployment, where there
        is no systemd unit inside the container to query - a different fact
        than the unit existing and simply being disabled.
        """
        self._ensure_available()
        result = await self._run(["systemctl", "is-enabled", self._unit])
        return result.stdout.strip() == "enabled"

    async def set_enabled(self, enabled: bool) -> bool:
        """Enable or disable the unit; return the resulting enabled state.

        Raises AutostartUnavailableError in a Docker deployment. Raises
        AutostartToggleError if the unit exists but the enable/disable call
        itself failed (e.g. the sudoers rule rejects it), rather than
        silently reporting whatever is_enabled() finds afterward.
        """
        self._ensure_available()
        action = "enable" if enabled else "disable"
        result = await self._run(["sudo", "systemctl", action, self._unit])
        if not result.ok:
            msg = f"failed to {action} systemd unit {self._unit!r}: {result.combined}"
            raise AutostartToggleError(msg)
        return await self.is_enabled()
