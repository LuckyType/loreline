"""Per-version log files: one append-only file per transcript version.

Deliberately the same shape as :class:`loreline.persistence.AudioStore` (and
:class:`loreline.video.VideoStore`): a root directory, paths resolved from ids,
nothing in the database. A session's logs live under ``<root>/<session_id>/``
with one file per transcript version - ``original.log`` for the live capture,
which has no job id, and ``<job_id>.log`` for each re-processing run.

The dashboard's ring buffer holds a few hundred lines and then forgets them,
which is exactly the wrong behaviour for the one run that can never be
repeated: the live capture. These files are what "why does this version look
like that" is answered from, hours or weeks later.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Collection

_SUFFIX = ".log"


class LogStore:
    """Resolve per-version log paths and append/read their lines."""

    def __init__(self, root: Path) -> None:
        self._root = root

    def session_dir(self, session_id: str) -> Path:
        return self._root / _segment(session_id)

    def path(self, session_id: str, version: str) -> Path:
        """Path of one version's log file.

        Both ids are validated rather than trusted: ``version`` arrives from a
        query string (see the session logs route), and a value of ``../../..``
        would otherwise resolve to any file on the host.
        """
        return self.session_dir(session_id) / f"{_segment(version)}{_SUFFIX}"

    def exists(self, session_id: str, version: str) -> bool:
        try:
            return self.path(session_id, version).is_file()
        except ValueError:
            return False

    def append(self, session_id: str, version: str, line: str) -> None:
        """Append one rendered line, creating the session dir on first use.

        Opens and closes per line instead of holding a handle: this app logs a
        few lines a second, so the syscalls are free, while a cached handle
        would keep writing into a file that deleting a version already
        unlinked. Failures are swallowed because this runs inside the logging
        processor chain - a full disk must not turn every ``log.info`` in the
        process into an exception.
        """
        try:
            target = self.path(session_id, version)
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except (OSError, ValueError):  # see docstring
            return

    def read(self, session_id: str, version: str) -> str:
        """Return one version's stored log text (raises if there is none)."""
        return self.path(session_id, version).read_text(encoding="utf-8")

    def delete_version(self, session_id: str, version: str) -> None:
        """Remove one version's log file (no-op if absent)."""
        try:
            self.path(session_id, version).unlink(missing_ok=True)
        except (OSError, ValueError):
            return

    def delete_session(self, session_id: str) -> None:
        """Remove a session's whole log directory (no-op if absent)."""
        try:
            shutil.rmtree(self.session_dir(session_id), ignore_errors=True)
        except ValueError:
            return

    def prune(self, keep: Collection[str]) -> int:
        """Drop log directories whose session is gone; return how many went.

        Unlike the ring buffer these files have no natural bound, and unlike
        audio they are small enough that nobody notices them accumulating over
        a campaign. Deleting a session takes its logs (same call site as its
        WAV), so this sweep only ever finds what an interrupted delete or an
        older build left behind - which is precisely the kind of thing that is
        never cleaned up at all without a sweep.
        """
        if not self._root.is_dir():
            return 0
        removed = 0
        for entry in self._root.iterdir():
            if entry.is_dir() and entry.name not in keep:
                shutil.rmtree(entry, ignore_errors=True)
                removed += 1
        return removed


def _segment(value: str) -> str:
    """Return ``value`` if it is a single, plain path component."""
    if not value or value in {".", ".."} or {"/", "\\", "\x00"} & set(value):
        msg = f"unsafe log path segment {value!r}"
        raise ValueError(msg)
    return value
