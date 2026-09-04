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
several interactions at once, so it cannot carry one model, and every caller
(a session, a re-processing job) has chosen one. Nothing here resolves a
default: the one caller that used to arrive without a model, the health probe,
no longer builds a connector at all (see :mod:`loreline.health_probe`).

What the model *can do* is resolved here too, and only here.
``create_backend`` reads the pair's ``TranscribeCapabilities`` out of
capabilities.yaml once and hands the value to the connector, which then reads
the glossary ceiling and the conflict groups off it. The same request used to
collect that answer four times, in the session manager, here, and twice inside
the connector, and four lookups of one fact are four chances to disagree about
which model is running.
"""

from __future__ import annotations

from collections.abc import Callable

from loreline.capabilities import is_realtime_model
from loreline.capability_config import TranscribeCapabilities
from loreline.models import ProviderConfig, ProviderKind
from loreline.secrets import SecretStore
from loreline.stt.base import STTBackend, transcribe_capabilities

# The third argument is the chosen model, or None for a kind whose catalogue
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

# What ``@register`` decorates. One argument more than a ``BackendFactory``:
# the model's resolved capabilities, or None where the pair is not annotated.
# Only ``create_backend`` resolves that value, so it is the only caller that
# can supply it, and an injected test double stays a ``BackendFactory``.
ConnectorFactory = Callable[
    [ProviderConfig, SecretStore, str | None, TranscribeCapabilities | None], STTBackend
]

_REGISTRY: dict[tuple[ProviderKind, bool], ConnectorFactory] = {}


def register(
    kind: ProviderKind, *, realtime: bool = False
) -> Callable[[ConnectorFactory], ConnectorFactory]:
    """Decorator registering a backend factory for a provider kind + transport.

    ``realtime=True`` marks a streaming connector (interim results within an
    utterance); the default covers batch connectors, which post one complete
    utterance per request. A kind may register one of each.
    """

    def decorator(factory: ConnectorFactory) -> ConnectorFactory:
        key = (kind, realtime)
        if key in _REGISTRY:
            msg = f"backend already registered for {kind} (realtime={realtime})"
            raise ValueError(msg)
        _REGISTRY[key] = factory
        return factory

    return decorator


def create_backend(config: ProviderConfig, secrets: SecretStore, model: str | None) -> STTBackend:
    """Instantiate a backend for ``config``, resolved by kind *and* model.

    ``model`` is what the session, the re-processing job or the picker chose,
    and choosing is the caller's job: nothing is resolved here, so the
    transport lookup and the connector always agree on which model is
    running, which passing it through the config was how they could disagree.

    None is allowed for one reason only, a kind whose models are whatever the
    operator installed (the self-hosted one): its connector then names no
    model and lets the server apply its own default. Every other kind's
    callers name one, because their request schemas require it.

    The pair's capabilities are resolved here, once, and handed to the
    connector. An unknown model - one the yaml does not annotate, and the unset
    model above - resolves to None, which every reader downstream already
    treats as "not annotated, send what you would have sent". That rule is
    applied in this one place instead of separately wherever a capability was
    looked up.
    """
    _load_backends()
    realtime = is_realtime_model(config.kind, model)
    caps = transcribe_capabilities(config.kind, model)
    factory = _REGISTRY.get((config.kind, realtime))
    if factory is not None:
        return factory(config, secrets, model, caps)
    if (config.kind, not realtime) in _REGISTRY:
        # The kind exists but this model needs the transport we lack: say so,
        # rather than letting the wrong connector produce a provider-side
        # error that never mentions the transport. No shipped kind hits this
        # today (Gemini's -live model gained its Live API connector), but the
        # next vendor to split its catalogue across transports will.
        transport = "streaming" if realtime else "batch"
        msg = (
            f"model {model!r} needs a {transport} connector, which this app "
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
