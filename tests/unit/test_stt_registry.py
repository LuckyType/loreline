"""Unit tests for the STT registry."""

from __future__ import annotations

from pathlib import Path

import pytest

from loreline.models import ProviderConfig, ProviderKind
from loreline.secrets import SecretStore
from loreline.stt import create_backend, registry
from loreline.stt.backends.assemblyai import AssemblyAIBackend
from loreline.stt.backends.assemblyai_batch import AssemblyAIBatchBackend
from loreline.stt.backends.deepgram import DeepgramBackend
from loreline.stt.backends.deepgram_batch import DeepgramBatchBackend
from loreline.stt.backends.gemini import GeminiSTTBackend
from loreline.stt.backends.gemini_live import GeminiLiveBackend
from loreline.stt.backends.openai_compat import OpenAICompatBackend
from loreline.stt.backends.openai_realtime import OpenAIRealtimeBackend
from loreline.stt.registry import registered_kinds


def _config(
    kind: ProviderKind, base_url: str | None = "http://localhost:9999/v1"
) -> ProviderConfig:
    return ProviderConfig(
        id="p1",
        name="test",
        kind=kind,
        base_url=base_url,
    )


def test_openai_compat_kind_registered() -> None:
    kinds = registered_kinds()
    assert ProviderKind.OPENAI_COMPAT in kinds
    assert ProviderKind.OPENAI in kinds


def test_create_backend_returns_connector(tmp_path: Path) -> None:
    secrets = SecretStore(tmp_path / "secrets.json")
    backend = create_backend(_config(ProviderKind.OPENAI_COMPAT), secrets, "whisper-1")
    assert backend.config.id == "p1"


def test_create_backend_unknown_kind_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every shipped kind has a connector now, so the guard is provoked by
    emptying the registry. It still has to hold: a stored config whose kind
    lost its backend must fail with a message naming the kind, which is what
    the vosk kind did for real until it was removed in migration v15.
    """
    secrets = SecretStore(tmp_path / "secrets.json")
    # Also stub the loader: the backend modules may not be imported yet, and
    # importing them would re-run their @register decorators into the empty
    # dict this test just installed.
    monkeypatch.setattr(registry, "_REGISTRY", {})
    monkeypatch.setattr(registry, "_load_backends", lambda: None)
    with pytest.raises(ValueError, match="no STT backend registered"):
        create_backend(_config(ProviderKind.OPENAI_COMPAT), secrets, "whisper-1")


class TestModelResolution:
    """One vendor, two transports: the chosen model decides the connector.

    This is the regression the kind-keyed registry could not express - OpenAI
    serves realtime-only models (gpt-live-transcribe) and batch-only ones
    (whisper-1, gpt-transcribe) behind a single kind, so selection has to key
    on (kind, model). The model arrives as an argument rather than on the
    config, which is what keeps the connector and this lookup in agreement.
    """

    def _secrets(self, tmp_path: Path) -> SecretStore:
        return SecretStore(tmp_path / "secrets.json")

    def test_openai_realtime_model_gets_the_realtime_connector(self, tmp_path: Path) -> None:
        backend = create_backend(
            _config(ProviderKind.OPENAI, base_url=None),
            self._secrets(tmp_path),
            "gpt-realtime-whisper",
        )
        assert isinstance(backend, OpenAIRealtimeBackend)

    def test_openai_batch_model_gets_the_batch_connector(self, tmp_path: Path) -> None:
        backend = create_backend(
            _config(ProviderKind.OPENAI, base_url=None), self._secrets(tmp_path), "gpt-transcribe"
        )
        assert isinstance(backend, OpenAICompatBackend)

    def test_openai_batch_ignores_the_realtime_base_url(self, tmp_path: Path) -> None:
        """For the OPENAI kind, base_url has always meant the Realtime WebSocket
        endpoint - handing a wss URL to the HTTP batch connector would fail
        every request. The batch surface is declared non-overridable, so the
        client is built on OpenAI's own base whatever the row says."""
        backend = create_backend(
            _config(
                ProviderKind.OPENAI,
                base_url="wss://api.openai.com/v1/realtime?intent=transcription",
            ),
            self._secrets(tmp_path),
            "whisper-1",
        )
        assert isinstance(backend, OpenAICompatBackend)
        client = backend._client  # pyright: ignore[reportPrivateUsage]
        assert str(client.base_url).rstrip("/") == "https://api.openai.com/v1"

    def test_gemini_batch_model_resolves(self, tmp_path: Path) -> None:
        backend = create_backend(
            _config(ProviderKind.GEMINI, base_url=None),
            self._secrets(tmp_path),
            "gemini-3.5-transcribe",
        )
        assert isinstance(backend, GeminiSTTBackend)

    def test_gemini_live_model_resolves_to_the_live_connector(self, tmp_path: Path) -> None:
        """gemini-3.5-transcribe-live rides the Live API's WebSocket. It spent
        its unverified life hidden from the pickers while still resolving to
        the connector for a config that named it explicitly, which is how the
        verification run that unhid it was switched on."""
        backend = create_backend(
            _config(ProviderKind.GEMINI, base_url=None),
            self._secrets(tmp_path),
            "gemini-3.5-transcribe-live",
        )
        assert isinstance(backend, GeminiLiveBackend)

    def test_a_kind_with_no_curated_catalogue_runs_with_no_model(self, tmp_path: Path) -> None:
        """The self-hosted kind lists nothing this repo can vouch for, so None
        is a legitimate model there: the connector names none and the server
        uses its own. Guessing one (this connector used to pin whisper-1)
        fails a request against a server that simply has a different model
        loaded. Nothing is resolved on the way: the registry takes what the
        caller chose, and every caller chooses."""
        backend = create_backend(_config(ProviderKind.OPENAI_COMPAT), self._secrets(tmp_path), None)
        assert isinstance(backend, OpenAICompatBackend)
        assert backend._model is None  # pyright: ignore[reportPrivateUsage]

    def test_deepgram_batch_only_model_gets_the_batch_connector(self, tmp_path: Path) -> None:
        """Deepgram's hosted Whisper is pre-recorded only, so it must not reach
        the WebSocket connector, which cannot serve it. The model is hidden from
        the pickers until verified against the real API (see the gate in
        loreline.stt.catalog), but a config naming it explicitly still routes,
        which is how that verification run is switched on."""
        backend = create_backend(
            _config(ProviderKind.DEEPGRAM, base_url=None),
            self._secrets(tmp_path),
            "whisper-large",
        )
        assert isinstance(backend, DeepgramBatchBackend)

    def test_deepgram_dual_transport_model_keeps_the_streaming_connector(
        self, tmp_path: Path
    ) -> None:
        """Nova serves both transports and has always streamed here. Adding a
        batch connector for Whisper must not quietly move it."""
        backend = create_backend(
            _config(ProviderKind.DEEPGRAM, base_url=None),
            self._secrets(tmp_path),
            "nova-3",
        )
        assert isinstance(backend, DeepgramBackend)

    def test_assemblyai_batch_only_model_gets_the_batch_connector(self, tmp_path: Path) -> None:
        """universal-2 is async only; the streaming endpoint does not accept it
        as a speech_model at all."""
        backend = create_backend(
            _config(ProviderKind.ASSEMBLYAI, base_url=None),
            self._secrets(tmp_path),
            "universal-2",
        )
        assert isinstance(backend, AssemblyAIBatchBackend)

    def test_assemblyai_streaming_model_keeps_the_streaming_connector(self, tmp_path: Path) -> None:
        backend = create_backend(
            _config(ProviderKind.ASSEMBLYAI, base_url=None),
            self._secrets(tmp_path),
            "universal-3-5-pro",
        )
        assert isinstance(backend, AssemblyAIBackend)
