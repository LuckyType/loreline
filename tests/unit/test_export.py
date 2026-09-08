"""Unit tests for transcript exporters."""

from __future__ import annotations

import json
import re

from loreline.export import (
    EXPORTERS,
    canonical_transcript,
    final_rows,
    has_version,
    relabel_speakers,
    subtitle_cues,
    to_json,
    to_markdown,
    to_srt,
    to_txt,
    to_vtt,
)
from loreline.models import (
    DIARIZE_SOURCE_PREFIX,
    GAP_SOURCE,
    ORIGINAL_VERSION,
    REPROCESS_SOURCE_PREFIX,
    Session,
    SessionStatus,
    TranscriptEvent,
    Word,
)


def _session() -> Session:
    return Session(id="s1", status=SessionStatus.COMPLETED, started_at=0.0, campaign_id="camp")


def _events() -> list[TranscriptEvent]:
    return [
        TranscriptEvent(
            session_id="s1",
            source="p",
            text="hello there",
            speaker="A",
            start_ts=1.0,
            end_ts=2.5,
            is_final=True,
        ),
        TranscriptEvent(
            session_id="s1",
            source="p",
            text="general kenobi",
            start_ts=3.0,
            end_ts=4.0,
            is_final=True,
        ),
    ]


def test_relabel_speakers_applies_map() -> None:
    relabeled = relabel_speakers(_events(), {"A": "GM"})
    assert relabeled[0].speaker == "GM"  # mapped label
    assert relabeled[1].speaker is None  # unlabeled segment untouched
    assert relabel_speakers(_events(), {}) == _events()  # empty map = unchanged
    # Names flow through exports:
    assert "GM: hello there" in to_txt(_session(), relabeled)


def test_txt_lines() -> None:
    out = to_txt(_session(), _events())
    assert "[00:01] A: hello there" in out
    assert "[00:03] Unknown: general kenobi" in out


def test_srt_timestamps_use_comma() -> None:
    out = to_srt(_session(), _events())
    assert "00:00:01,000 --> 00:00:02,500" in out
    assert "A: hello there" in out


def test_vtt_header_and_dot_timestamps() -> None:
    out = to_vtt(_session(), _events())
    assert out.startswith("WEBVTT")
    assert "00:00:03.000 --> 00:00:04.000" in out


def test_json_roundtrip() -> None:
    out = to_json(_session(), _events())
    payload = json.loads(out)
    assert payload["session"]["id"] == "s1"
    assert len(payload["transcript"]) == 2


def test_all_formats_registered() -> None:
    assert set(EXPORTERS) == {"txt", "md", "srt", "vtt", "json"}
    for render, media_type, ext in EXPORTERS.values():
        body = render(_session(), _events())
        assert isinstance(body, str)
        assert media_type
        assert ext


def test_final_rows_keeps_only_what_somebody_said() -> None:
    """A running streaming session has three kinds of row in the table at once.

    Interims are guesses about to be replaced and gap markers are the app
    saying it lost audio; exporting, summarizing or diarizing either would make
    text nobody said permanent (see ``docs/adr/0006``).
    """
    rows = [
        TranscriptEvent(
            session_id="s1",
            source="oai",
            text="the goblin runs",
            start_ts=1.0,
            end_ts=2.0,
            is_final=True,
            turn_id="oai:1:a",
        ),
        TranscriptEvent(
            session_id="s1",
            source="oai",
            text="and then the",
            start_ts=3.0,
            end_ts=3.4,
            is_final=False,
            turn_id="oai:1:b",
        ),
        TranscriptEvent(
            session_id="s1",
            source=GAP_SOURCE,
            text="4s of audio was not transcribed",
            start_ts=5.0,
            end_ts=9.0,
            is_final=True,
        ),
    ]

    assert [e.text for e in final_rows(rows)] == ["the goblin runs"]
    # The live views keep all three: the browser is what the marker is for.
    assert len(canonical_transcript(rows)) == 3


def _row(text: str, start: float, end: float, speaker: str | None = None) -> TranscriptEvent:
    return TranscriptEvent(
        session_id="s1",
        source="p",
        text=text,
        speaker=speaker,
        start_ts=start,
        end_ts=end,
        is_final=True,
    )


def _cue_spans(subtitles: str) -> list[tuple[str, str]]:
    """Every ``start --> end`` pair in an SRT or VTT body, in file order."""
    return re.findall(r"([\d:,.]+) --> ([\d:,.]+)", subtitles)


def test_has_version_tells_an_empty_version_from_a_missing_one() -> None:
    """Export and summarize have to refuse an id nobody produced, and a
    fallback to the original would hand back a plausible wrong file instead."""
    events = [
        _row("live", 0.0, 1.0),
        TranscriptEvent(
            session_id="s1",
            source=f"{REPROCESS_SOURCE_PREFIX}job1",
            text="re-run",
            start_ts=0.0,
            end_ts=1.0,
            is_final=True,
        ),
        TranscriptEvent(
            session_id="s1",
            source=f"{DIARIZE_SOURCE_PREFIX}job2",
            text="relabeled",
            start_ts=0.0,
            end_ts=1.0,
            is_final=True,
        ),
    ]
    assert has_version(events, ORIGINAL_VERSION)
    assert has_version([], ORIGINAL_VERSION)  # a session that captured nothing still has one
    assert has_version(events, "job1")
    assert has_version(events, "job2")  # reachable through its diarized copy alone
    assert not has_version(events, "job3")


def test_subtitle_cues_never_overlap() -> None:
    """Endpointing revises a turn's start backwards, so stored rows overlap by
    a few milliseconds. That is a fine transcript and an invalid subtitle
    file - players drop a cue, stack them, or desync."""
    events = [_row("first", 1.0, 3.365), _row("second", 3.356, 6.0)]

    cues = subtitle_cues(events)
    assert [c.text for c in cues] == ["first", "second"]
    assert abs(cues[0].end - 3.356) < 1e-9  # clamped to the next cue's start
    assert abs(cues[1].end - 6.0) < 1e-9  # the last cue keeps its own end

    for fmt in (to_srt(_session(), events), to_vtt(_session(), events)):
        spans = _cue_spans(fmt)
        assert len(spans) == 2
        assert spans[0][1] == spans[1][0]  # touching, never overlapping


def test_colliding_rows_are_pushed_apart_rather_than_dropped() -> None:
    """Two rows that start at the same instant (diarization is free to say two
    people spoke at once) leave no room to clamp into. The later cue is pushed
    to begin where the earlier one ends and both keep their text."""
    events = [_row("at once", 2.0, 5.0, "A"), _row("and so am I", 2.0, 5.0, "B")]

    cues = subtitle_cues(events)
    assert [c.text for c in cues] == ["A: at once", "B: and so am I"]
    assert cues[0].end > cues[0].start
    assert cues[1].start >= cues[0].end


def test_a_long_turn_is_split_into_readable_cues() -> None:
    """A 30-second paragraph is a perfectly good transcript row and an
    unreadable subtitle; nothing may be cut to make it fit."""
    sentence = "Der Drache landet krachend auf dem Turm."
    event = _row(" ".join([sentence] * 12), 0.0, 30.1)

    cues = subtitle_cues([event])
    assert len(cues) > 1
    assert all(len(c.text) <= 84 for c in cues)
    assert all(c.end - c.start <= 7.0 + 1e-6 for c in cues)
    assert all(c.end > c.start for c in cues)
    assert " ".join(c.text for c in cues) == event.text  # every word survives


def test_a_long_unpunctuated_turn_is_split_on_duration_too() -> None:
    """No sentence to break at, so the character and duration limits are all
    there is - and the duration limit is the one a wall of text hits first."""
    event = _row(" ".join(["wort"] * 200), 0.0, 40.0)

    cues = subtitle_cues([event])
    assert all(c.end - c.start <= 7.0 + 1e-6 for c in cues)
    assert " ".join(c.text for c in cues) == event.text


def test_word_timings_time_the_split_where_a_row_carries_them() -> None:
    """Only some providers return word timings. Where they are there, a cue
    boundary lands on the word's own clock instead of an interpolated guess."""
    words = [Word(text=f"w{i}", start=float(i), end=float(i) + 0.5) for i in range(20)]
    event = TranscriptEvent(
        session_id="s1",
        source="p",
        text=" ".join(w.text for w in words),
        words=words,
        start_ts=0.0,
        end_ts=20.0,
        is_final=True,
    )

    cues = subtitle_cues([event])
    assert len(cues) > 1
    # The split is driven by _MAX_CUE_S, and the boundary is a real word edge:
    # the second cue starts exactly where some word does.
    assert cues[1].start in {w.start for w in words}


def test_short_rows_still_export_exactly_as_they_did() -> None:
    """The overwhelmingly common row fits a cue already, and must go through
    untouched - its own text, its own two timestamps."""
    out = to_srt(_session(), _events())
    assert "1\n00:00:01,000 --> 00:00:02,500\nA: hello there\n" in out


def test_undiarized_transcript_omits_the_speaker() -> None:
    """ "Unknown" on every line of a 683-line file reads as 683 failed
    identifications rather than "nobody ever ran a diarizer here"."""
    events = [_row("hello there", 1.0, 2.5), _row("general kenobi", 3.0, 4.0)]

    txt = to_txt(_session(), events)
    assert "Unknown" not in txt
    assert "[00:01] hello there" in txt

    md = to_markdown(_session(), events)
    assert "Unknown" not in md
    assert "(00:01) hello there" in md


def test_partly_diarized_transcript_keeps_unknown() -> None:
    """Mixed is the case "Unknown" is honest for: somebody said the line and
    the diarizer could not say who."""
    events = [_row("hello there", 1.0, 2.5, "A"), _row("general kenobi", 3.0, 4.0)]

    assert "[00:03] Unknown: general kenobi" in to_txt(_session(), events)
    assert "**Unknown** (00:03): general kenobi" in to_markdown(_session(), events)
