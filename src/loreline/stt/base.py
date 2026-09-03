"""STT backend protocol.

A backend consumes a stream of voiced ``Utterance`` chunks (from the audio
pipeline) and yields ``TranscriptEvent`` objects. Batch backends (e.g. an
OpenAI-compatible ``/v1/audio/transcriptions`` endpoint) emit a single final
event per utterance; streaming backends (Deepgram/AssemblyAI WS) may emit
multiple interim events before a final.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable
from typing import Protocol, runtime_checkable

from loreline.audio.chunker import Utterance
from loreline.capabilities import config as capability_config
from loreline.capability_config import GlossarySupport, TranscribeCapabilities
from loreline.health import HealthReport, error_detail
from loreline.logging import get_logger
from loreline.models import Glossary, ProviderConfig, ProviderKind, TranscriptEvent

log = get_logger(__name__)

# Re-exported: ``error_detail`` moved to loreline.health so that grading a
# health probe and reporting a failed utterance read the same vendor body the
# same way, including Google's array-wrapped envelope. The connectors keep
# importing it from here, where they always have.
__all__ = [
    "FeatureConflictGuard",
    "STTBackend",
    "capped_terms",
    "error_detail",
    "glossary_support",
    "glossary_terms",
    "glossary_terms_for",
    "http_base_url",
    "transcribe_capabilities",
]


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

    async def health(self) -> HealthReport:
        """Probe the endpoint and the credential, without raising.

        Returns a graded :class:`~loreline.health.HealthReport` rather than a
        bool: "reachable" and "the key works" are separate facts and used to be
        collapsed into one, inconsistently, per connector. See
        :mod:`loreline.health`.
        """
        ...

    async def aclose(self) -> None:
        """Release any held resources (connections, clients)."""
        ...


def glossary_terms(glossary: Glossary | None) -> list[str]:
    """Return glossary terms, or an empty list when none configured."""
    return list(glossary.terms) if glossary else []


def transcribe_capabilities(
    kind: ProviderKind,
    model: str | None,
) -> TranscribeCapabilities | None:
    """Curated transcription surface for a provider+model pair.

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
    return entry.transcribe if entry else None


def glossary_support(kind: ProviderKind, model: str | None) -> GlossarySupport | None:
    """Curated keyword-biasing surface for a provider+model pair, or None."""
    caps = transcribe_capabilities(kind, model)
    return caps.glossary if caps else None


class FeatureConflictGuard:
    """Drops the features a model refuses to combine, once per backend instance.

    capabilities.yaml has always been able to say that two transcription
    features cannot be sent together, and nothing enforced it: Gemini's batch
    transcriber declares ``[glossary, word_timestamps]`` and
    ``[glossary, inline_diarization]``, sent all three anyway, and every
    utterance of every re-process for a campaign with a glossary came back
    ``400 ... custom_vocabulary is incompatible with timestamps``. A declared
    rule that request time ignores is worse than no rule, because the picker
    greys a control out on the strength of it.

    Resolution is ``TranscribeCapabilities.resolve_conflicts``, so the policy
    (see ``CONFLICT_PRECEDENCE``) lives with the data rather than in each
    connector, and a model that declares no conflicts is unaffected.

    Why an object and not a function: a connector's ``transcribe`` is called
    once per *utterance*, so a log line emitted where the decision is made
    would reproduce the flood it replaces, one line per utterance for hours.
    The guard is built once with the backend, which lives for the session or
    the job, and reports the first time it drops anything.
    """

    def __init__(self, config: ProviderConfig, model: str | None) -> None:
        self._config = config
        self._model = model
        self._reported = False

    def allowed(self, requested: Iterable[str]) -> frozenset[str]:
        """Which of ``requested`` may be sent, reporting a drop once."""
        wanted = frozenset(requested)
        caps = transcribe_capabilities(self._config.kind, self._model)
        if caps is None:
            return wanted
        kept = caps.resolve_conflicts(wanted)
        dropped = wanted - kept
        if dropped and not self._reported:
            self._reported = True
            log.warning(
                "stt.features.dropped",
                provider=self._config.name,
                provider_id=self._config.id,
                model=self._model,
                dropped=sorted(dropped),
                kept=sorted(kept),
            )
        return kept


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


def glossary_terms_for(
    kind: ProviderKind,
    model: str | None,
    terms: list[str],
    *,
    realtime: bool,
    fallback_max_terms: int | None = None,
) -> list[str]:
    """The glossary terms this model will actually accept on this transport.

    Two rules, both read from capabilities.yaml so no connector restates them:
    a model annotated ``glossary.supported: false`` is sent nothing at all
    rather than a parameter its endpoint ignores or rejects, and anything over
    the model's documented ceiling for this transport is trimmed here (see
    :func:`capped_terms` for why trimming beats letting the vendor decide).

    ``fallback_max_terms`` covers a model the yaml does not annotate, where
    there is no ceiling to read. Only the Gemini batch connector passes one:
    that service rejects the request outright over 1000 entries, so an
    uncurated Gemini id still has to be trimmed somewhere.
    """
    support = glossary_support(kind, model)
    if support is not None and not support.supported:
        return []
    limit = support.max_terms_for(realtime=realtime) if support else None
    if limit is None and fallback_max_terms is not None:
        return terms[:fallback_max_terms]
    return capped_terms(terms, support, realtime=realtime)


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
