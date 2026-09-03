"""Registry mapping provider kind + transport to backend factories.

Backend modules register themselves via the ``@register`` decorator. The web
layer calls ``create_backend`` with a stored ``ProviderConfig`` plus the secret
store to instantiate a connector for a session.

Selection is keyed on ``(kind, transport)``, not kind alone, because one vendor
can serve models that need different connectors: OpenAI's gpt-live-transcribe
runs only over the Realtime WebSocket while whisper-1 and gpt-transcribe run
only over ``/audio/transcriptions``. The model decides which transport applies
(see :func:`loreline.capabilities.is_realtime_model`).

The model is passed in rather than read off the config: a provider row serves
several interactions at once, so it cannot carry one model, and every action
route now requires the caller to choose. What is left is the health probe in
POST /providers/{id}/test, which has no caller to ask - it gets the model
capabilities.yaml marks as this kind's transcription default.
"""

from __future__ import annotations

from collections.abc import Callable

from loreline.capabilities import curates_a_catalogue, default_model, is_realtime_model
from loreline.models import Interaction, ProviderConfig, ProviderKind
from loreline.secrets import SecretStore
from loreline.stt.base import STTBackend

# The third argument is the resolved model, or None for a kind whose catalogue
# this repo does not curate (the self-hosted one) - see ``create_backend``. It
# is passed alongside the config rather than stamped onto a copy of it: a
# provider row has no model field to stamp, and the connector and the transport
# lookup must agree on which one is running.
#
# Defined here, once. The session manager and the reprocess manager take an
# injectable factory of this shape and import the alias from here rather than
# restating it, which they used to: three copies meant a change to what a
# factory receives could land in one of them and not the others.
BackendFactory = Callable[[ProviderConfig, SecretStore, str | None], STTBackend]

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


def create_backend(
    config: ProviderConfig, secrets: SecretStore, model: str | None = None
) -> STTBackend:
    """Instantiate a backend for ``config``, resolved by kind *and* model.

    ``model`` is what the session, the re-processing job or the picker chose.
    None means nobody chose - only the health probe - and falls back to the
    kind's declared transcription default. That resolution happens here, once,
    so the transport lookup and the connector always agree on which model is
    running; passing it through the config was how they could disagree.

    The resolved model can still be None, for a kind whose models are whatever
    the operator installed. Connectors handle that by naming no model and
    letting the endpoint apply its own default. A kind that *does* curate a
    catalogue and still resolves nothing is a different matter, and raises.
    """
    _load_backends()
    resolved = model or default_model(config.kind, Interaction.TRANSCRIBE)
    if resolved is None and curates_a_catalogue(config.kind):
        # The catalogue exists but has nothing left to offer for transcription
        # - every model retired and was removed, say. OpenAI is the plausible
        # case: whisper-1 and the whole gpt-4o-transcribe family share a
        # removal date. Left to fall through, this would post a request with no
        # model and surface as the vendor complaining about a missing field,
        # which points at the wrong file entirely.
        msg = (
            f"capabilities.yaml offers no transcription model for kind "
            f"{config.kind.value!r}; choose a model explicitly or add one to the file"
        )
        raise ValueError(msg)
    realtime = is_realtime_model(config.kind, resolved)
    factory = _REGISTRY.get((config.kind, realtime))
    if factory is not None:
        return factory(config, secrets, resolved)
    if (config.kind, not realtime) in _REGISTRY:
        # The kind exists but this model needs the transport we lack: say so,
        # rather than letting the wrong connector produce a provider-side
        # error that never mentions the transport. No shipped kind hits this
        # today (Gemini's -live model gained its Live API connector), but the
        # next vendor to split its catalogue across transports will.
        transport = "streaming" if realtime else "batch"
        msg = (
            f"model {resolved!r} needs a {transport} connector, which this app "
            f"does not have for kind {config.kind.value!r}"
        )
        raise ValueError(msg)
    msg = f"no STT backend registered for kind {config.kind.value!r}"
    raise ValueError(msg)


def registered_transports() -> frozenset[tuple[ProviderKind, bool]]:
    """Every (kind, realtime) pair that has a backend.

    Exposed so the capability config can be checked against what can actually
    run, rather than that check reaching into the registry's internals.
    """
    return frozenset(_REGISTRY)


def registered_kinds() -> list[ProviderKind]:
    """Return the provider kinds with at least one registered backend."""
    _load_backends()
    return sorted({kind for kind, _ in _REGISTRY})


def _load_backends() -> None:
    """Import backend modules so their ``@register`` decorators run."""
    from loreline.stt import backends  # noqa: PLC0415

    backends.load()
