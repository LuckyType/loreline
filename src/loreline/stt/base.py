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
from loreline.capabilities import config as capability_config
from loreline.capability_config import GlossarySupport
from loreline.logging import get_logger
from loreline.models import Glossary, ProviderConfig, ProviderKind, TranscriptEvent

log = get_logger(__name__)


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


def glossary_support(kind: ProviderKind, model: str | None) -> GlossarySupport | None:
    """Curated keyword-biasing surface for a provider+model pair.

    None means "not annotated", which is not the same as "unsupported": the
    caller keeps whatever it would have sent anyway. capabilities.yaml is where
    the per-model traps are already written down (Deepgram nova-3 takes
    ``keyterm`` while nova-2 takes legacy ``keywords``; AssemblyAI caps the same
    model at 1000 terms async and 100 streaming), so a connector reads them from
    there rather than restating them in code and drifting from the file the UI
    renders from.
    """
    if not model:
        return None
    spec = capability_config().provider(kind)
    entry = spec.find(model) if spec else None
    caps = entry.transcribe if entry else None
    return caps.glossary if caps else None


def capped_terms(terms: list[str], support: GlossarySupport | None, *, realtime: bool) -> list[str]:
    """Trim a glossary to the model's documented ceiling for one transport.

    Over the ceiling the vendors differ between ignoring the surplus and
    rejecting the whole request (AssemblyAI streaming errors above 100 terms),
    and a failed request costs the whole utterance, so trimming is the safer
    read. Glossary order is priority order, so the head is what survives.
    Ceilings expressed in tokens rather than terms (Deepgram's 500-token
    keyterm budget) cannot be counted here and are left alone.
    """
    limit = support.max_terms_for(realtime=realtime) if support else None
    if limit is None or len(terms) <= limit:
        return terms
    log.warning("stt.glossary.truncated", limit=limit, dropped=len(terms) - limit)
    return terms[:limit]


def http_base_url(base_url: str | None) -> str | None:
    """A stored ``base_url`` an HTTP connector can actually use.

    For the kinds whose streaming connector shipped first, base_url has always
    meant that vendor's WebSocket endpoint, and a ``wss://`` URL handed to a
    REST client fails every request. Dropping it falls back to the vendor's
    documented HTTP host, which is what such a config meant all along - the
    same call the OPENAI batch factory makes in stt/backends/openai_compat.py.
    """
    if base_url and base_url.lower().startswith(("ws://", "wss://")):
        return None
    return base_url


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
