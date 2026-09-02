"""Registry mapping provider kind + transport to backend factories.

Backend modules register themselves via the ``@register`` decorator. The web
layer calls ``create_backend`` with a stored ``ProviderConfig`` plus the secret
store to instantiate a connector for a session.

Selection is keyed on ``(kind, transport)``, not kind alone, because one vendor
can serve models that need different connectors: OpenAI's gpt-live-transcribe
runs only over the Realtime WebSocket while whisper-1 and gpt-transcribe run
only over ``/audio/transcriptions``. The stored config's model decides which
transport applies (see :func:`loreline.capabilities.is_realtime_model`).
"""

from __future__ import annotations

from collections.abc import Callable

from loreline.capabilities import is_realtime_model
from loreline.models import ProviderConfig, ProviderKind
from loreline.secrets import SecretStore
from loreline.stt.base import STTBackend

BackendFactory = Callable[[ProviderConfig, SecretStore], STTBackend]

_REGISTRY: dict[tuple[ProviderKind, bool], BackendFactory] = {}


def register(
    kind: ProviderKind, *, realtime: bool = False
) -> Callable[[BackendFactory], BackendFactory]:
    """Decorator registering a backend factory for a provider kind + transport.

    ``realtime=True`` marks a streaming connector (interim results within an
    utterance); the default covers batch connectors, which post one complete
    utterance per request. A kind may register one of each.
    """

    def decorator(factory: BackendFactory) -> BackendFactory:
        key = (kind, realtime)
        if key in _REGISTRY:
            msg = f"backend already registered for {kind} (realtime={realtime})"
            raise ValueError(msg)
        _REGISTRY[key] = factory
        return factory

    return decorator


def create_backend(config: ProviderConfig, secrets: SecretStore) -> STTBackend:
    """Instantiate a backend for ``config``, resolved by kind *and* model."""
    _load_backends()
    realtime = is_realtime_model(config.kind, config.model)
    factory = _REGISTRY.get((config.kind, realtime))
    if factory is not None:
        return factory(config, secrets)
    if (config.kind, not realtime) in _REGISTRY:
        # The kind exists but this model needs the transport we lack: say so,
        # rather than letting the wrong connector produce a provider-side
        # error that never mentions the transport. Today this is Gemini's
        # -live model, which only the Live API's WebSocket can reach.
        transport = "streaming" if realtime else "batch"
        msg = (
            f"model {config.model!r} needs a {transport} connector, which this app "
            f"does not have for kind {config.kind.value!r}"
        )
        raise ValueError(msg)
    msg = f"no STT backend registered for kind {config.kind.value!r}"
    raise ValueError(msg)


def registered_kinds() -> list[ProviderKind]:
    """Return the provider kinds with at least one registered backend."""
    _load_backends()
    return sorted({kind for kind, _ in _REGISTRY})


def _load_backends() -> None:
    """Import backend modules so their ``@register`` decorators run."""
    from loreline.stt import backends  # noqa: PLC0415

    backends.load()
