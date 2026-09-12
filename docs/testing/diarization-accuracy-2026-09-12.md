# Diarization accuracy, measured 2026-09-12

The product review asked for diarization to be measured honestly rather than
described. This is that measurement. It needed no new jobs and no reference
corpus: the GM had already renamed the speakers on a real session, and those
renames are ground truth for what the machine should have produced.

Read off the live deployment through the API. Nothing was changed. The
speakers are pseudonymised here, since this repository is public and the
people at that table did not publish their names: Player A through Player D
are stable across every table below.

## The session

`e323e5bc`, 36.1 minutes, 150 transcript rows, 147 of them carrying a speaker
label, self hosted sherpa-onnx diarization, no `min_speakers` or `max_speakers`
set.

## The result

| Measure | Value |
| --- | --- |
| Labels the diarizer produced | 11 |
| Labels the GM bothered to rename | 7 |
| Real people behind those 7 | 4 |
| Over-clustering on the named part | 1.75x |
| Over-clustering counting every label | 2.75x |

Every one of the three main speakers was split, and the shape of each split is
the same: one dominant label plus a small satellite.

| Person | Rows | Labels |
| --- | --- | --- |
| Player A | 55 | Speaker 1 (46) and Speaker 3 (9) |
| Player B | 41 | Speaker 7 (40) and Speaker 8 (1) |
| Player C | 27 | Speaker 5 (16) and Speaker 11 (11) |
| Player D | 15 | Speaker 9 |
| unnamed | 9 total | Speakers 0, 2, 4 and 10, two or three rows each |

The four unnamed labels hold 9 rows between them, 6 percent of the transcript.
Nothing suggests a fifth or eleventh person was in the room; they read as
fragments of the same four.

## What this says

The failure is over-clustering, not collapse. ADR 0007 fixed the opposite
problem, where every call clustered alone and everyone became "Speaker 0", by
giving the service a session scoped speaker bank. That fix holds: labels are
stable across the session. What is left is that a short or acoustically odd
utterance earns its own cluster rather than joining the speaker it belongs to.

A one row satellite like Player B's Speaker 8 is the clearest case: a single
utterance whose embedding was noisy enough to miss its own speaker by whatever
margin the clustering threshold uses.

## The cheap fix nobody has tried

`DiarizationConfig` already carries `min_speakers` and `max_speakers`, and both
are surfaced in the diarize dialog. This session ran with neither set, so the
clustering was free to invent as many speakers as it liked. Bounding it is the
first thing to try, and it now has a source it did not have before: **a
campaign knows its cast.** The Aventurien campaign has three players recorded,
so a session in that campaign has a defensible default of roughly that many
speakers, plus the GM.

The proposal, small and testable: seed the diarize dialog's speaker bounds from
the campaign's cast size when the session belongs to a campaign, leaving them
editable, and re-run this session with them set. Then measure again against the
same renames. If the 11 labels collapse toward 4, the bounds are the fix and
the default is worth keeping. If they do not, the clustering threshold itself
is the problem and that is a change inside the diarization service.

## Also observed, not diarization

- `36583e39`, 289 minutes and 683 rows, carries no speaker labels at all. It
  has never been diarized, so it is not evidence either way, but it is the
  longest real session on the box and the obvious candidate for a second
  measurement.
- `95f12eed` has `ended_at: null` and `merged_from: []`, so the History page
  can show no duration for it. This is a merged session created during the
  2026-09-08 audit, before the fix for finding F-10 landed, so it is stale data
  rather than a live defect. A merge made today records both fields.
