"""STT router: primary/fallback failover.

Consumes a stream of voiced ``Utterance`` chunks and drives one STT backend
per utterance, publishing ``TranscriptEvent`` objects to an ``EventBus``. On
error or timeout, an optional fallback backend is tried. Diarization (inline
or remote) is merged onto each event before publishing. A failure that will
repeat for every remaining utterance (a rejected key, an exhausted balance, a
model that does not exist) retires that provider instead of being paid for
again, and once every provider is retired the run raises
``ProvidersExhaustedError`` rather than looking busy while transcribing
nothing.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field

from loreline.audio.chunker import Utterance
from loreline.audio.wav import pcm_to_wav
from loreline.bus import EventBus
from loreline.diarization.base import DiarizationProvider
from loreline.diarization.merge import assign_speakers, segments_from_words
from loreline.health import classify_request_error
from loreline.logging import get_logger
from loreline.models import DiarizationConfig, DiarizationMode, Glossary, TranscriptEvent
from loreline.stt.base import STTBackend

log = get_logger(__name__)


class ProvidersExhaustedError(RuntimeError):
    """Every provider this router can use has failed terminally.

    Raised out of :meth:`SttRouter.run`, carrying each dead provider's own
    words ("OpenAI: You have no credits remaining."). Nothing below this point
    can produce a transcript, so continuing would only spend two doomed API
    calls per utterance and fill the log with the same line - which is exactly
    what a live deployment reported.

    What to do about it is the caller's, because the two callers differ:

    * a re-process job has nothing left to do and ends failed, with this
      message on the job row (see ``loreline.reprocess.jobs``);
    * a live capture keeps recording and only stops transcribing, because the
      audio is the part that cannot be recreated (see
      ``loreline.session.manager``).
    """


@dataclass(slots=True)
class RouterConfig:
    """Per-session router settings."""

    session_id: str
    glossary: Glossary | None = None
    diarization: DiarizationConfig = field(default_factory=DiarizationConfig)
    timeout_s: float = 30.0


# Consecutive utterances that produced no transcript (primary and fallback both
# failing) before the session counts as degraded. One or two misses are routine
# transient errors; a streak means transcription has actually stopped flowing.
_DEGRADED_AFTER_FAILURES = 3


class SttRouter:
    """Route utterances through STT backends with failover and diarization."""

    def __init__(
        self,
        primary: STTBackend,
        bus: EventBus[TranscriptEvent],
        config: RouterConfig,
        *,
        fallback: STTBackend | None = None,
        diarizer: DiarizationProvider | None = None,
        on_failover: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        self._primary = primary
        self._fallback = fallback
        self._bus = bus
        self._config = config
        self._diarizer = diarizer
        self._on_failover = on_failover
        self._consecutive_failures = 0
        self._degraded_since: float | None = None
        # provider id -> "<name>: <the vendor's own sentence>", for a provider
        # whose failure will repeat for every remaining utterance. Retiring it
        # is what stops the doomed calls and the per-utterance log line.
        self._retired: dict[str, str] = {}

    @property
    def terminal_error(self) -> str | None:
        """Why transcription stopped for good, or None while any provider works.

        Set together with the :class:`ProvidersExhaustedError` that ends
        :meth:`run`, so a live capture that swallows the error can still show
        the GM what the vendor said.
        """
        return self._exhausted_message() if self._retired and not self._viable() else None

    @property
    def degraded_since(self) -> float | None:
        """Epoch time live transcription entered its current failing streak.

        None while healthy. Set once a run of ``_DEGRADED_AFTER_FAILURES``
        consecutive utterances produced no transcript at all (fallback
        included); cleared by the next utterance that transcribes.
        """
        return self._degraded_since

    async def run(self, utterances: AsyncIterator[Utterance]) -> None:
        """Drive the live transcription loop until the stream ends.

        Raises :class:`ProvidersExhaustedError` if every provider fails
        terminally before the stream does; the caller decides what that means
        for its kind of run.
        """
        async for utterance in utterances:
            events = await self._transcribe_with_failover(utterance)
            for event in events:
                merged = await self._merge_diarization(event, utterance)
                await self._bus.publish(merged)

    async def _transcribe_with_failover(self, utterance: Utterance) -> list[TranscriptEvent]:
        """Try each provider still worth trying; [] when none produced a transcript.

        Failover runs before any of the terminal handling below, and that order
        is deliberate: a primary with no credits left is precisely when the
        fallback should be asked, since it is usually a different vendor with a
        different bill. Only when nothing is left to ask does the run stop.
        """
        last_error = ""
        for backend, role in self._viable():
            try:
                events = await asyncio.wait_for(
                    self._collect(backend, utterance), timeout=self._config.timeout_s
                )
            except Exception as exc:  # resilience: any backend error triggers fallback
                last_error = self._note_backend_failed(backend, role, exc)
            else:
                self._note_transcribed()
                return events
        if not self._viable():
            # Every provider has answered in a way that will not change. Stop
            # rather than repeat it for every remaining utterance.
            if self._degraded_since is None:
                self._degraded_since = time.time()
            raise ProvidersExhaustedError(self._exhausted_message())
        await self._note_dropped(last_error)
        return []

    def _viable(self) -> list[tuple[STTBackend, str]]:
        """The backends still worth a request, primary first, with their role.

        The role is only there to keep the two long-standing log events
        distinct: an unavailable primary is routine and a warning, while a
        fallback failing on top of it means nothing got transcribed at all.
        """
        candidates = [(self._primary, "primary")]
        if self._fallback is not None:
            candidates.append((self._fallback, "fallback"))
        return [(b, role) for b, role in candidates if b.config.id not in self._retired]

    def _note_backend_failed(self, backend: STTBackend, role: str, exc: Exception) -> str:
        """Log one backend's failure and return it as a one-line reason.

        A terminal failure is logged once, here, and then never again for this
        provider: it is retired, so the next utterance does not call it, does
        not wait out its timeout, and does not repeat this line. That flood is
        half of what made the reported outage look like a working run.
        """
        failure = classify_request_error(exc)
        reason = f"{backend.config.name}: {failure.detail}"
        if failure.terminal:
            self._retired[backend.config.id] = reason
            log.error(
                "stt.provider.terminal",
                provider=backend.config.name,
                provider_id=backend.config.id,
                role=role,
                status=failure.status.value,
                error=failure.detail,
            )
            return reason
        # An unavailable primary still has a fallback behind it; a fallback
        # failing on top of it means this utterance is lost, so it is louder.
        emit = log.error if role == "fallback" else log.warning
        emit(
            f"stt.{role}.failed",
            provider=backend.config.name,
            provider_id=backend.config.id,
            error=str(exc),
        )
        return reason

    def _exhausted_message(self) -> str:
        """Every retired provider's own words, in the order they were tried."""
        return "; ".join(self._retired.values())

    def _note_transcribed(self) -> None:
        """An utterance produced a transcript; end any failing streak."""
        if self._degraded_since is not None:
            log.info("stt.recovered", failures=self._consecutive_failures)
        self._consecutive_failures = 0
        self._degraded_since = None

    async def _note_dropped(self, error: str) -> None:
        """An utterance produced no transcript at all; track the streak.

        The push alert fires once, on the transition into degraded - not per
        failure, which floods every configured channel when a backend stays
        down for hours.
        """
        self._consecutive_failures += 1
        if self._consecutive_failures != _DEGRADED_AFTER_FAILURES:
            return
        self._degraded_since = time.time()
        log.error("stt.degraded", failures=self._consecutive_failures, error=error)
        await self._notify_failover(
            f"Live transcription is failing ({error}). "
            "Audio keeps recording; the session can be re-transcribed later."
        )

    async def _notify_failover(self, message: str) -> None:
        if self._on_failover is None:
            return
        with contextlib.suppress(Exception):
            await self._on_failover(message)

    async def _collect(self, backend: STTBackend, utterance: Utterance) -> list[TranscriptEvent]:
        events: list[TranscriptEvent] = []
        async for event in backend.transcribe(
            _single(utterance),
            session_id=self._config.session_id,
            glossary=self._config.glossary,
        ):
            events.append(event)
        return events

    async def _merge_diarization(
        self, event: TranscriptEvent, utterance: Utterance
    ) -> TranscriptEvent:
        mode = self._config.diarization.mode
        if mode == DiarizationMode.REMOTE and self._diarizer is not None:
            wav = pcm_to_wav(utterance.pcm, sample_rate=self._primary.config.sample_rate)
            segments = await self._diarizer.diarize(
                wav,
                sample_rate=self._primary.config.sample_rate,
                min_speakers=self._config.diarization.min_speakers,
                max_speakers=self._config.diarization.max_speakers,
            )
            # The diarizer only sees this utterance's isolated audio, so its
            # segments are utterance-relative (0-based); shift them to match the
            # word timings, which already carry the utterance's session offset.
            shifted = [
                s.model_copy(
                    update={"start": s.start + utterance.start, "end": s.end + utterance.start}
                )
                for s in segments
            ]
            return assign_speakers(event, shifted)
        if mode == DiarizationMode.INLINE:
            return assign_speakers(event, segments_from_words(event.words))
        return event


async def _single(utterance: Utterance) -> AsyncIterator[Utterance]:
    yield utterance
