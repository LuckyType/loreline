"""Mock xAI live STT WebSocket server, in both of the shapes the real one has.

Which one a connection gets is decided the way the real endpoint decides it, by
the query string, because that is the whole configuration of an xAI socket:
``interim_results=true`` asks for a continuous stream and its absence asks for
one locked answer. Both greet with ``transcript.created`` as soon as the socket
is up, which is what the health probe reads.

**One utterance per connection** (no ``interim_results``), what
``XaiBackend.transcribe_one`` speaks: accept binary PCM, then on
``{"type":"audio.done"}`` emit the locked ``transcript.partial`` events for the
utterance followed by one ``transcript.done`` carrying the WHOLE transcript, and
close. That last part is the thing worth mocking. The partials and the final
event overlap completely, so a connector that accumulated both would emit each
word twice; the text below is built so that doubling is visible in an assertion
rather than plausible, and per-word speaker labels are attached so that inline
diarization is exercised at the same time.

**A continuous stream** (``interim_results=true``), what
``XaiBackend.open_stream`` speaks: decide the turns from the audio itself and
narrate them the way the documented events do. Interims roughly every 500 ms of
speech, a locked chunk (``is_final=true``, ``speech_final=false``) roughly every
three seconds of it, and ``speech_final=true`` once the audio has been quiet for
the ``endpointing`` window the query string asked for. ``finalize`` closes the
open turn at once and leaves the socket up; ``audio.done`` closes it, sends the
session's ``transcript.done`` and hangs up. The turn rule is a loudness
threshold rather than a VAD model, which is enough to let a test drive turns
with loud and quiet frames and deterministic in a way a model would not be.

Each event's ``text`` is the open turn's transcript so far and its ``words``
carry offsets from the start of the stream, which is the reading of the docs
``xai.py`` is written to; the other reading, where ``text`` accumulates across
turns, is covered by ``tests/unit/test_xai_stream_frames.py`` instead, since one
server cannot be both at once.

Deterministic throughout, so tests can assert wiring without the real API.

Docs: https://docs.x.ai/developers/model-capabilities/audio/speech-to-text
"""

from __future__ import annotations

import array
import json
from typing import cast
from urllib.parse import parse_qs, urlparse

from websockets.asyncio.server import ServerConnection

_SAMPLE_RATE = 16000

# The vendor's default endpointing window, used when the query string names
# none: silence this long closes a turn.
_ENDPOINTING_MS = 400.0
# One word of transcript per this much voiced audio.
_WORD_MS = 200.0
# An event per this much voiced audio, matching the documented "~500 ms".
_INTERIM_MS = 500.0
# ...and every so often that event is a locked chunk instead of an interim,
# matching the documented "~3s of speech finalized".
_CHUNK_MS = 3000.0
# Mean absolute sample value at or above which a block counts as speech. Well
# clear of the near-silent filler a test sends between turns, and well under a
# normal speaking level.
_SPEECH_LEVEL = 500

_WORDS = ("grok", "hears", "the", "party", "argue", "about", "the", "map")


async def xai_handler(websocket: ServerConnection) -> None:
    """Handle one mock xAI connection, in whichever shape it asked for."""
    await websocket.send(json.dumps({"type": "transcript.created"}))
    query = _query(websocket)
    if _flag(query, "interim_results"):
        await _stream_session(websocket, query)
        return
    await _utterance_session(websocket)


# -- one utterance per connection -----------------------------------------


async def _utterance_session(websocket: ServerConnection) -> None:
    """Collect one utterance and answer ``audio.done`` with its transcript."""
    total_bytes = 0
    async for message in websocket:
        if isinstance(message, bytes):
            total_bytes += len(message)
            continue
        # "finalize" is the other documented control message; this shape never
        # sends it, and neither branch of the real service ends the session on
        # it, so it is ignored here too.
        if _kind(message) == "audio.done":
            if total_bytes:
                await _send(websocket, _flush_frames(total_bytes // 2))
            return


def _flush_frames(samples: int) -> list[dict[str, object]]:
    """One utterance's flush: two locked partials, then the whole transcript."""
    duration = round(samples / _SAMPLE_RATE, 3)
    quarter = duration / 4
    first_words: list[dict[str, object]] = [
        {"text": "xai", "start": 0.0, "end": quarter, "speaker": 0},
        {"text": "mock", "start": quarter, "end": quarter * 2, "speaker": 0},
    ]
    second_words: list[dict[str, object]] = [
        {"text": str(samples), "start": quarter * 2, "end": quarter * 3, "speaker": 1},
        {"text": "samples", "start": quarter * 3, "end": duration, "speaker": 1},
    ]
    return [
        _locked("xai mock", first_words, start=0.0, duration=quarter * 2),
        _locked(f"{samples} samples", second_words, start=quarter * 2, duration=quarter * 2),
        {
            "type": "transcript.done",
            "text": f"xai mock {samples} samples",
            "words": first_words + second_words,
            "duration": duration,
        },
    ]


def _locked(
    text: str, words: list[dict[str, object]], *, start: float, duration: float
) -> dict[str, object]:
    """A locked partial: is_final true, because interim results are off.

    ``speech_final`` marks the end of the utterance rather than of a three
    second chunk, so only the last one carries it.
    """
    return {
        "type": "transcript.partial",
        "text": text,
        "words": words,
        "is_final": True,
        "speech_final": start > 0,
        "start": start,
        "duration": duration,
    }


# -- one continuous stream ------------------------------------------------


class _Stream:
    """The mock's turn machinery: loudness in, the documented events out."""

    def __init__(self, *, rate: int, endpointing_ms: float) -> None:
        self._rate = rate
        self._endpointing_ms = endpointing_ms
        self._ms = 0.0  # audio received on this connection, in milliseconds
        self._silence_ms = 0.0
        self._start_ms: float | None = None  # where the open turn began
        self._words: list[dict[str, object]] = []
        self._since_word = 0.0
        self._since_emit = 0.0
        self._since_lock = 0.0
        self._settled: list[str] = []  # closed turns, for transcript.done

    def feed(self, pcm: bytes) -> list[dict[str, object]]:
        """One binary frame; the events the real service would have sent."""
        block_ms = len(pcm) / 2 / self._rate * 1000
        events: list[dict[str, object]] = []
        if _loud(pcm):
            events.extend(self._speech(block_ms))
        elif self._start_ms is not None:
            self._silence_ms += block_ms
            if self._silence_ms >= self._endpointing_ms:
                events.extend(self.close())
        self._ms += block_ms
        return events

    def _speech(self, block_ms: float) -> list[dict[str, object]]:
        """A voiced block: grow the open turn, and say so now and then."""
        self._silence_ms = 0.0
        if self._start_ms is None:
            self._start_ms = self._ms
        self._since_word += block_ms
        while self._since_word >= _WORD_MS:
            self._since_word -= _WORD_MS
            self._add_word()
        self._since_emit += block_ms
        self._since_lock += block_ms
        if self._since_emit < _INTERIM_MS or not self._words:
            return []
        self._since_emit = 0.0
        locked = self._since_lock >= _CHUNK_MS
        if locked:
            self._since_lock = 0.0
        return [self._partial(is_final=locked, speech_final=False)]

    def close(self) -> list[dict[str, object]]:
        """End the open turn as ``speech_final``, if there is one with text."""
        if self._start_ms is None:
            return []
        events: list[dict[str, object]] = []
        if self._words:
            event = self._partial(is_final=True, speech_final=True)
            self._settled.append(str(event["text"]))
            events.append(event)
        self._start_ms = None
        self._words = []
        self._since_word = self._since_emit = self._since_lock = self._silence_ms = 0.0
        return events

    def done(self) -> dict[str, object]:
        """The whole stream's transcript, which is what audio.done answers."""
        return {
            "type": "transcript.done",
            "text": " ".join(self._settled),
            "words": [],
            "duration": round(self._ms / 1000, 3),
        }

    def _add_word(self) -> None:
        index = len(self._words)
        start = ((self._start_ms or 0.0) + index * _WORD_MS) / 1000
        self._words.append(
            {
                "text": _WORDS[index % len(_WORDS)],
                "start": round(start, 3),
                "end": round(start + _WORD_MS / 1000, 3),
                # Alternating, so a turn exercises inline diarization the way
                # a table with more than one person at it would.
                "speaker": index % 2,
            }
        )

    def _partial(self, *, is_final: bool, speech_final: bool) -> dict[str, object]:
        start = (self._start_ms or 0.0) / 1000
        last = self._words[-1]["end"] if self._words else start
        end = last if isinstance(last, float) else start
        return {
            "type": "transcript.partial",
            "text": " ".join(str(word["text"]) for word in self._words),
            "words": [dict(word) for word in self._words],
            "is_final": is_final or speech_final,
            "speech_final": speech_final,
            "start": round(start, 3),
            "duration": round(end - start, 3),
        }


async def _stream_session(websocket: ServerConnection, query: dict[str, list[str]]) -> None:
    """Serve one persistent connection until the client says the audio ended."""
    stream = _Stream(
        rate=int(_number(query, "sample_rate", _SAMPLE_RATE)),
        endpointing_ms=_number(query, "endpointing", _ENDPOINTING_MS),
    )
    async for message in websocket:
        if isinstance(message, bytes):
            await _send(websocket, stream.feed(message))
            continue
        kind = _kind(message)
        if kind == "finalize":
            await _send(websocket, stream.close())
        elif kind == "audio.done":
            await _send(websocket, [*stream.close(), stream.done()])
            return


def _loud(pcm: bytes) -> bool:
    """Whether a block is speech, by mean absolute sample value."""
    samples = array.array("h")
    samples.frombytes(pcm[: len(pcm) // 2 * 2])
    return bool(samples) and sum(abs(s) for s in samples) / len(samples) >= _SPEECH_LEVEL


# -- reading the client ----------------------------------------------------


def _query(websocket: ServerConnection) -> dict[str, list[str]]:
    """The connection's query string, which is its whole configuration."""
    request = websocket.request
    return parse_qs(urlparse(request.path).query) if request is not None else {}


def _flag(query: dict[str, list[str]], name: str) -> bool:
    return query.get(name, ["false"])[0].lower() == "true"


def _number(query: dict[str, list[str]], name: str, default: float) -> float:
    try:
        return float(query.get(name, [""])[0])
    except ValueError:
        return default


def _kind(message: str | bytes) -> str:
    """A control message's type, lowercased: the docs spell finalize both ways."""
    data: object = json.loads(message)
    if not isinstance(data, dict):
        return ""
    kind = cast("dict[str, object]", data).get("type")
    return kind.lower() if isinstance(kind, str) else ""


async def _send(websocket: ServerConnection, events: list[dict[str, object]]) -> None:
    for event in events:
        await websocket.send(json.dumps(event))
