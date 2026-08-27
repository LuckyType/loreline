"""Self-update + autostart control for the source deployment."""

from __future__ import annotations

from loreline.updater.autostart import Autostart
from loreline.updater.process import CommandResult, CommandRunner, run_command
from loreline.updater.updater import Updater, UpdateResult

__all__ = [
    "Autostart",
    "CommandResult",
    "CommandRunner",
    "UpdateResult",
    "Updater",
    "run_command",
]
