"""Health/monitoring helpers: disk usage and overall status rollup."""

from __future__ import annotations

import shutil
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


def disk_usage(path: Path) -> tuple[int, int]:
    """Return ``(free_bytes, total_bytes)`` for the filesystem holding ``path``.

    Walks up to the nearest existing ancestor so it works before the data dir is
    created.
    """
    target = path
    while not target.exists() and target != target.parent:
        target = target.parent
    usage = shutil.disk_usage(target)
    return usage.free, usage.total


def overall_status(*, capture_status: str, disk_free: int, disk_threshold_bytes: int) -> str:
    """Roll capture state + disk headroom into ``ok`` / ``degraded`` / ``error``."""
    if capture_status == "error":
        return "error"
    if disk_threshold_bytes > 0 and disk_free < disk_threshold_bytes:
        return "degraded"
    return "ok"
