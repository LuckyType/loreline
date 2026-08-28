"""Transcript exporters: TXT, Markdown, SRT, VTT, JSON.

Each exporter renders a session's persisted :class:`TranscriptEvent` list into a
string. Speaker labels (when present) are surfaced; segments are assumed already
ordered by ``start_ts``.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence

from loreline.models import (
    DIARIZE_SOURCE_PREFIX,
    ORIGINAL_VERSION,
    REPROCESS_SOURCE_PREFIX,
    Session,
    TranscriptEvent,
)


def variant_rows(events: Sequence[TranscriptEvent], version: str) -> list[TranscriptEvent]:
    """A transcript version's raw rows, ignoring any diarized relabeling.

    ``ORIGINAL_VERSION`` selects the live capture (every untagged row - with
    failover a session's live rows can carry more than one provider id); any
    other version id selects that re-transcription job's rows.
    """
    if version == ORIGINAL_VERSION:
        return [
            e
            for e in events
            if not e.source.startswith((REPROCESS_SOURCE_PREFIX, DIARIZE_SOURCE_PREFIX))
        ]
    return [e for e in events if e.source == f"{REPROCESS_SOURCE_PREFIX}{version}"]


def variant_view(
    events: Sequence[TranscriptEvent], version: str = ORIGINAL_VERSION
) -> list[TranscriptEvent]:
    """Select one transcript version to show/export/summarize.

    A session's rows exist in versions - the live capture and one per
    re-transcription job (see ``loreline.reprocess.jobs``). A diarization pass
    over a version stores a fully relabeled copy of its rows (tagged
    ``DIARIZE_SOURCE_PREFIX + version``) which supersedes the raw rows here;
    other versions never leak in, so nothing is duplicated.
    """
    diarized = [e for e in events if e.source == f"{DIARIZE_SOURCE_PREFIX}{version}"]
    if diarized:
        return diarized
    return variant_rows(events, version)


def canonical_transcript(events: Sequence[TranscriptEvent]) -> list[TranscriptEvent]:
    """The default transcript view for a session: the original version."""
    return variant_view(events, ORIGINAL_VERSION)


def relabel_speakers(
    events: Sequence[TranscriptEvent], names: Mapping[str, str]
) -> list[TranscriptEvent]:
    """Apply a ``{label: display name}`` map to events' speaker labels (copy)."""
    if not names:
        return list(events)
    return [
        event.model_copy(update={"speaker": names.get(event.speaker, event.speaker)})
        if event.speaker
        else event
        for event in events
    ]


def _speaker(event: TranscriptEvent) -> str:
    return event.speaker or "Unknown"


def to_txt(session: Session, events: Sequence[TranscriptEvent]) -> str:
    """Plain text, one line per segment: ``[mm:ss] Speaker: text``."""
    _ = session
    lines = [f"[{_clock(e.start_ts)}] {_speaker(e)}: {e.text}" for e in events]
    return "\n".join(lines) + ("\n" if lines else "")


def to_markdown(session: Session, events: Sequence[TranscriptEvent]) -> str:
    """Markdown with a header and per-speaker blockquotes."""
    head = f"# Session {session.id}\n"
    if session.campaign_id:
        head += f"\n*Campaign:* {session.campaign_id}\n"
    body = [f"\n**{_speaker(e)}** ({_clock(e.start_ts)}): {e.text}" for e in events]
    return head + "".join(body) + "\n"


def to_srt(session: Session, events: Sequence[TranscriptEvent]) -> str:
    """SubRip subtitles."""
    _ = session
    blocks: list[str] = []
    for i, e in enumerate(events, start=1):
        text = f"{_speaker(e)}: {e.text}" if e.speaker else e.text
        blocks.append(f"{i}\n{_ts(e.start_ts, ',')} --> {_ts(e.end_ts, ',')}\n{text}\n")
    return "\n".join(blocks)


def to_vtt(session: Session, events: Sequence[TranscriptEvent]) -> str:
    """WebVTT subtitles."""
    _ = session
    blocks = ["WEBVTT\n"]
    for e in events:
        text = f"{_speaker(e)}: {e.text}" if e.speaker else e.text
        blocks.append(f"{_ts(e.start_ts, '.')} --> {_ts(e.end_ts, '.')}\n{text}\n")
    return "\n".join(blocks)


def to_json(session: Session, events: Sequence[TranscriptEvent]) -> str:
    """Structured JSON: full session + transcript."""
    payload = {
        "session": session.model_dump(mode="json"),
        "transcript": [e.model_dump(mode="json") for e in events],
    }
    return json.dumps(payload, indent=2)


def _clock(seconds: float) -> str:
    total = int(max(seconds, 0.0))
    return f"{total // 60:02d}:{total % 60:02d}"


def _ts(seconds: float, frac_sep: str) -> str:
    seconds = max(seconds, 0.0)
    ms = round(seconds * 1000)
    hours, ms = divmod(ms, 3_600_000)
    minutes, ms = divmod(ms, 60_000)
    secs, ms = divmod(ms, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}{frac_sep}{ms:03d}"


Exporter = Callable[[Session, "Sequence[TranscriptEvent]"], str]

EXPORTERS: dict[str, tuple[Exporter, str, str]] = {
    "txt": (to_txt, "text/plain; charset=utf-8", "txt"),
    "md": (to_markdown, "text/markdown; charset=utf-8", "md"),
    "srt": (to_srt, "application/x-subrip; charset=utf-8", "srt"),
    "vtt": (to_vtt, "text/vtt; charset=utf-8", "vtt"),
    "json": (to_json, "application/json", "json"),
}
