"""STT router: primary/fallback failover + compare fan-out.

Consumes a stream of voiced ``Utterance`` chunks and drives one or more STT
backends, publishing ``TranscriptEvent`` objects to an ``EventBus``:

- **Live mode** (``run``): a single primary backend per utterance; on error or
  timeout, an optional fallback backend is tried. Diarization (inline or remote)
  is merged onto each event before publishing.
- **Compare mode** (``transcribe_compare``): fan out the same utterance to every
  configured backend and return their outputs side by side (web-UI testing).
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass, field

from loreline.audio.chunker import Utterance
from loreline.audio.wav import pcm_to_wav
from loreline.bus import EventBus
from loreline.diarization.base import DiarizationProvider
from loreline.diarization.merge import assign_speakers, segments_from_words
from loreline.logging import get_logger
from loreline.models import DiarizationConfig, DiarizationMode, Glossary, TranscriptEvent
from loreline.stt.base import STTBackend

log = get_logger(__name__)


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

    @property
    def degraded_since(self) -> float | None:
        """Epoch time live transcription entered its current failing streak.

        None while healthy. Set once a run of ``_DEGRADED_AFTER_FAILURES``
        consecutive utterances produced no transcript at all (fallback
        included); cleared by the next utterance that transcribes.
        """
        return self._degraded_since

    async def run(self, utterances: AsyncIterator[Utterance]) -> None:
        """Drive the live transcription loop until the stream ends."""
        async for utterance in utterances:
            events = await self._transcribe_with_failover(utterance)
            for event in events:
                merged = await self._merge_diarization(event, utterance)
                await self._bus.publish(merged)

    async def transcribe_compare(
        self, utterance: Utterance, backends: Mapping[str, STTBackend]
    ) -> dict[str, list[TranscriptEvent]]:
        """Fan an utterance out to multiple backends; return per-backend events."""
        results = await asyncio.gather(
            *(self._collect(backend, utterance) for backend in backends.values()),
            return_exceptions=True,
        )
        output: dict[str, list[TranscriptEvent]] = {}
        for provider_id, result in zip(backends, results, strict=True):
            if isinstance(result, BaseException):
                log.warning(
                    "stt.compare.error",
                    provider=backends[provider_id].config.name,
                    provider_id=provider_id,
                    error=str(result),
                )
                output[provider_id] = []
            else:
                output[provider_id] = result
        return output

    async def _transcribe_with_failover(self, utterance: Utterance) -> list[TranscriptEvent]:
        last_error = ""
        try:
            events = await asyncio.wait_for(
                self._collect(self._primary, utterance), timeout=self._config.timeout_s
            )
        except Exception as exc:  # resilience: any backend error triggers fallback
            last_error = f"{self._primary.config.name}: {exc}"
            log.warning(
                "stt.primary.failed",
                provider=self._primary.config.name,
                provider_id=self._primary.config.id,
                error=str(exc),
            )
        else:
            self._note_transcribed()
            return events
        if self._fallback is not None:
            try:
                events = await asyncio.wait_for(
                    self._collect(self._fallback, utterance), timeout=self._config.timeout_s
                )
            except Exception as exc:  # both backends failed; emit nothing for this utterance
                last_error = f"{self._fallback.config.name}: {exc}"
                log.error(
                    "stt.fallback.failed",
                    provider=self._fallback.config.name,
                    provider_id=self._fallback.config.id,
                    error=str(exc),
                )
            else:
                self._note_transcribed()
                return events
        await self._note_dropped(last_error)
        return []

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
