"""STT backend protocol.

A backend consumes a stream of voiced ``Utterance`` chunks (from the audio
pipeline) and yields ``TranscriptEvent`` objects. Batch backends (e.g. an
OpenAI-compatible ``/v1/audio/transcriptions`` endpoint) emit a single final
event per utterance; streaming backends (Deepgram/AssemblyAI WS) may emit
multiple interim events before a final.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol, cast, runtime_checkable

import httpx

from loreline.audio.chunker import Utterance
from loreline.models import Glossary, ProviderConfig, TranscriptEvent


@runtime_checkable
class STTBackend(Protocol):
    """Pluggable speech-to-text connector."""

    config: ProviderConfig

    def transcribe(
        self,
        audio: AsyncIterator[Utterance],
        *,
        session_id: str,
        glossary: Glossary | None = None,
    ) -> AsyncIterator[TranscriptEvent]:
        """Transcribe a stream of utterances into transcript events."""
        ...

    async def health(self) -> bool:
        """Return True if the backend endpoint is reachable."""
        ...

    async def aclose(self) -> None:
        """Release any held resources (connections, clients)."""
        ...


def glossary_terms(glossary: Glossary | None) -> list[str]:
    """Return glossary terms, or an empty list when none configured."""
    return list(glossary.terms) if glossary else []


def error_detail(response: httpx.Response) -> str:
    """Best-effort human-readable reason from an HTTP error response body.

    Status lines alone ("404 Not Found") throw away the part that actually
    says what to fix - "Model 'X' is not installed locally", "API key not
    valid". Providers bury that under varying keys, so try the common ones.
    """
    try:
        payload: object = response.json()
    except ValueError:
        return response.text[:300].strip() or response.reason_phrase
    if isinstance(payload, dict):
        mapping = cast("dict[str, object]", payload)
        for key in ("detail", "message", "error"):
            value = mapping.get(key)
            if isinstance(value, str) and value:
                return value
            if isinstance(value, dict):
                nested = cast("dict[str, object]", value).get("message")
                if isinstance(nested, str) and nested:
                    return nested
    return response.text[:300].strip() or response.reason_phrase
