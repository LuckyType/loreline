"""Unit tests for the STT registry."""

from __future__ import annotations

from pathlib import Path

import pytest

from loreline.models import Protocol, ProviderConfig, ProviderKind
from loreline.secrets import SecretStore
from loreline.stt import create_backend
from loreline.stt.backends.gemini import GeminiSTTBackend
from loreline.stt.backends.gemini_live import GeminiLiveBackend
from loreline.stt.backends.openai_compat import OpenAICompatBackend
from loreline.stt.backends.openai_realtime import OpenAIRealtimeBackend
from loreline.stt.registry import registered_kinds


def _config(
    kind: ProviderKind, model: str | None = None, base_url: str | None = "http://localhost:9999/v1"
) -> ProviderConfig:
    return ProviderConfig(
        id="p1",
        name="test",
        kind=kind,
        base_url=base_url,
        protocol=Protocol.HTTP_BATCH,
        model=model,
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


class TestModelResolution:
    """One vendor, two transports: the stored model decides the connector.

    This is the regression the kind-keyed registry could not express - OpenAI
    serves realtime-only models (gpt-live-transcribe) and batch-only ones
    (whisper-1, gpt-transcribe) behind a single kind, so selection has to key
    on (kind, model).
    """

    def _secrets(self, tmp_path: Path) -> SecretStore:
        return SecretStore(tmp_path / "secrets.json")

    def test_openai_realtime_model_gets_the_realtime_connector(self, tmp_path: Path) -> None:
        backend = create_backend(
            _config(ProviderKind.OPENAI, model="gpt-realtime-whisper", base_url=None),
            self._secrets(tmp_path),
        )
        assert isinstance(backend, OpenAIRealtimeBackend)

    def test_openai_batch_model_gets_the_batch_connector(self, tmp_path: Path) -> None:
        backend = create_backend(
            _config(ProviderKind.OPENAI, model="gpt-transcribe", base_url=None),
            self._secrets(tmp_path),
        )
        assert isinstance(backend, OpenAICompatBackend)

    def test_openai_without_a_model_keeps_its_historical_connector(self, tmp_path: Path) -> None:
        """Stored configs predate per-model resolution, and for this kind they
        have always meant the Realtime session."""
        backend = create_backend(
            _config(ProviderKind.OPENAI, base_url=None), self._secrets(tmp_path)
        )
        assert isinstance(backend, OpenAIRealtimeBackend)

    def test_openai_batch_ignores_the_realtime_base_url(self, tmp_path: Path) -> None:
        """For the OPENAI kind, base_url has always meant the Realtime WebSocket
        endpoint - handing a wss URL to the HTTP batch connector would fail
        every request."""
        backend = create_backend(
            _config(
                ProviderKind.OPENAI,
                model="whisper-1",
                base_url="wss://api.openai.com/v1/realtime?intent=transcription",
            ),
            self._secrets(tmp_path),
        )
        assert isinstance(backend, OpenAICompatBackend)
        assert backend.config.base_url is None

    def test_gemini_batch_model_resolves(self, tmp_path: Path) -> None:
        backend = create_backend(
            _config(ProviderKind.GEMINI, model="gemini-3.5-transcribe", base_url=None),
            self._secrets(tmp_path),
        )
        assert isinstance(backend, GeminiSTTBackend)

    def test_gemini_live_model_resolves_to_the_live_connector(self, tmp_path: Path) -> None:
        """gemini-3.5-transcribe-live rides the Live API's WebSocket. The model
        is hidden from the pickers until verified against the real service (see
        the gate in loreline.stt.catalog), but a config that names it
        explicitly must still reach the connector - that is how the
        verification run is switched on without a code change."""
        backend = create_backend(
            _config(ProviderKind.GEMINI, model="gemini-3.5-transcribe-live", base_url=None),
            self._secrets(tmp_path),
        )
        assert isinstance(backend, GeminiLiveBackend)

    def test_gemini_without_a_model_keeps_the_batch_connector(self, tmp_path: Path) -> None:
        """Stored Gemini configs predate the Live connector, and for this kind
        an unset model has always meant the batch Interactions API."""
        backend = create_backend(
            _config(ProviderKind.GEMINI, base_url=None), self._secrets(tmp_path)
        )
        assert isinstance(backend, GeminiSTTBackend)
