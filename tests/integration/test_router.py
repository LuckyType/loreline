"""Integration tests for the STT router (failover, diarization)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping

import httpx
import pytest

from loreline.audio.chunker import Utterance
from loreline.bus import EventBus
from loreline.models import (
    DiarizationConfig,
    DiarizationMode,
    ProviderConfig,
    ProviderKind,
    SpeakerSegment,
    TranscriptEvent,
    Word,
)
from loreline.stt.router import ProvidersExhaustedError, RouterConfig, SttRouter


def _config(provider_id: str) -> ProviderConfig:
    return ProviderConfig(
        id=provider_id,
        name=provider_id,
        kind=ProviderKind.OPENAI_COMPAT,
    )


class FakeBackend:
    def __init__(
        self, provider_id: str, *, words: list[Word] | None = None, text: str = "ok"
    ) -> None:
        self.config = _config(provider_id)
        self._words = words or []
        self._text = text

    async def transcribe(
        self,
        audio: AsyncIterator[Utterance],
        *,
        session_id: str,
        glossary: object = None,
    ) -> AsyncIterator[TranscriptEvent]:
        _ = glossary
        async for utt in audio:
            yield TranscriptEvent(
                session_id=session_id,
                source=self.config.id,
                text=self._text,
                words=self._words,
                start_ts=utt.start,
                end_ts=utt.end,
                is_final=True,
            )

    async def aclose(self) -> None:
        return None


class FailingBackend(FakeBackend):
    async def transcribe(
        self,
        audio: AsyncIterator[Utterance],
        *,
        session_id: str,
        glossary: object = None,
    ) -> AsyncIterator[TranscriptEvent]:
        _ = (session_id, glossary)
        async for _utt in audio:
            msg = "backend unavailable"
            raise RuntimeError(msg)
        if False:  # pragma: no cover - marks this as an async generator
            yield TranscriptEvent(
                session_id=session_id, source=self.config.id, text="", start_ts=0.0, end_ts=0.0
            )


class FlakyBackend(FakeBackend):
    """Fails the first ``fail_first`` utterances, then transcribes normally."""

    def __init__(self, provider_id: str, *, fail_first: int) -> None:
        super().__init__(provider_id)
        self._remaining_failures = fail_first

    async def transcribe(
        self,
        audio: AsyncIterator[Utterance],
        *,
        session_id: str,
        glossary: object = None,
    ) -> AsyncIterator[TranscriptEvent]:
        _ = glossary
        async for utt in audio:
            if self._remaining_failures > 0:
                self._remaining_failures -= 1
                msg = "still flaky"
                raise RuntimeError(msg)
            yield TranscriptEvent(
                session_id=session_id,
                source=self.config.id,
                text="recovered",
                start_ts=utt.start,
                end_ts=utt.end,
                is_final=True,
            )


class FakeDiarizer:
    def __init__(self, segments: list[SpeakerSegment]) -> None:
        self._segments = segments
        self.calls = 0

    async def diarize(
        self,
        wav: bytes,
        *,
        sample_rate: int = 16000,
        min_speakers: int | None = None,
        max_speakers: int | None = None,
    ) -> list[SpeakerSegment]:
        _ = (wav, sample_rate, min_speakers, max_speakers)
        self.calls += 1
        return self._segments

    async def aclose(self) -> None:
        return None


async def _utterances() -> AsyncIterator[Utterance]:
    yield Utterance(pcm=b"\x01\x00" * 1600, start=0.0, end=1.0)


async def _collect(bus: EventBus[TranscriptEvent], count: int) -> list[TranscriptEvent]:
    received: list[TranscriptEvent] = []

    async def run() -> None:
        async with bus.subscribe() as stream:
            async for event in stream:
                received.append(event)
                if len(received) >= count:
                    return

    await asyncio.wait_for(run(), timeout=2.0)
    return received


async def test_router_publishes_primary_events() -> None:
    bus: EventBus[TranscriptEvent] = EventBus()
    router = SttRouter(FakeBackend("p1"), bus, RouterConfig(session_id="s1"))
    collector = asyncio.create_task(_collect(bus, 1))
    await asyncio.sleep(0.01)
    await router.run(_utterances())
    events = await collector
    assert len(events) == 1
    assert events[0].source == "p1"


async def test_router_falls_back_on_primary_error() -> None:
    bus: EventBus[TranscriptEvent] = EventBus()
    router = SttRouter(
        FailingBackend("primary"),
        bus,
        RouterConfig(session_id="s1"),
        fallback=FakeBackend("fallback"),
    )
    collector = asyncio.create_task(_collect(bus, 1))
    await asyncio.sleep(0.01)
    await router.run(_utterances())
    events = await collector
    assert len(events) == 1
    assert events[0].source == "fallback"


async def test_router_no_fallback_emits_nothing() -> None:
    bus: EventBus[TranscriptEvent] = EventBus()
    router = SttRouter(FailingBackend("primary"), bus, RouterConfig(session_id="s1"))
    published: list[TranscriptEvent] = []

    async def collect() -> None:
        async with bus.subscribe() as stream:
            async for event in stream:
                published.append(event)

    task = asyncio.create_task(collect())
    await asyncio.sleep(0.01)
    await router.run(_utterances())
    await asyncio.sleep(0.05)
    task.cancel()
    assert published == []


async def test_router_remote_diarization_merge() -> None:
    bus: EventBus[TranscriptEvent] = EventBus()
    words = [Word(text="hallo", start=0.1, end=0.4), Word(text="welt", start=0.6, end=0.9)]
    diarizer = FakeDiarizer(
        [
            SpeakerSegment(start=0.0, end=0.5, speaker="Speaker 0"),
            SpeakerSegment(start=0.5, end=1.0, speaker="Speaker 1"),
        ]
    )
    router = SttRouter(
        FakeBackend("p1", words=words),
        bus,
        RouterConfig(
            session_id="s1",
            diarization=DiarizationConfig(mode=DiarizationMode.REMOTE, endpoint="http://x"),
        ),
        diarizer=diarizer,
    )
    collector = asyncio.create_task(_collect(bus, 1))
    await asyncio.sleep(0.01)
    await router.run(_utterances())
    events = await collector
    assert diarizer.calls == 1
    assert events[0].words[0].speaker == "Speaker 0"
    assert events[0].words[1].speaker == "Speaker 1"


async def test_router_remote_diarization_merge_mid_session() -> None:
    """The diarizer only sees one utterance's isolated audio, so its segments are
    utterance-relative (0-based) regardless of where in the session the utterance
    falls; the router must shift them by ``utterance.start`` before overlapping
    them against word times, which already carry that offset.
    """
    bus: EventBus[TranscriptEvent] = EventBus()
    words = [Word(text="hallo", start=100.1, end=100.4), Word(text="welt", start=100.6, end=100.9)]
    diarizer = FakeDiarizer(
        [
            SpeakerSegment(start=0.0, end=0.5, speaker="Speaker 0"),
            SpeakerSegment(start=0.5, end=1.0, speaker="Speaker 1"),
        ]
    )
    router = SttRouter(
        FakeBackend("p1", words=words),
        bus,
        RouterConfig(
            session_id="s1",
            diarization=DiarizationConfig(mode=DiarizationMode.REMOTE, endpoint="http://x"),
        ),
        diarizer=diarizer,
    )

    async def _mid_session_utterance() -> AsyncIterator[Utterance]:
        yield Utterance(pcm=b"\x01\x00" * 1600, start=100.0, end=101.0)

    collector = asyncio.create_task(_collect(bus, 1))
    await asyncio.sleep(0.01)
    await router.run(_mid_session_utterance())
    events = await collector
    assert events[0].words[0].speaker == "Speaker 0"
    assert events[0].words[1].speaker == "Speaker 1"


async def test_router_inline_diarization_sets_event_speaker() -> None:
    bus: EventBus[TranscriptEvent] = EventBus()
    words = [
        Word(text="a", start=0.0, end=0.6, speaker="Speaker A"),
        Word(text="b", start=0.6, end=0.8, speaker="Speaker B"),
    ]
    router = SttRouter(
        FakeBackend("p1", words=words),
        bus,
        RouterConfig(session_id="s1", diarization=DiarizationConfig(mode=DiarizationMode.INLINE)),
    )
    collector = asyncio.create_task(_collect(bus, 1))
    await asyncio.sleep(0.01)
    await router.run(_utterances())
    events = await collector
    assert events[0].speaker == "Speaker A"  # dominant by duration


async def _n_utterances(n: int) -> AsyncIterator[Utterance]:
    for i in range(n):
        yield Utterance(pcm=b"\x01\x00" * 1600, start=float(i), end=i + 1.0)


async def test_router_degraded_after_failure_streak_alerts_once() -> None:
    """A streak of utterances that transcribe nothing marks the router degraded
    and notifies exactly once - not per failure, which floods alert channels
    when a backend stays down for hours."""
    bus: EventBus[TranscriptEvent] = EventBus()
    alerts: list[str] = []

    async def on_failover(message: str) -> None:
        alerts.append(message)

    router = SttRouter(
        FailingBackend("primary"), bus, RouterConfig(session_id="s1"), on_failover=on_failover
    )
    await router.run(_n_utterances(2))
    assert router.degraded_since is None  # under the streak threshold
    assert alerts == []

    await router.run(_n_utterances(3))
    assert router.degraded_since is not None
    assert len(alerts) == 1


async def test_router_recovers_when_transcription_resumes() -> None:
    bus: EventBus[TranscriptEvent] = EventBus()
    router = SttRouter(FlakyBackend("primary", fail_first=3), bus, RouterConfig(session_id="s1"))
    collector = asyncio.create_task(_collect(bus, 1))
    await asyncio.sleep(0.01)
    await router.run(_n_utterances(4))
    events = await collector
    assert events[0].text == "recovered"
    assert router.degraded_since is None  # streak ended by the 4th utterance


async def test_router_fallback_success_is_not_degraded() -> None:
    """A working fallback keeps the transcript flowing, so the session is not
    degraded no matter how long the primary keeps failing."""
    bus: EventBus[TranscriptEvent] = EventBus()
    alerts: list[str] = []

    async def on_failover(message: str) -> None:
        alerts.append(message)

    router = SttRouter(
        FailingBackend("primary"),
        bus,
        RouterConfig(session_id="s1"),
        fallback=FakeBackend("fallback"),
        on_failover=on_failover,
    )
    collector = asyncio.create_task(_collect(bus, 4))
    await asyncio.sleep(0.01)
    await router.run(_n_utterances(4))
    events = await collector
    assert all(e.source == "fallback" for e in events)
    assert router.degraded_since is None
    assert alerts == []


# --- terminal provider failures --------------------------------------------

# The two 429s a provider can answer with, verbatim. Same status, opposite
# handling: the first can only be fixed on the billing page, the second fixes
# itself in a moment. See loreline.health.classify_request_error.
_NO_CREDITS = {
    "error": {
        "message": (
            "You have no credits remaining. Add credits to continue using the API at "
            "https://platform.openai.com/settings/organization/billing/."
        ),
        "type": "insufficient_quota",
        "code": "insufficient_quota",
    }
}
_RATE_LIMITED = {
    "error": {
        "message": (
            "Rate limit reached for gpt-4o-transcribe in organization org-x on requests "
            "per min (RPM): Limit 500, Used 500, Requested 1. Please try again in 120ms."
        ),
        "type": "requests",
        "code": "rate_limit_exceeded",
    }
}


class RejectingBackend(FakeBackend):
    """Answers every request with one fixed HTTP error, and counts the calls.

    Raised the way the batch connectors do, body and all: the router has to
    read that body, because the status code alone cannot tell an exhausted
    account from a busy minute.
    """

    def __init__(self, provider_id: str, *, status: int, body: Mapping[str, object]) -> None:
        super().__init__(provider_id)
        self._status = status
        self._body = body
        self.calls = 0

    async def transcribe(
        self,
        audio: AsyncIterator[Utterance],
        *,
        session_id: str,
        glossary: object = None,
    ) -> AsyncIterator[TranscriptEvent]:
        _ = glossary
        async for _utt in audio:
            self.calls += 1
            request = httpx.Request("POST", "https://api.example.test/v1/audio/transcriptions")
            response = httpx.Response(self._status, json=self._body, request=request)
            raise httpx.HTTPStatusError(
                f"{self._status} from {request.url}", request=request, response=response
            )
        if False:  # pragma: no cover - marks this as an async generator
            yield TranscriptEvent(
                session_id=session_id, source=self.config.id, text="", start_ts=0.0, end_ts=0.0
            )


async def test_router_stops_when_the_only_provider_is_out_of_credit() -> None:
    """The reported bug: a terminal 429 used to be retried for every utterance,
    so the run finished "successfully" with an empty transcript after two doomed
    API calls per utterance. It must stop instead, once, with the vendor's own
    sentence for the GM."""
    bus: EventBus[TranscriptEvent] = EventBus()
    primary = RejectingBackend("primary", status=429, body=_NO_CREDITS)
    router = SttRouter(primary, bus, RouterConfig(session_id="s1"))

    with pytest.raises(ProvidersExhaustedError) as caught:
        await router.run(_n_utterances(5))

    assert "no credits remaining" in str(caught.value)
    assert str(caught.value).startswith("primary: ")  # the provider is named
    assert primary.calls == 1  # not once per utterance
    assert router.terminal_error is not None


async def test_router_keeps_going_when_the_provider_is_merely_rate_limited() -> None:
    """The same status, and it must not stop the run: a throttle passes, and
    aborting a healthy session over one would be worse than the bug above."""
    bus: EventBus[TranscriptEvent] = EventBus()
    primary = RejectingBackend("primary", status=429, body=_RATE_LIMITED)
    router = SttRouter(primary, bus, RouterConfig(session_id="s1"))

    await router.run(_n_utterances(5))  # no exception

    assert primary.calls == 5  # still tried every utterance
    assert router.terminal_error is None
    assert router.degraded_since is not None  # nothing transcribed, so still degraded


async def test_router_fails_over_to_a_fallback_before_giving_up() -> None:
    """A dead primary is exactly when the fallback earns its keep: it is usually
    a different vendor, with a different bill. The primary is asked once and
    then left alone, and the session keeps transcribing."""
    bus: EventBus[TranscriptEvent] = EventBus()
    primary = RejectingBackend("primary", status=401, body={"error": {"message": "Invalid key"}})
    router = SttRouter(
        primary,
        bus,
        RouterConfig(session_id="s1"),
        fallback=FakeBackend("fallback"),
    )
    collector = asyncio.create_task(_collect(bus, 3))
    await asyncio.sleep(0.01)
    await router.run(_n_utterances(3))
    events = await collector

    assert [e.source for e in events] == ["fallback"] * 3
    assert primary.calls == 1
    assert router.terminal_error is None  # a working provider remains
    assert router.degraded_since is None


async def test_router_reports_every_dead_provider_when_both_are_gone() -> None:
    bus: EventBus[TranscriptEvent] = EventBus()
    primary = RejectingBackend("primary", status=429, body=_NO_CREDITS)
    fallback = RejectingBackend("fallback", status=401, body={"error": {"message": "Invalid key"}})
    router = SttRouter(primary, bus, RouterConfig(session_id="s1"), fallback=fallback)

    with pytest.raises(ProvidersExhaustedError) as caught:
        await router.run(_n_utterances(4))

    message = str(caught.value)
    assert "primary: You have no credits remaining." in message
    assert "fallback: Invalid key" in message
    assert (primary.calls, fallback.calls) == (1, 1)
