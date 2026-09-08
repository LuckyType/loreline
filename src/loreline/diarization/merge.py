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
    """The speaker whose segments cover the most of ``[start, end)`` in total.

    Sums every segment's overlap per speaker, the same aggregation
    ``_dominant_speaker`` applies to words, rather than taking whichever single
    segment overlaps most: a speaker heard across three short segments can hold
    more of the span than one heard in a single longer segment, and comparing
    segments one at a time instead of by speaker used to hand the turn to
    whichever segment happened to be longest.
    """
    totals: dict[str, float] = {}
    for segment in segments:
        overlap = _overlap(start, end, segment)
        if overlap <= 0.0:
            continue
        totals[segment.speaker] = totals.get(segment.speaker, 0.0) + overlap
    if not totals:
        return None
    return max(totals, key=lambda spk: totals[spk])


def assign_speakers(event: TranscriptEvent, segments: list[SpeakerSegment]) -> TranscriptEvent:
    """Return a copy of ``event`` with speakers assigned from ``segments``.

    Each word is labelled with the speaker of the maximally overlapping
    segment. The event's own speaker is the one that holds most of its words,
    else the segment covering most of the event's span, else whatever it
    already carried.

    That order is what makes this work for every connector rather than only
    for the ones that return words. The OpenAI Realtime and Gemini Live
    sessions return none at all, and a connector that does return them can still have every one
    of them fall in a gap the diarizer heard as silence; in both cases the
    event is still one voiced stretch of audio with a speaker, and taking the
    dominant segment over its span says who it was. Answering None there, which
    is what a dominant word speaker of None used to write over the event, threw
    away a speaker the audio had already been asked about.
    """
    if not segments:
        return event

    labelled: list[Word] = []
    for word in event.words:
        speaker = _speaker_for(word.start, word.end, segments) or word.speaker
        labelled.append(word.model_copy(update={"speaker": speaker}))

    event_speaker = (
        _dominant_speaker(labelled)
        or _speaker_for(event.start_ts, event.end_ts, segments)
        or event.speaker
    )
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
