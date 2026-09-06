"""Unit tests for diarization merge logic."""

from __future__ import annotations

from loreline.diarization import assign_speakers, segments_from_words
from loreline.models import SpeakerSegment, TranscriptEvent, Word


def _word(text: str, start: float, end: float, speaker: str | None = None) -> Word:
    return Word(text=text, start=start, end=end, speaker=speaker)


def test_segments_from_words_collapses_runs() -> None:
    words = [
        _word("a", 0.0, 0.5, "Speaker A"),
        _word("b", 0.5, 1.0, "Speaker A"),
        _word("c", 1.0, 1.5, "Speaker B"),
    ]
    segments = segments_from_words(words)
    assert len(segments) == 2
    assert segments[0].speaker == "Speaker A"
    assert segments[0].start == 0.0
    assert segments[0].end == 1.0
    assert segments[1].speaker == "Speaker B"


def test_segments_from_words_ignores_unlabelled() -> None:
    assert segments_from_words([_word("a", 0.0, 0.5)]) == []


def test_assign_speakers_by_overlap() -> None:
    event = TranscriptEvent(
        session_id="s1",
        source="p1",
        text="hallo welt",
        words=[_word("hallo", 0.0, 0.4), _word("welt", 0.6, 1.0)],
        start_ts=0.0,
        end_ts=1.0,
        is_final=True,
    )
    segments = [
        SpeakerSegment(start=0.0, end=0.5, speaker="Speaker 0"),
        SpeakerSegment(start=0.5, end=1.0, speaker="Speaker 1"),
    ]
    merged = assign_speakers(event, segments)
    assert merged.words[0].speaker == "Speaker 0"
    assert merged.words[1].speaker == "Speaker 1"
    # dominant speaker: equal duration -> first by max() stability; just ensure set.
    assert merged.speaker in {"Speaker 0", "Speaker 1"}


def test_assign_speakers_noop_without_segments() -> None:
    event = TranscriptEvent(
        session_id="s1", source="p1", text="x", start_ts=0.0, end_ts=1.0, is_final=True
    )
    assert assign_speakers(event, []) is event


def test_assign_speakers_event_level_without_words() -> None:
    event = TranscriptEvent(
        session_id="s1", source="p1", text="x", start_ts=0.2, end_ts=0.8, is_final=True
    )
    segments = [SpeakerSegment(start=0.0, end=1.0, speaker="Speaker 3")]
    merged = assign_speakers(event, segments)
    assert merged.speaker == "Speaker 3"


def test_assign_speakers_keeps_event_speaker_when_no_word_overlaps() -> None:
    """Words that all fall in a gap the diarizer heard as silence.

    Every word comes back unattributed, and the event used to take that None as
    its own speaker, wiping a label the audio had already been asked about.
    It now falls back to the segment covering most of the event's span.
    """
    event = TranscriptEvent(
        session_id="s1",
        source="p1",
        text="hm",
        words=[_word("hm", 5.0, 5.2)],
        speaker="Speaker 9",
        start_ts=5.0,
        end_ts=5.2,
        is_final=True,
    )
    segments = [SpeakerSegment(start=0.0, end=1.0, speaker="Speaker 3")]
    merged = assign_speakers(event, segments)
    assert merged.words[0].speaker is None  # no segment covers it, so nothing is invented
    assert merged.speaker == "Speaker 9"  # what it already carried, not None


def test_assign_speakers_event_takes_the_dominant_segment_over_its_span() -> None:
    """No words and two segments: the one holding most of the event wins."""
    event = TranscriptEvent(
        session_id="s1", source="p1", text="x", start_ts=0.0, end_ts=1.0, is_final=True
    )
    segments = [
        SpeakerSegment(start=0.0, end=0.2, speaker="Speaker 0"),
        SpeakerSegment(start=0.2, end=1.0, speaker="Speaker 1"),
    ]
    assert assign_speakers(event, segments).speaker == "Speaker 1"
