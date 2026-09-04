"""One factory for a session's or a job's diarizer, and the owner of its credentials."""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable, Iterable
from typing import Protocol

from loreline.diarization.base import DiarizationProvider, NoopDiarizer
from loreline.diarization.remote import RemoteDiarizer
from loreline.models import DiarizationConfig, DiarizationMode, ProviderConfig, ProviderKind

# What both managers take, and what a test injects in place of the real
# factory: anything that turns a config into a diarizer, awaited.
BuildDiarizer = Callable[[DiarizationConfig], Awaitable[DiarizationProvider]]

OPENAI_KEY_ENV = "OPENAI_API_KEY"


class ProviderRows(Protocol):
    """The slice of :class:`loreline.persistence.ProviderRepository` the factory reads."""

    async def list(self) -> list[ProviderConfig]: ...


class KeyStore(Protocol):
    """The slice of :class:`loreline.secrets.SecretStore` the factory reads."""

    def get(self, name: str) -> str | None: ...


def resolve_openai_key(providers: Iterable[ProviderConfig], secrets: KeyStore) -> str | None:
    """The key OpenAI batch diarization runs with.

    A configured OpenAI provider row's stored key wins, so the GM never has to
    keep a second copy of it in the environment; ``OPENAI_API_KEY`` is the
    fallback when no row has one; None when neither is set, and the request
    then goes out without a key and the vendor says so.
    """
    for provider in providers:
        if provider.kind == ProviderKind.OPENAI and provider.auth_ref:
            key = secrets.get(provider.auth_ref)
            if key:
                return key
    return os.environ.get(OPENAI_KEY_ENV)


class DiarizerFactory:
    """Build the diarizer for one ``DiarizationConfig``.

    Built once in ``loreline.web.app`` and handed to both the session manager
    and the reprocess manager, so a live session and a reprocess job in the
    same mode get the same diarizer with the same credentials. This is the
    only place the OpenAI key is resolved (see :func:`resolve_openai_key`).
    """

    def __init__(self, providers: ProviderRows, secrets: KeyStore) -> None:
        self._providers = providers
        self._secrets = secrets

    async def __call__(self, config: DiarizationConfig) -> DiarizationProvider:
        key = None
        if config.mode == DiarizationMode.OPENAI:
            key = resolve_openai_key(await self._providers.list(), self._secrets)
        return create_diarizer(config, openai_api_key=key)


def create_diarizer(
    config: DiarizationConfig, *, openai_api_key: str | None = None
) -> DiarizationProvider:
    """Construct the diarizer for the configured mode, credentials given.

    ``inline`` returns a no-op diarizer: speaker labels already arrive on STT
    words, and segments are derived via ``segments_from_words`` at merge time.
    ``remote`` calls a self-hosted sherpa-onnx service. ``none`` produces no
    segments. ``openai`` takes the key exactly as passed: resolving one is
    :class:`DiarizerFactory`'s job, and this stays the only place the OpenAI
    diarizer is constructed.
    """
    if config.mode == DiarizationMode.REMOTE:
        if not config.endpoint:
            msg = "remote diarization requires an endpoint"
            raise ValueError(msg)
        return RemoteDiarizer(config.endpoint)
    if config.mode == DiarizationMode.OPENAI:
        from loreline.diarization.openai_diarizer import OpenAIDiarizer  # noqa: PLC0415

        return OpenAIDiarizer(api_key=openai_api_key)
    return NoopDiarizer()
