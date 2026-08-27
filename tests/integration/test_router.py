"""Integration tests for the STT router (failover, diarization, compare)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from loreline.audio.chunker import Utterance
from loreline.bus import EventBus
from loreline.models import (
    DiarizationConfig,
    DiarizationMode,
    Protocol,
    ProviderConfig,
    ProviderKind,
    SpeakerSegment,
    TranscriptEvent,
    Word,
)
from loreline.stt.router import RouterConfig, SttRouter


def _config(provider_id: str) -> ProviderConfig:
    return ProviderConfig(
        id=provider_id,
        name=provider_id,
        kind=ProviderKind.OPENAI_COMPAT,
        protocol=Protocol.HTTP_BATCH,
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

    async def health(self) -> bool:
        return True

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


async def test_compare_fan_out() -> None:
    bus: EventBus[TranscriptEvent] = EventBus()
    router = SttRouter(FakeBackend("p1"), bus, RouterConfig(session_id="s1"))
    backends = {
        "p1": FakeBackend("p1", text="one"),
        "p2": FakeBackend("p2", text="two"),
        "bad": FailingBackend("bad"),
    }
    utterance = Utterance(pcm=b"\x01\x00" * 1600, start=0.0, end=1.0)
    result = await router.transcribe_compare(utterance, backends)
    assert result["p1"][0].text == "one"
    assert result["p2"][0].text == "two"
    assert result["bad"] == []
