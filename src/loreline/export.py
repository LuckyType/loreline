"""Transcript exporters: TXT, Markdown, SRT, VTT, JSON.

Each exporter renders a session's persisted :class:`TranscriptEvent` list into a
string. Speaker labels (when present) are surfaced; segments are assumed already
ordered by ``start_ts``.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence

from loreline.models import DIARIZE_SOURCE, REPROCESS_SOURCE_PREFIX, Session, TranscriptEvent


def canonical_transcript(events: Sequence[TranscriptEvent]) -> list[TranscriptEvent]:
    """Select the transcript view to show/export/summarize for a session.

    A session's stored rows can come from more than one source: the live
    capture (tagged by provider id), a post-session global re-diarization pass
    (tagged ``DIARIZE_SOURCE``), and alternate re-transcriptions kept for future
    comparison (tagged ``REPROCESS_SOURCE_PREFIX + provider_id``, see
    ``loreline.reprocess.jobs``). A re-diarization pass supersedes the live rows
    when present (it's a full relabeled copy of them); re-transcription
    alternates are never part of the canonical view - nothing merges or diffs
    them yet, so surfacing them here would just duplicate every segment.
    """
    diarized = [e for e in events if e.source == DIARIZE_SOURCE]
    if diarized:
        return diarized
    return [e for e in events if not e.source.startswith(REPROCESS_SOURCE_PREFIX)]


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
