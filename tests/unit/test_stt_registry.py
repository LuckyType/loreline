"""Unit tests for the STT registry."""

from __future__ import annotations

from pathlib import Path

import pytest

from loreline.capabilities import default_model
from loreline.models import Interaction, Protocol, ProviderConfig, ProviderKind
from loreline.secrets import SecretStore
from loreline.stt import create_backend, registry
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
        create_backend(_config(ProviderKind.OPENAI_COMPAT), secrets)


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

    def test_openai_without_a_model_falls_back_to_the_declared_default(
        self, tmp_path: Path
    ) -> None:
        """Only the health probe gets here without a model, and it resolves to
        capabilities.yaml's OpenAI transcription default (gpt-transcribe),
        which is a batch model - so the probe exercises the key against
        /models instead of opening a Realtime session."""
        backend = create_backend(
            _config(ProviderKind.OPENAI, base_url=None), self._secrets(tmp_path)
        )
        assert isinstance(backend, OpenAICompatBackend)

    def test_openai_batch_ignores_the_realtime_base_url(self, tmp_path: Path) -> None:
        """For the OPENAI kind, base_url has always meant the Realtime WebSocket
        endpoint - handing a wss URL to the HTTP batch connector would fail
        every request."""
        backend = create_backend(
            _config(
                ProviderKind.OPENAI,
                base_url="wss://api.openai.com/v1/realtime?intent=transcription",
            ),
            self._secrets(tmp_path),
            "whisper-1",
        )
        assert isinstance(backend, OpenAICompatBackend)
        assert backend.config.base_url is None

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

    def test_gemini_without_a_model_keeps_the_batch_connector(self, tmp_path: Path) -> None:
        """Gemini's declared default is the batch model, and resolving it is
        what holds a config nobody chose a model for on the batch connector.
        This matters more now that the Live variant is offered: it serves no
        diarization and no word timestamps, so a silent reroute would drop
        both without erroring."""
        backend = create_backend(
            _config(ProviderKind.GEMINI, base_url=None), self._secrets(tmp_path)
        )
        assert isinstance(backend, GeminiSTTBackend)

    def test_the_resolved_default_reaches_the_connector(self, tmp_path: Path) -> None:
        """Not just the transport: the model the registry resolved is the one
        the connector sends, so the probe and the routing cannot disagree about
        which model is running."""
        backend = create_backend(
            _config(ProviderKind.GEMINI, base_url=None), self._secrets(tmp_path)
        )
        assert isinstance(backend, GeminiSTTBackend)
        body = backend._request_body(b"", [])  # pyright: ignore[reportPrivateUsage]
        assert body["model"] == default_model(ProviderKind.GEMINI, Interaction.TRANSCRIBE)

    def test_an_empty_catalogue_for_a_curated_kind_is_a_clear_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A curated kind that resolves nothing means the file is out of models.

        Plausible for OpenAI, whose whole gpt-4o-transcribe family shares one
        removal date: once the last entry is deleted there is nothing to fall
        back to. Left alone it would post a modelless request and surface as
        the vendor complaining about a missing field, which sends whoever reads
        it to the wrong file. Distinct from the self-hosted kind below, which
        curates nothing on purpose and works fine with no model named.
        """

        def no_default(_kind: ProviderKind, _interaction: Interaction) -> str | None:
            return None

        monkeypatch.setattr(registry, "default_model", no_default)
        with pytest.raises(ValueError, match="offers no transcription model"):
            create_backend(_config(ProviderKind.OPENAI, base_url=None), self._secrets(tmp_path))

    def test_a_kind_with_no_curated_catalogue_resolves_no_model(self, tmp_path: Path) -> None:
        """The self-hosted kind lists nothing this repo can vouch for, so the
        connector names no model and the server uses its own. Guessing one
        (this connector used to pin whisper-1) fails a request against a server
        that simply has a different model loaded."""
        backend = create_backend(_config(ProviderKind.OPENAI_COMPAT), self._secrets(tmp_path))
        assert isinstance(backend, OpenAICompatBackend)
        assert backend._model is None  # pyright: ignore[reportPrivateUsage]
