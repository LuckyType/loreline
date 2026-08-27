"""Merge diarization segments onto transcript words by time overlap."""

from __future__ import annotations

from loreline.models import SpeakerSegment, TranscriptEvent, Word


def segments_from_words(words: list[Word]) -> list[SpeakerSegment]:
    """Collapse consecutive same-speaker words into speaker segments.

    Used for inline diarization, where the STT backend already attached a
    speaker to each word.
    """
    segments: list[SpeakerSegment] = []
    for word in words:
        if word.speaker is None:
            continue
        if segments and segments[-1].speaker == word.speaker:
            segments[-1] = segments[-1].model_copy(update={"end": word.end})
        else:
            segments.append(SpeakerSegment(start=word.start, end=word.end, speaker=word.speaker))
    return segments


def _overlap(start: float, end: float, segment: SpeakerSegment) -> float:
    return max(0.0, min(end, segment.end) - max(start, segment.start))


def _speaker_for(start: float, end: float, segments: list[SpeakerSegment]) -> str | None:
    best: str | None = None
    best_overlap = 0.0
    for segment in segments:
        overlap = _overlap(start, end, segment)
        if overlap > best_overlap:
            best_overlap = overlap
            best = segment.speaker
    return best


def assign_speakers(event: TranscriptEvent, segments: list[SpeakerSegment]) -> TranscriptEvent:
    """Return a copy of ``event`` with speakers assigned from ``segments``.

    Each word is labelled with the speaker of the maximally overlapping
    segment. The event-level speaker is set to the dominant word speaker.
    """
    if not segments:
        return event

    labelled: list[Word] = []
    for word in event.words:
        speaker = _speaker_for(word.start, word.end, segments) or word.speaker
        labelled.append(word.model_copy(update={"speaker": speaker}))

    event_speaker = _dominant_speaker(labelled)
    if not labelled:
        event_speaker = _speaker_for(event.start_ts, event.end_ts, segments) or event.speaker
    return event.model_copy(update={"words": labelled, "speaker": event_speaker})


def _dominant_speaker(words: list[Word]) -> str | None:
    durations: dict[str, float] = {}
    for word in words:
        if word.speaker is None:
            continue
        durations[word.speaker] = durations.get(word.speaker, 0.0) + max(0.0, word.end - word.start)
    if not durations:
        return None
    return max(durations, key=lambda spk: durations[spk])
