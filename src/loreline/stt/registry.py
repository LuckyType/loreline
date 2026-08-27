"""Registry mapping ``ProviderKind`` to backend factories.

Backend modules register themselves via the ``@register`` decorator. The web
layer calls ``create_backend`` with a stored ``ProviderConfig`` plus the secret
store to instantiate a connector for a session.
"""

from __future__ import annotations

from collections.abc import Callable

from loreline.models import ProviderConfig, ProviderKind
from loreline.secrets import SecretStore
from loreline.stt.base import STTBackend

BackendFactory = Callable[[ProviderConfig, SecretStore], STTBackend]

_REGISTRY: dict[ProviderKind, BackendFactory] = {}


def register(kind: ProviderKind) -> Callable[[BackendFactory], BackendFactory]:
    """Decorator registering a backend factory for a provider kind."""

    def decorator(factory: BackendFactory) -> BackendFactory:
        if kind in _REGISTRY:
            msg = f"backend already registered for {kind}"
            raise ValueError(msg)
        _REGISTRY[kind] = factory
        return factory

    return decorator


def create_backend(config: ProviderConfig, secrets: SecretStore) -> STTBackend:
    """Instantiate a backend for ``config`` using the registered factory."""
    _load_backends()
    try:
        factory = _REGISTRY[config.kind]
    except KeyError as exc:
        msg = f"no STT backend registered for kind {config.kind.value!r}"
        raise ValueError(msg) from exc
    return factory(config, secrets)


def registered_kinds() -> list[ProviderKind]:
    """Return the provider kinds with a registered backend."""
    _load_backends()
    return sorted(_REGISTRY)


def _load_backends() -> None:
    """Import backend modules so their ``@register`` decorators run."""
    from loreline.stt import backends  # noqa: PLC0415

    backends.load()
