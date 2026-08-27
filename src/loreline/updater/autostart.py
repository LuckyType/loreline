"""Systemd autostart toggle for the loreline unit.

Wraps ``systemctl is-enabled/enable/disable loreline``. The privileged
enable/disable calls go through ``sudo`` and rely on the narrow rule installed
by ``deploy/sudoers.d/loreline``. The runner is injectable for offline tests.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from loreline.updater.process import run_command

if TYPE_CHECKING:
    from loreline.updater.process import CommandRunner


class Autostart:
    """Query and toggle whether the systemd unit starts at boot."""

    def __init__(self, *, unit: str = "loreline", runner: CommandRunner | None = None) -> None:
        self._unit = unit
        self._run: CommandRunner = runner or run_command

    async def is_enabled(self) -> bool:
        """Return True if the unit is enabled (starts at boot)."""
        result = await self._run(["systemctl", "is-enabled", self._unit])
        return result.stdout.strip() == "enabled"

    async def set_enabled(self, enabled: bool) -> bool:
        """Enable or disable the unit; return the resulting enabled state."""
        action = "enable" if enabled else "disable"
        await self._run(["sudo", "systemctl", action, self._unit])
        return await self.is_enabled()
