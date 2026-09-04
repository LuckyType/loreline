"""The STT backend contract and the Connector spine the connectors share.

A backend consumes a stream of voiced ``Utterance`` chunks (from the audio
pipeline) and yields ``TranscriptEvent`` objects, one final event per
utterance. Two things in this module say what a backend is:

``STTBackend``, the Protocol, is the contract the rest of the app consumes:
``SttRouter``, the session manager, the re-process jobs and every test fake
speak to it and nothing else. It is structural on purpose. Nothing subclasses
it, the fakes in the tests satisfy it by shape, and it stays alongside the base
class below so that a caller never has to know how a backend was built.

``Connector`` is how the eight real connectors are built. Every one of them
does the same thing per utterance: build the per-call setup once from the
glossary, send each utterance to the vendor, turn the answer into text and
words, and wrap that in a ``TranscriptEvent`` whose fields are the same eight
whichever vendor answered. The base class owns that loop and the event; a
connector supplies two hooks:

* ``prepare(glossary) -> P``: whatever one call to ``transcribe`` needs once
  for all of its utterances (a URL with the query string built, a params tuple,
  a capped term list, a prompt). The type ``P`` is the connector's own, and a
  connector with nothing to prepare returns None.
* ``transcribe_one(utterance, prepared) -> Transcription | None``: one
  utterance to the vendor and back. None means "no event for this one", which
  is what a connector says when the vendor answered but not with a transcript
  (an incomplete status, an empty channel list). Empty text is skipped by the
  base, so a hook may also return a ``Transcription`` with nothing in it.

A connector has no health method. Whether a provider row works is a question
about its declared surface and its key, not about a connector, and
:mod:`loreline.health_probe` answers it without building one; ``aclose`` is a
no-op until a connector holds something.

``HttpConnector`` is the one level below ``Connector`` and the last: it serves
the four batch connectors, which post through an ``httpx.AsyncClient`` that is
either injected (tests) or owned, and which all report a failed request with
the vendor's body in the message because the status line alone ("400 Bad
Request") never says which parameter was wrong.

The speaker rule, stated once: an event's ``speaker`` is the speaker of the
first word that carries one (:func:`first_labelled_speaker`), else None. Three
connectors used to read the first word's speaker whether it had one or not,
which turned "the first word came back unattributed" into "this utterance has
no speaker" even when every other word was labelled.

Everything below the classes is shared glossary policy read from
capabilities.yaml, so no connector restates a per-model ceiling in code. Where
a connector posts, and how it spells the credential, is read from the same
file: each connector asks :func:`loreline.capabilities.surface_for` for the
surface its kind declares for its transport, which is also where a provider
row's ``base_url`` is applied (a socket address reaches the streaming
connector and never the batch one, and the reverse).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass, field
from http import HTTPStatus
from typing import Protocol, runtime_checkable

import httpx

from loreline.audio.chunker import Utterance
from loreline.capabilities import config as capability_config
from loreline.capability_config import GlossarySupport, TranscribeCapabilities
from loreline.health import error_detail
from loreline.httpclient import ClientHandle
from loreline.logging import get_logger
from loreline.models import Glossary, ProviderConfig, ProviderKind, TranscriptEvent, Word
from loreline.secrets import SecretStore

log = get_logger(__name__)

# Re-exported: ``error_detail`` moved to loreline.health so that grading a
# health probe and reporting a failed utterance read the same vendor body the
# same way, including Google's array-wrapped envelope. The connectors keep
# importing it from here, where they always have.
__all__ = [
    "Connector",
    "FeatureConflictGuard",
    "HttpConnector",
    "STTBackend",
    "Transcription",
    "capped_terms",
    "error_detail",
    "first_labelled_speaker",
    "glossary_support",
    "glossary_terms",
    "glossary_terms_for",
    "secret_for",
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

    async def aclose(self) -> None:
        """Release any held resources (connections, clients)."""
        ...


@dataclass(frozen=True, slots=True)
class Transcription:
    """One utterance's result: the text plus whatever structure came with it.

    ``words`` is empty for a vendor that returns none (the OpenAI Realtime and
    Gemini Live sessions), which leaves the event exactly as it was before
    word timings existed here.
    """

    text: str
    words: list[Word] = field(default_factory=list[Word])


def first_labelled_speaker(words: Iterable[Word]) -> str | None:
    """The utterance's speaker: that of its first word carrying one, else None.

    The first *labelled* word, not the first word. A vendor can leave a lead-in
    word unattributed, and an utterance whose speaker is known from its second
    word on is not an utterance with no speaker.
    """
    return next((w.speaker for w in words if w.speaker), None)


def secret_for(config: ProviderConfig, secrets: SecretStore) -> str | None:
    """The credential a registry factory hands its connector.

    None when the config names no ``auth_ref``, which is a normal state for a
    kind whose capabilities.yaml entry says ``auth: optional`` (a self-hosted
    server that checks no key).
    """
    return secrets.get(config.auth_ref) if config.auth_ref else None


class Connector[P](ABC):
    """The shared spine behind every connector: see the module docstring.

    ``P`` is whatever :meth:`prepare` builds once per ``transcribe`` call and
    :meth:`transcribe_one` reads per utterance. The base never looks inside
    it.
    """

    config: ProviderConfig

    def __init__(self, config: ProviderConfig) -> None:
        self.config = config

    @abstractmethod
    def prepare(self, glossary: Glossary | None) -> P:
        """Per-call setup, computed once for every utterance of one stream."""

    @abstractmethod
    async def transcribe_one(self, utterance: Utterance, prepared: P) -> Transcription | None:
        """One utterance to the vendor and back; None means no event for it."""

    async def transcribe(
        self,
        audio: AsyncIterator[Utterance],
        *,
        session_id: str,
        glossary: Glossary | None = None,
    ) -> AsyncIterator[TranscriptEvent]:
        prepared = self.prepare(glossary)
        async for utterance in audio:
            result = await self.transcribe_one(utterance, prepared)
            if result is None or not result.text:
                continue
            yield TranscriptEvent(
                session_id=session_id,
                source=self.config.id,
                text=result.text,
                words=result.words,
                speaker=first_labelled_speaker(result.words),
                start_ts=utterance.start,
                end_ts=utterance.end,
                is_final=True,
            )

    async def aclose(self) -> None:  # noqa: B027  (a real default, not a forgotten hook)
        """Release held resources. Nothing is held unless a connector says so."""


class HttpConnector[P](Connector[P]):
    """A connector that posts each utterance over ``httpx``.

    The client is injected or owned (see :mod:`loreline.httpclient`); a
    subclass reads it as ``self._client`` and closes nothing itself. Failed
    requests go through :meth:`_raise_for_status`, so every batch connector
    reports the vendor's own reason the same way.
    """

    def __init__(
        self,
        config: ProviderConfig,
        *,
        client: httpx.AsyncClient | None,
        base_url: str,
        headers: dict[str, str] | None = None,
        timeout: float,
    ) -> None:
        super().__init__(config)
        self._http = ClientHandle(client, base_url=base_url, headers=headers, timeout=timeout)
        self._client = self._http.client

    def _raise_for_status(self, response: httpx.Response) -> None:
        """Raise on a 4xx/5xx with the vendor's body in the message.

        ``raise_for_status()`` alone reports only "404 Not Found", throwing away
        the body, which is where these servers put the actual reason ("Model
        'X' is not installed locally", "keyterm is not supported for this
        model", "Not authorized"). Keeping it is usually the difference between
        a mystery and an obvious fix.
        """
        if response.status_code < HTTPStatus.BAD_REQUEST:
            return
        raise httpx.HTTPStatusError(
            f"{response.status_code} from {response.request.url}: {error_detail(response)}",
            request=response.request,
            response=response,
        )

    async def aclose(self) -> None:
        await self._http.aclose()


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
