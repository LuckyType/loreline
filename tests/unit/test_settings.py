"""Tests for settings."""

from __future__ import annotations

from pathlib import Path

from pytest import MonkeyPatch

from loreline.settings import Settings


def test_derived_paths(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path)
    assert settings.db_path == tmp_path / "loreline.db"
    assert settings.audio_dir == tmp_path / "audio"
    assert settings.secrets_path == tmp_path / "secrets.json"


def test_env_prefix(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("LORELINE_PORT", "9999")
    assert Settings().port == 9999
