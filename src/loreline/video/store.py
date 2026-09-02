"""Where generated videos live on disk.

Deliberately the same shape as :class:`loreline.persistence.AudioStore`: a
root directory plus one file per job, resolved by id. Videos sit under
``data_dir/video`` so a session's generated media is backed up and pruned by
whatever already covers ``data_dir``.
"""

from __future__ import annotations

from pathlib import Path

# OpenRouter's video models return MP4; the extension is fixed rather than
# sniffed from a Content-Type so the path is derivable from the job id alone
# (the store never sees the response headers).
_EXTENSION = ".mp4"


class VideoStore:
    """Resolve per-job video paths and read/write their bytes."""

    def __init__(self, root: Path) -> None:
        self._root = root

    def path(self, job_id: str) -> Path:
        return self._root / f"{job_id}{_EXTENSION}"

    def exists(self, job_id: str) -> bool:
        return self.path(job_id).exists()

    def write(self, job_id: str, data: bytes) -> Path:
        """Persist a finished video, creating the store directory on first use."""
        self._root.mkdir(parents=True, exist_ok=True)
        target = self.path(job_id)
        target.write_bytes(data)
        return target

    def delete(self, job_id: str) -> None:
        """Remove a job's video file (no-op if absent)."""
        self.path(job_id).unlink(missing_ok=True)
