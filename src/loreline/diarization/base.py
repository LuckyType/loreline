"""Diarization provider protocol and no-op implementation."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from loreline.models import SpeakerSegment


@runtime_checkable
class DiarizationProvider(Protocol):
    """Produces speaker segments for a chunk of mono PCM/WAV audio."""

    async def diarize(
        self,
        wav: bytes,
        *,
        sample_rate: int = 16000,
        min_speakers: int | None = None,
        max_speakers: int | None = None,
    ) -> list[SpeakerSegment]:
        """Return speaker segments covering ``wav`` (times in seconds)."""
        ...

    async def aclose(self) -> None:
        """Release any held resources."""
        ...


class NoopDiarizer:
    """Diarizer that produces no segments (mode ``none``)."""

    async def diarize(
        self,
        wav: bytes,
        *,
        sample_rate: int = 16000,
        min_speakers: int | None = None,
        max_speakers: int | None = None,
    ) -> list[SpeakerSegment]:
        _ = (wav, sample_rate, min_speakers, max_speakers)
        return []

    async def aclose(self) -> None:
        return None
