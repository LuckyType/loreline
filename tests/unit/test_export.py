"""Unit tests for transcript exporters."""

from __future__ import annotations

import json

from loreline.export import EXPORTERS, relabel_speakers, to_json, to_srt, to_txt, to_vtt
from loreline.models import Session, SessionStatus, TranscriptEvent


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
