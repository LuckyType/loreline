"""App-managed secret store for UI-entered provider API keys.

Secrets are persisted to a JSON file (``data_dir/secrets.json``) with ``0600``
permissions and a dedicated owner. Environment variables take precedence: a
lookup checks ``os.environ`` (``LORELINE_SECRET_<NAME>``) before the file.

This is the "tier 1" approach from the build spec - pragmatic for a single-user
LAN device. Keys are never returned by the API (write-only; UI shows masked).
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import cast

_ENV_PREFIX = "LORELINE_SECRET_"


class SecretStore:
    """File-backed key/value secret store with env-var override."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def _load(self) -> dict[str, str]:
        if not self._path.exists():
            return {}
        raw: object = json.loads(self._path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return {}
        mapping = cast("dict[object, object]", raw)
        return {str(k): str(v) for k, v in mapping.items()}

    def _write(self, data: dict[str, str]) -> None:
        """Atomically write the secret file with ``0600`` from creation.

        The file is created via ``os.open`` with mode ``0600`` (no world-readable
        window), fsynced, then ``os.replace``d into place so a crash mid-write
        cannot truncate the live file.
        """
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(data, indent=2, sort_keys=True)
        tmp = self._path.with_name(f"{self._path.name}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self._path)
        finally:
            tmp.unlink(missing_ok=True)
        self._path.chmod(stat.S_IRUSR | stat.S_IWUSR)  # enforce 0600 regardless of umask

    def get(self, name: str) -> str | None:
        """Return a secret, preferring the environment override."""
        env = os.environ.get(_ENV_PREFIX + name.upper())
        if env is not None:
            return env
        return self._load().get(name)

    def set(self, name: str, value: str) -> None:
        """Persist a secret to the managed file."""
        data = self._load()
        data[name] = value
        self._write(data)

    def delete(self, name: str) -> None:
        """Remove a secret from the managed file (env override unaffected)."""
        data = self._load()
        data.pop(name, None)
        self._write(data)

    def names(self) -> list[str]:
        """Return the stored secret names (file only), sorted."""
        return sorted(self._load())

    def hint(self, name: str) -> str | None:
        """Return a masked preview of a secret (first/last chars), or None.

        Reveals only the leading and trailing characters so the UI can identify
        *which* key is stored without exposing it. Short values are masked more
        aggressively. Honors the environment override like ``get``.
        """
        value = self.get(name)
        if not value:
            return None
        n = len(value)
        if n <= 4:  # noqa: PLR2004 - too short to reveal safely
            return "•" * n
        if n <= 8:  # noqa: PLR2004
            return f"{value[:2]}…{value[-2:]}"
        return f"{value[:4]}…{value[-4:]}"
