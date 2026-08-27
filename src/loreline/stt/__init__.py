"""STT backend abstraction, registry, and connectors."""

from __future__ import annotations

from loreline.stt.base import STTBackend, glossary_terms
from loreline.stt.registry import BackendFactory, create_backend, register
from loreline.stt.router import RouterConfig, SttRouter

__all__ = [
    "BackendFactory",
    "RouterConfig",
    "STTBackend",
    "SttRouter",
    "create_backend",
    "glossary_terms",
    "register",
]
