"""Unit tests for the STT registry."""

from __future__ import annotations

from pathlib import Path

import pytest

from loreline.models import Protocol, ProviderConfig, ProviderKind
from loreline.secrets import SecretStore
from loreline.stt import create_backend
from loreline.stt.registry import registered_kinds


def _config(kind: ProviderKind) -> ProviderConfig:
    return ProviderConfig(
        id="p1",
        name="test",
        kind=kind,
        base_url="http://localhost:9999/v1",
        protocol=Protocol.HTTP_BATCH,
    )


def test_openai_compat_kind_registered() -> None:
    kinds = registered_kinds()
    assert ProviderKind.OPENAI_COMPAT in kinds
    assert ProviderKind.OPENAI in kinds


def test_create_backend_returns_connector(tmp_path: Path) -> None:
    secrets = SecretStore(tmp_path / "secrets.json")
    backend = create_backend(_config(ProviderKind.OPENAI_COMPAT), secrets)
    assert backend.config.id == "p1"


def test_create_backend_unknown_kind_raises(tmp_path: Path) -> None:
    secrets = SecretStore(tmp_path / "secrets.json")
    config = _config(ProviderKind.VOSK)  # no backend registered yet
    with pytest.raises(ValueError, match="no STT backend registered"):
        create_backend(config, secrets)
