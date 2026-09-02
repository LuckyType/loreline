"""STT backend connectors.

Importing this package's ``load()`` triggers registration of all available
backends with the registry.
"""

from __future__ import annotations

import importlib

_BACKEND_MODULES = (
    "loreline.stt.backends.openai_compat",
    "loreline.stt.backends.openrouter",
    "loreline.stt.backends.openai_realtime",
    "loreline.stt.backends.deepgram",
    "loreline.stt.backends.assemblyai",
    "loreline.stt.backends.gemini",
    "loreline.stt.backends.gemini_live",
)


def load() -> None:
    """Import backend modules to trigger their registration side effects."""
    for module in _BACKEND_MODULES:
        importlib.import_module(module)
