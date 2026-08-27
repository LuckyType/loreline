"""Tests for domain-model helpers."""

from __future__ import annotations

from loreline.models import TranscriptEvent, Word, rebase_transcript


def test_rebase_transcript_shifts_to_origin() -> None:
    event = TranscriptEvent(
        session_id="s",
        source="p",
        text="hi",
        start_ts=1000.5,
        end_ts=1002.0,
        words=[Word(text="hi", start=1000.5, end=1001.0)],
        is_final=True,
    )
    rebased = rebase_transcript(event, 1000.0)
    assert abs(rebased.start_ts - 0.5) < 1e-9
    assert abs(rebased.end_ts - 2.0) < 1e-9
    assert abs(rebased.words[0].start - 0.5) < 1e-9
    assert abs(rebased.words[0].end - 1.0) < 1e-9
    assert event.start_ts == 1000.5  # original untouched


def test_rebase_transcript_zero_offset_is_noop() -> None:
    event = TranscriptEvent(
        session_id="s", source="p", text="hi", start_ts=5.0, end_ts=6.0, is_final=True
    )
    assert rebase_transcript(event, 0.0) is event
