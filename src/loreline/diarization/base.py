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
        session_id: str | None = None,
    ) -> list[SpeakerSegment]:
        """Return speaker segments covering ``wav`` (times in seconds).

        ``session_id`` names the run these seconds belong to, so a diarizer
        that can remember voices returns labels that mean the same person for
        every call of that run. A caller that has no such id, and a diarizer
        that cannot remember, both still work: the labels are then only
        comparable within one call.
        """
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
        session_id: str | None = None,
    ) -> list[SpeakerSegment]:
        _ = (wav, sample_rate, min_speakers, max_speakers, session_id)
        return []

    async def aclose(self) -> None:
        return None
