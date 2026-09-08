"""Transcript exporters: TXT, Markdown, SRT, VTT, JSON.

Each exporter renders a session's persisted :class:`TranscriptEvent` list into a
string. Speaker labels (when present) are surfaced; segments are assumed already
ordered by ``start_ts``.

TXT, Markdown and JSON render the transcript one row at a time, because that is
what they are: a record. SRT and VTT do not - a subtitle file is read by a
player, which has rules a transcript never had to keep (see
:func:`subtitle_cues`), so those two reshape the rows before writing them.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping, Sequence
from typing import NamedTuple

from loreline.models import (
    DIARIZE_SOURCE_PREFIX,
    GAP_SOURCE,
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


def has_version(events: Sequence[TranscriptEvent], version: str) -> bool:
    """Whether ``version`` is a version this session actually has.

    :func:`variant_view` answers "which rows are this version" and returns an
    empty list for a version nobody ever produced. That is the right answer for
    a reader that only wants rows, and the wrong one for a caller that has to
    tell "this version is empty" apart from "you named a version that does not
    exist" - export and summarize both do, because the alternative is a silent
    fallback to the original, which hands back a file (or bills a summary) that
    looks exactly like the one that was asked for and is not it.

    ``ORIGINAL_VERSION`` is always a version a session has, even with not one
    row stored: it is the live capture, not something a job produced, so a
    session that captured nothing still *has* an empty original.
    """
    if version == ORIGINAL_VERSION:
        return True
    tags = (f"{REPROCESS_SOURCE_PREFIX}{version}", f"{DIARIZE_SOURCE_PREFIX}{version}")
    return any(e.source in tags for e in events)


def canonical_transcript(events: Sequence[TranscriptEvent]) -> list[TranscriptEvent]:
    """The default transcript view for a session: the original version."""
    return variant_view(events, ORIGINAL_VERSION)


def final_rows(events: Sequence[TranscriptEvent]) -> list[TranscriptEvent]:
    """A version's settled text: no interims, no gap markers.

    The views above answer "which version is this", which is a question about
    ``source``. This answers a different one, "is this a thing somebody said",
    and the two are separate because the browser wants the whole version while
    it is being captured, and everything downstream of the capture wants only
    what settled.

    A running streaming session has both kinds of row in the table at once (see
    ``docs/adr/0006``): an interim, which is a guess that will be replaced, and
    a gap marker, which is the app saying it lost audio. Exporting either would
    put text nobody said into a file, summarizing either would feed it to a
    model, and a diarize job relabelling either would carry it into a new
    version and make it permanent. So each of the three reads through here.
    """
    return [e for e in events if e.is_final and e.source != GAP_SOURCE]


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


def _has_speakers(events: Sequence[TranscriptEvent]) -> bool:
    """Whether anything in this transcript carries a speaker at all.

    "Unknown" is the honest word for an unlabeled line in a transcript whose
    other lines are labeled: somebody said it and the diarizer could not say
    who. On a transcript that was never diarized it is a lie told once per
    line - a 683-line file in which every single line reports a failed
    identification that was never attempted - so the two prose formats drop the
    speaker entirely there, which is what the on-screen transcript already
    does. SRT and VTT never had the problem: they only ever prefixed a speaker
    they had.
    """
    return any(e.speaker for e in events)


def to_txt(session: Session, events: Sequence[TranscriptEvent]) -> str:
    """Plain text, one line per segment: ``[mm:ss] Speaker: text``.

    The speaker is dropped from every line of a wholly undiarized transcript;
    see :func:`_has_speakers` for why that is not the same as labelling them
    all "Unknown".
    """
    _ = session
    if _has_speakers(events):
        lines = [f"[{_clock(e.start_ts)}] {_speaker(e)}: {e.text}" for e in events]
    else:
        lines = [f"[{_clock(e.start_ts)}] {e.text}" for e in events]
    return "\n".join(lines) + ("\n" if lines else "")


def to_markdown(session: Session, events: Sequence[TranscriptEvent]) -> str:
    """Markdown with a header and per-speaker blockquotes.

    Same speaker rule as :func:`to_txt`: an undiarized transcript is timestamps
    and text, with nothing standing in for a speaker nobody ever looked for.
    """
    head = f"# Session {session.id}\n"
    if session.campaign_id:
        head += f"\n*Campaign:* {session.campaign_id}\n"
    if _has_speakers(events):
        body = [f"\n**{_speaker(e)}** ({_clock(e.start_ts)}): {e.text}" for e in events]
    else:
        body = [f"\n({_clock(e.start_ts)}) {e.text}" for e in events]
    return head + "".join(body) + "\n"


# Subtitle shaping, for SRT and VTT only. The numbers are the broadcast
# conventions rather than anything this app invented: roughly two lines of
# ~42 characters is what a player renders without overflowing its safe area,
# and about seven seconds is the longest anyone leaves a single cue up.
_MAX_CUE_CHARS = 84
_MAX_CUE_S = 7.0
# The floor a clamped cue is never allowed below. Only reached where two rows
# genuinely collide (see :func:`_lay_out`); short enough that a real one-word
# utterance is never stretched to reach it, and positive so no player is ever
# handed a cue that ends before it starts.
_MIN_CUE_S = 0.1
# A long speaker name must not eat the whole line budget and leave one word per
# cue.
_MIN_CUE_CHARS = 24
# How far outside its own row's span a word timing may sit before the whole
# alignment is treated as a different clock - see :func:`_word_spans`.
_WORD_SLACK_S = 0.5
_SENTENCE_ENDS = (".", "!", "?", "…")
# Trailing characters that close a sentence without ending it, so that
# `he said "run."` still reads as a sentence end. Written as escapes because
# the curly quotes among them are indistinguishable from the straight ones at
# a glance, and here the difference is the whole point.
_CLOSERS = "\"')]}\u00bb\u201d\u2019"


class Cue(NamedTuple):
    """One subtitle cue: a span a player shows, and the text it shows."""

    start: float
    end: float
    text: str


class _Token(NamedTuple):
    """One whitespace-separated word of a row: where it sits, and when."""

    char_start: int
    char_end: int
    start: float
    end: float


def _word_spans(event: TranscriptEvent) -> list[tuple[int, int, float, float]]:
    """Align ``event.words`` onto ``event.text``: (char range, time range) each.

    Only some providers return word timings at all, and a row that has them can
    still have text that does not match them (a provider that normalizes
    numbers, or a diarized copy rewritten after the fact). So this returns an
    empty list on the first word it cannot find in order, rather than a partial
    alignment: half a row timed by the provider and half by a guess would be
    indistinguishable downstream from a row timed properly throughout.
    """
    spans: list[tuple[int, int, float, float]] = []
    cursor = 0
    for word in event.words:
        token = word.text.strip()
        if not token:
            continue
        at = event.text.find(token, cursor)
        if at < 0:
            return []
        spans.append((at, at + len(token), word.start, word.end))
        cursor = at + len(token)
    if not spans:
        return []
    # Word clocks that do not sit inside the row's own span are not the same
    # clock - an absolute epoch, or a per-file offset that nothing rebased.
    # Interpolating over the row is the honest answer there, because the row's
    # own two timestamps are the only ones known to be session-relative.
    if spans[0][2] < event.start_ts - _WORD_SLACK_S or spans[-1][3] > event.end_ts + _WORD_SLACK_S:
        return []
    return spans


def _lerp(event: TranscriptEvent, offset: int, length: int) -> float:
    """Where a character offset falls in a row's span, read off proportionally.

    The fallback for rows with no usable word timings. It assumes an even
    speaking rate across the row, which nobody has; it is an estimate, and the
    only place it is used is a cue boundary *inside* a row - a row's own two
    endpoints always come from the row itself.
    """
    if length <= 0:
        return event.start_ts
    frac = min(max(offset / length, 0.0), 1.0)
    return event.start_ts + (event.end_ts - event.start_ts) * frac


def _tokens(event: TranscriptEvent) -> list[_Token]:
    """The row's words, each with a time span, from word timings where there are any."""
    spans = _word_spans(event)
    length = len(event.text)
    tokens: list[_Token] = []
    index = 0
    for match in re.finditer(r"\S+", event.text):
        start, end = match.start(), match.end()
        while index < len(spans) and spans[index][0] < start:
            index += 1
        last = index
        while last < len(spans) and spans[last][1] <= end:
            last += 1
        if last > index:
            tokens.append(_Token(start, end, spans[index][2], spans[last - 1][3]))
            index = last
        else:
            first, final = _lerp(event, start, length), _lerp(event, end, length)
            tokens.append(_Token(start, end, first, final))
    return tokens


def _ends_sentence(text: str) -> bool:
    return text.rstrip(_CLOSERS).endswith(_SENTENCE_ENDS)


def _pack(tokens: Sequence[_Token], text: str, budget: int) -> list[list[_Token]]:
    """Group a row's words into cue-sized runs.

    Greedy and forward-only: a run is closed when the next word would push it
    past ``budget`` characters or past ``_MAX_CUE_S``, and immediately when a
    word ends a sentence, because a cue break a reader was going to make anyway
    is the one that costs them nothing. A single word longer than the budget
    still gets its own cue rather than being cut - a subtitle file may reshape
    a transcript, it may not edit it.
    """
    runs: list[list[_Token]] = []
    current: list[_Token] = []
    for token in tokens:
        if current and (
            token.char_end - current[0].char_start > budget
            or token.end - current[0].start > _MAX_CUE_S
        ):
            runs.append(current)
            current = []
        current.append(token)
        if _ends_sentence(text[token.char_start : token.char_end]):
            runs.append(current)
            current = []
    if current:
        runs.append(current)
    return runs


def _cues_for(event: TranscriptEvent) -> list[Cue]:
    """One row's cues: itself when it already fits, its own words when it does not."""
    label = f"{event.speaker}: " if event.speaker else ""
    budget = max(_MAX_CUE_CHARS - len(label), _MIN_CUE_CHARS)
    runs = _pack(_tokens(event), event.text, budget)
    if len(runs) <= 1:
        # The overwhelmingly common case, and deliberately not reassembled from
        # tokens: the row's own text and its own two timestamps go through
        # untouched, so a transcript of ordinary-length turns exports exactly
        # as it always did.
        return [Cue(event.start_ts, event.end_ts, label + event.text)]
    return [
        Cue(run[0].start, run[-1].end, label + event.text[run[0].char_start : run[-1].char_end])
        for run in runs
    ]


def _lay_out(cues: Sequence[Cue]) -> list[Cue]:
    """Make a chronological, non-overlapping cue list out of transcript rows.

    A transcript may hold rows that overlap: endpointing revises a turn's start
    backwards, and diarization is free to say two people spoke at once. Both are
    true statements about a conversation and both are invalid in a subtitle
    file, where players variously drop one cue, stack them, or desync.

    So: cues are sorted by start, each cue's end is clamped to the next one's
    start, and a cue that would then be left with no duration at all - two rows
    that genuinely collide, same start or out of order - is given ``_MIN_CUE_S``
    and the cue after it is pushed to begin where it ends. Nothing is dropped
    and no text is cut; what gives is the timing, by a fraction of a second, and
    the timing is the only thing a subtitle file can trade.
    """
    ordered = sorted(cues, key=lambda c: (c.start, c.end))
    out: list[Cue] = []
    for i, cue in enumerate(ordered):
        start = max(cue.start, out[-1].end) if out else cue.start
        end = min(cue.end, ordered[i + 1].start) if i + 1 < len(ordered) else cue.end
        out.append(Cue(start, max(end, start + _MIN_CUE_S), cue.text))
    return out


def subtitle_cues(events: Sequence[TranscriptEvent]) -> list[Cue]:
    """Reshape transcript rows into cues a subtitle player can actually show.

    Two things separate a transcript from a subtitle file, and this does both.

    Rows may overlap; cues may not (see :func:`_lay_out`). And a row is one
    thing somebody said, which is routinely half a minute of speech and several
    hundred characters, while a cue is one thing a reader takes in before it is
    replaced. A 30-second paragraph is a perfectly good transcript line and an
    unreadable subtitle, so rows longer than ``_MAX_CUE_S`` or ``_MAX_CUE_CHARS``
    are split at sentence ends, and at word boundaries where a sentence runs
    longer than that.

    The limitation worth knowing: a split's *inner* timings are only as good as
    what the provider returned. Rows that carry word timings (Deepgram and
    AssemblyAI do; several batch endpoints return none) are cut on real word
    boundaries. Rows without them get their inner boundaries interpolated across
    the row at an assumed even speaking rate, which drifts within a long turn.
    The row's own first start and last end are never interpolated either way, so
    the error is bounded by the row and cannot accumulate down the file.
    """
    cues: list[Cue] = []
    for event in events:
        cues.extend(_cues_for(event))
    return _lay_out(cues)


def to_srt(session: Session, events: Sequence[TranscriptEvent]) -> str:
    """SubRip subtitles, reshaped into playable cues by :func:`subtitle_cues`."""
    _ = session
    blocks = [
        f"{i}\n{_ts(cue.start, ',')} --> {_ts(cue.end, ',')}\n{cue.text}\n"
        for i, cue in enumerate(subtitle_cues(events), start=1)
    ]
    return "\n".join(blocks)


def to_vtt(session: Session, events: Sequence[TranscriptEvent]) -> str:
    """WebVTT subtitles, reshaped into playable cues by :func:`subtitle_cues`."""
    _ = session
    blocks = ["WEBVTT\n"]
    blocks += [
        f"{_ts(cue.start, '.')} --> {_ts(cue.end, '.')}\n{cue.text}\n"
        for cue in subtitle_cues(events)
    ]
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
