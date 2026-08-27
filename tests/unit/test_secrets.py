"""Tests for the secret store."""

from __future__ import annotations

import stat
from pathlib import Path

from pytest import MonkeyPatch

from loreline.secrets import SecretStore


def test_set_get_delete(tmp_path: Path) -> None:
    store = SecretStore(tmp_path / "secrets.json")
    assert store.get("deepgram") is None

    store.set("deepgram", "dg-key")
    assert store.get("deepgram") == "dg-key"
    assert store.names() == ["deepgram"]

    store.delete("deepgram")
    assert store.get("deepgram") is None


def test_env_override(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    store = SecretStore(tmp_path / "secrets.json")
    store.set("openai", "file-value")
    monkeypatch.setenv("LORELINE_SECRET_OPENAI", "env-value")
    assert store.get("openai") == "env-value"


def test_file_permissions(tmp_path: Path) -> None:
    path = tmp_path / "secrets.json"
    store = SecretStore(path)
    store.set("k", "v")
    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o600
