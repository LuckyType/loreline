"""Async subprocess runner shared by the updater and autostart toggles.

``CommandRunner`` is a small Protocol so the web layer can inject a fake runner
in tests (no real ``git`` / ``systemctl`` calls), while production uses
``run_command`` backed by ``asyncio.create_subprocess_exec``.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Protocol

_DEFAULT_TIMEOUT = 600.0


@dataclass(slots=True)
class CommandResult:
    """Outcome of a subprocess invocation."""

    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    @property
    def combined(self) -> str:
        return (self.stdout + self.stderr).strip()


class CommandRunner(Protocol):
    """Callable that runs an argv and returns its result."""

    async def __call__(self, argv: list[str], *, cwd: str | None = None) -> CommandResult: ...


async def run_command(argv: list[str], *, cwd: str | None = None) -> CommandResult:
    """Run ``argv`` to completion, capturing stdout/stderr (never raises)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:  # missing binary (e.g. systemctl absent on dev machines)
        return CommandResult(returncode=127, stdout="", stderr=str(exc))
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=_DEFAULT_TIMEOUT)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return CommandResult(returncode=124, stdout="", stderr="command timed out")
    return CommandResult(
        returncode=proc.returncode if proc.returncode is not None else -1,
        stdout=out.decode("utf-8", errors="replace"),
        stderr=err.decode("utf-8", errors="replace"),
    )
