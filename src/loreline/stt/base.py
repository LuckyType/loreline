"""STT backend protocol.

A backend consumes a stream of voiced ``Utterance`` chunks (from the audio
pipeline) and yields ``TranscriptEvent`` objects. Batch backends (e.g. an
OpenAI-compatible ``/v1/audio/transcriptions`` endpoint) emit a single final
event per utterance; streaming backends (Deepgram/AssemblyAI WS) may emit
multiple interim events before a final.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable

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
