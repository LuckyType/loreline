#!/usr/bin/env -S uv run python
"""Run one real vendor's streaming connector against paced real speech.

The mock proves the wiring; this is the only thing that proves the feature.
What a live capture actually costs a GM is the time between somebody speaking
and their words appearing, and that number exists only where a real model is
listening to real speech arriving at the speed a microphone delivers it. So
this feeds a public-domain LibriVox clip through the streaming path at wall
clock, one 20 ms frame at a time with a real sleep between them, and reports:

* **time to first interim**, from the moment the speaker started (the vendor's
  own turn start, on the capture clock) to the first text on screen. This is
  the number the whole feature is about, and it has no counterpart on the
  utterance path, which shows nothing until a turn is over.
* **time to final**, from the moment the speaker *stopped* to the settled text.
  Compare against the utterance path's floor, which is 800 ms of trailing
  silence before the vendor is even asked, plus the round trip.
* the same two again for a vendor that reports **no offsets at all**, where
  both of the above are circular: the stream dates such a turn from the frame
  in flight when the vendor first mentioned it, so "from the turn's start" is
  zero by construction. Those two are measured from the clip's own speech
  instead (see :class:`_Speech`) and from the turn's newest interim, which is
  the closest thing to when the vendor thought the speaker had stopped.

Never burst the audio in: a paced feed is not politeness, it is a correctness
condition. At least one vendor's turn machinery desynchronizes when audio
arrives faster than real time (see `gemini_live.py`), and every vendor's
endpointing is timing out against silence it is measuring itself.

    scripts/stream_check.py --db ~/.loreline-test/loreline.db \\
        --secrets ~/.loreline-test/secrets.json --kind openai --model gpt-live-transcribe

The credentials come from a SecretStore file and a copy of a deployment's
database, both outside the repo, and nothing here prints or stores a key.
"""

from __future__ import annotations

import argparse
import array
import asyncio
import json
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from loreline.models import ProviderConfig, ProviderKind, TranscriptEvent
from loreline.secrets import SecretStore
from loreline.stt.registry import create_backend
from loreline.stt.streaming import StreamConfig, TranscriptStream, is_streaming

# A LibriVox recording in the public domain, picked through the metadata API so
# only the item id has to be right. Any clear single-speaker reading does; the
# point is real speech with real pauses, which is what endpointing keys off.
_ARCHIVE_ITEM = "heartofamystery_2005_librivox"
_METADATA_URL = "https://archive.org/metadata/{item}"
_DOWNLOAD_URL = "https://archive.org/download/{item}/{name}"
# What Silero accepts, which is what a real capture runs at, so the resampler
# under the connector is exercised exactly as it is in a session.
_CAPTURE_RATE = 16000
_FRAME_MS = 20
_FRAME_BYTES = _CAPTURE_RATE * _FRAME_MS // 1000 * 2
# Mean absolute sample value above which a frame counts as speech. The liveness
# watchdog reads it, and so does _Speech below, so a threshold is enough and
# Silero is not worth the model download here.
_SPEECH_LEVEL = 300
# Quiet this long ends a span of local speech. Longer than the pauses inside a
# sentence, shorter than the ones between two people's turns.
_HANGOVER_S = 0.5


@dataclass
class _Speech:
    """When the clip's own audio was voiced, on the capture clock.

    Only needed for a vendor that reports no offsets at all: for one that does,
    the turn's own ``start_ts`` already says when the speaker started, and "time
    to first interim" is measured from it. Gemini Live states nothing, so the
    stream dates a turn from the frame that was in flight when the first partial
    arrived, and measuring against that would answer zero to the one question
    the whole feature is about. The threshold here is not Google's endpointing
    and is not meant to be: it is a second opinion on when somebody started
    talking, which is what makes a latency number mean something.
    """

    spans: list[list[float]] = field(default_factory=list[list[float]])
    _quiet_since: float | None = None

    def feed(self, ts: float, *, voiced: bool) -> None:
        if voiced:
            self._quiet_since = None
            if not self.spans or self.spans[-1][1] > 0:
                self.spans.append([ts, 0.0])
            return
        if self.spans and self.spans[-1][1] == 0:
            self._quiet_since = self._quiet_since or ts
            if ts - self._quiet_since >= _HANGOVER_S:
                self.spans[-1][1] = self._quiet_since

    def around(self, ts: float) -> tuple[float, float] | None:
        """The span of local speech a turn dated ``ts`` belongs to.

        The last one that began at or before it, since a vendor's turn is always
        reported after the speech that produced it.
        """
        earlier = [s for s in self.spans if s[0] <= ts]
        return (earlier[-1][0], earlier[-1][1] or ts) if earlier else None


@dataclass
class _Turn:
    """One vendor turn, and when each thing about it reached us."""

    turn_id: str
    start_ts: float
    end_ts: float = 0.0
    text: str = ""
    first_interim_at: float | None = None
    # The newest interim, which is where the vendor had got to just before it
    # settled: for one that reports no end offset, the distance from here to
    # the final is the closest thing to "how long the text took to settle".
    last_interim_at: float | None = None
    final_at: float | None = None
    revisions: int = 0


@dataclass
class _Recorder:
    """The publish callback, timing every event against the capture clock."""

    turns: dict[str, _Turn] = field(default_factory=dict[str, "_Turn"])
    gaps: list[TranscriptEvent] = field(default_factory=list["TranscriptEvent"])

    async def __call__(self, event: TranscriptEvent) -> None:
        now = time.monotonic()
        if event.source == "gap":
            self.gaps.append(event)
            return
        key = event.turn_id or f"{event.start_ts}"
        turn = self.turns.setdefault(key, _Turn(turn_id=key, start_ts=event.start_ts))
        turn.revisions += 1
        turn.text = event.text
        turn.end_ts = event.end_ts
        if event.is_final:
            turn.final_at = now
        else:
            turn.last_interim_at = now
            if turn.first_interim_at is None:
                turn.first_interim_at = now


def _clip(seconds: float, cache: Path) -> bytes:
    """The test clip as mono s16le PCM, downloading and trimming it once."""
    pcm_path = cache / f"clip-{int(seconds)}s-{_CAPTURE_RATE}.pcm"
    if pcm_path.exists():
        return pcm_path.read_bytes()
    if shutil.which("ffmpeg") is None:
        msg = "ffmpeg is needed to trim and convert the clip"
        raise SystemExit(msg)
    source = cache / "source.mp3"
    if not source.exists():
        cache.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(
            _METADATA_URL.format(item=_ARCHIVE_ITEM), timeout=30
        ) as response:
            files = json.load(response).get("files", [])
        name = next(f["name"] for f in files if str(f["name"]).endswith("_64kb.mp3"))
        print(f"downloading {name} from archive.org ({_ARCHIVE_ITEM})")
        urllib.request.urlretrieve(_DOWNLOAD_URL.format(item=_ARCHIVE_ITEM, name=name), source)
    # -ss 30 skips the LibriVox announcement, which is not the speech we want
    # to time: it is read at a different pace and its pauses are not a table's.
    subprocess.run(
        [
            "ffmpeg", "-nostdin", "-y", "-loglevel", "error",
            "-ss", "30", "-t", str(seconds), "-i", str(source),
            "-ac", "1", "-ar", str(_CAPTURE_RATE), "-f", "s16le", str(pcm_path),
        ],
        check=True,
    )  # fmt: skip
    return pcm_path.read_bytes()


def _provider(db: Path | None, kind: str, auth_ref: str | None, language: str) -> ProviderConfig:
    """The provider row to run as: read from a database, or named directly.

    A deployment's database is the honest source, because it carries the row's
    base URL, rate and language alongside its credential reference, and running
    against a row nobody configured proves less. ``--auth-ref`` is the way in
    without one, for a key that exists in a ``SecretStore`` but whose row does
    not: everything else then takes this app's defaults, which is what a fresh
    row would have anyway.

    The database is opened read-only on purpose: it is somebody's real file,
    copied here for its provider rows, and running migrations over it would be
    a change nobody asked for.
    """
    if db is None:
        if auth_ref is None:
            msg = "pass either --db or --auth-ref"
            raise SystemExit(msg)
        return ProviderConfig(
            id=auth_ref.removeprefix("provider:"),
            name=f"{kind} (stream-check)",
            kind=ProviderKind(kind),
            auth_ref=auth_ref,
            language=language,
        )
    with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM providers WHERE kind = ? AND enabled = 1 LIMIT 1;", (kind,)
        ).fetchone()
    if row is None:
        msg = f"no enabled provider of kind {kind!r} in {db}"
        raise SystemExit(msg)
    return ProviderConfig(
        id=row["id"],
        name=row["name"],
        kind=ProviderKind(row["kind"]),
        base_url=row["base_url"],
        auth_ref=row["auth_ref"],
        sample_rate=row["sample_rate"],
        language=row["language"],
    )


def _is_speech(frame: bytes) -> bool:
    """Whether a frame is voiced, for the liveness watchdog and nothing else."""
    samples = array.array("h")
    samples.frombytes(frame)
    return bool(samples) and sum(abs(s) for s in samples) / len(samples) >= _SPEECH_LEVEL


async def _run(args: argparse.Namespace) -> int:
    pcm = _clip(args.seconds, args.cache)
    config = _provider(args.db, args.kind, args.auth_ref, args.language)
    backend = create_backend(config, SecretStore(args.secrets), args.model)
    if not is_streaming(backend):
        print(f"{config.kind.value}/{args.model} has no streaming shape yet")
        return 1

    recorder = _Recorder()
    stream = TranscriptStream(
        backend,
        publish=recorder,
        capture_rate=_CAPTURE_RATE,
        config=StreamConfig(session_id="stream-check"),
    )
    task = asyncio.create_task(stream.run())
    frames = [pcm[i : i + _FRAME_BYTES] for i in range(0, len(pcm) - _FRAME_BYTES, _FRAME_BYTES)]
    print(f"feeding {len(frames)} frames ({len(frames) * _FRAME_MS / 1000:.0f}s) at wall clock")

    started = time.monotonic()
    speech = _Speech()
    for index, frame in enumerate(frames):
        now = time.monotonic()
        voiced = _is_speech(frame)
        speech.feed(now, voiced=voiced)
        stream.feed(frame, now, is_speech=voiced)
        # Against the run's own start, not the previous frame: sleeping a fixed
        # interval per frame drifts late by whatever each iteration costs, and
        # over a minute that is a feed slower than real time.
        await asyncio.sleep(max(0.0, started + (index + 1) * _FRAME_MS / 1000 - now))
    stream.stop()
    outcome = await task
    await backend.aclose()

    _report(recorder, outcome, started, speech)
    return 0


def _report(recorder: _Recorder, outcome: str, started: float, speech: _Speech) -> None:
    print(f"\nstream ended: {outcome}; {len(recorder.turns)} turns, {len(recorder.gaps)} gaps\n")
    interims: list[float] = []
    finals: list[float] = []
    heard: list[float] = []
    settled: list[float] = []
    for turn in sorted(recorder.turns.values(), key=lambda t: t.start_ts):
        to_interim = turn.first_interim_at - turn.start_ts if turn.first_interim_at else None
        to_final = turn.final_at - turn.end_ts if turn.final_at else None
        # ...and the same two against the clip's own speech, for a vendor whose
        # turns carry no offsets and are therefore dated from our own frames.
        span = speech.around(turn.start_ts)
        after_onset = turn.first_interim_at - span[0] if span and turn.first_interim_at else None
        last, final = turn.last_interim_at, turn.final_at
        after_last = final - last if final and last else None
        for value, bucket in (
            (to_interim, interims),
            (to_final, finals),
            (after_onset, heard),
            (after_last, settled),
        ):
            if value is not None:
                bucket.append(value)
        print(
            f"[{turn.start_ts - started:6.2f}s -> {turn.end_ts - started:6.2f}s] "
            f"interim {_ms(to_interim)}  final {_ms(to_final)}  "
            f"(after onset {_ms(after_onset)}, settled {_ms(after_last)}, "
            f"{turn.revisions} revisions, {turn.end_ts - turn.start_ts:.1f}s long)"
            f"\n    {turn.text}"
        )
    for gap in recorder.gaps:
        print(f"[{gap.start_ts - started:6.2f}s] GAP: {gap.text}")
    print(
        f"\nmedian time to first interim: {_ms(_median(interims))}"
        f"\nmedian time to final:         {_ms(_median(finals))}"
        f"\n...from local speech onset:   {_ms(_median(heard))}"
        f"\nmedian final after last interim: {_ms(_median(settled))}"
        "\n(the utterance path's floor is 800 ms of trailing silence plus a round trip;"
        "\n the last two are what a vendor reporting no offsets can be measured by)"
    )


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[len(ordered) // 2]


def _ms(seconds: float | None) -> str:
    return "-" if seconds is None else f"{seconds * 1000:5.0f}ms"


def _path(value: str) -> Path:
    return Path(value).expanduser()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    # Expanded here rather than inside the run: the paths are settings, and a
    # coroutine that touches the filesystem for them is a coroutine doing
    # blocking I/O on the event loop.
    parser.add_argument("--db", type=_path, help="a loreline database with a provider row")
    parser.add_argument("--auth-ref", help="a SecretStore key, instead of a database row")
    parser.add_argument("--language", default="en", help="the language the clip is read in")
    parser.add_argument("--secrets", required=True, type=_path, help="a SecretStore JSON file")
    parser.add_argument("--kind", default="openai", help="provider kind to use")
    parser.add_argument("--model", required=True, help="the model to stream with")
    parser.add_argument("--seconds", type=float, default=60.0, help="how much speech to feed")
    parser.add_argument("--cache", type=_path, default="~/.cache/loreline-stream-check")
    return asyncio.run(_run(parser.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
