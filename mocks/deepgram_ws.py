"""Mock Deepgram live WebSocket server, in both of the shapes it serves.

The real endpoint has one protocol and two ways of being used, and it tells
them apart by the query string, so this does too: ``interim_results=true`` is
what the streaming connector asks for and what nothing else does.

**One utterance per connection** (no ``interim_results``), what
``DeepgramBackend.transcribe_one`` does: accept binary linear16 PCM, then a
``{"type":"CloseStream"}`` that finalizes everything buffered. Like the real
service, one utterance is flushed as SEVERAL final ``Results`` frames (an empty
near-silent lead-in first, then the content splits, with per-word speaker
labels), followed by ``Metadata`` and a clean close. Connectors must accumulate
the finals, not take the first.

**A live stream** (``interim_results=true``), what the streaming shape does:
one connection for a whole session, with the turns decided here rather than by
the caller. Endpointing is a loudness threshold rather than a VAD model, which
is enough to let a test drive turns by handing it loud and quiet frames and
deterministic in a way a model would not be. It narrates a turn the way the
service does: growing interim ``Results`` for the segment in hand, an
``is_final`` frame per segment (so one turn is several of them, the way real
endpointing splits it), then a final carrying ``speech_final`` and an
``UtteranceEnd`` behind it. ``Finalize`` is answered with ``from_finalize``,
``KeepAlive`` is accepted and silent, and ``diarize=true`` puts a speaker on
every word, one per turn, so a test can ask whether labels stay consistent
across a whole connection.

Two details are copied from a recorded run rather than from the reference,
because they are what a connector gets wrong. Segment offsets are *contiguous*,
so a turn's first segment begins where the previous turn's audio ended and
carries the silence between them. And the ``UtteranceEnd`` that follows a
``speech_final`` arrives *late*, after the next turn's first interim, carrying
the closed turn's own last word end rather than the ``-1`` the documentation
defines for it. Four of four did that in 150 seconds of real speech, and a
connector that acts on one ends a turn that has settled nothing.

Deterministic throughout, so tests can assert wiring without the real API.
"""

from __future__ import annotations

import array
import json
from typing import cast
from urllib.parse import parse_qs, urlparse

from websockets.asyncio.server import ServerConnection

# What the socket is fed when no sample_rate says otherwise.
_DEFAULT_RATE = 16000
# Mean absolute sample value at or above which a block counts as speech. Well
# clear of the near-silent filler a test sends between turns, and well under a
# normal speaking level.
_SPEECH_LEVEL = 500
# Silence that ends a turn, matching the connector's `endpointing` request.
_SILENCE_S = 0.5
# One interim per this much speech, so a turn produces several of them.
_INTERIM_S = 0.2
# Speech after which the current segment is finalized and a new one starts,
# without the turn ending: this is what makes one turn several `is_final`
# frames, which is the part of the real protocol a connector gets wrong.
_SEGMENT_S = 1.0

_WORDS = ("the", "goblin", "takes", "the", "chest", "and", "runs", "for", "the", "door")


async def deepgram_handler(websocket: ServerConnection) -> None:
    """Handle one mock Deepgram connection, in whichever shape it asked for."""
    params = _query(websocket)
    if _flag(params, "interim_results"):
        await _live(websocket, params)
        return
    await _one_utterance(websocket)


def _query(websocket: ServerConnection) -> dict[str, list[str]]:
    request = websocket.request
    return parse_qs(urlparse(request.path).query) if request is not None else {}


def _flag(params: dict[str, list[str]], name: str) -> bool:
    return params.get(name, ["false"])[0] == "true"


# --------------------------------------------------------------------------
# One utterance per connection: unchanged, and still what the batch shape gets.
# --------------------------------------------------------------------------


async def _one_utterance(websocket: ServerConnection) -> None:
    """Accept one utterance and flush it on ``CloseStream``."""
    total_bytes = 0
    async for message in websocket:
        if isinstance(message, bytes):
            total_bytes += len(message)
            continue
        data: object = json.loads(message)
        kind = cast("dict[str, object]", data).get("type") if isinstance(data, dict) else None
        if kind == "CloseStream":
            if total_bytes:
                for frame in _flush_frames(total_bytes // 2):
                    await websocket.send(json.dumps(frame))
            await websocket.send(json.dumps({"type": "Metadata", "duration": total_bytes / 32000}))
            return


def _flush_frames(samples: int) -> list[dict[str, object]]:
    """One utterance's flush: a silent lead-in, then the content splits."""
    duration = round(samples / 16000, 3)
    lead_in = _results("", [])
    first = _results(
        "deepgram mock",
        [
            {
                "word": "deepgram",
                "punctuated_word": "Deepgram",
                "start": 0.0,
                "end": duration / 2,
                "confidence": 0.95,
                "speaker": 0,
            }
        ],
    )
    last = _results(
        f"{samples} samples",
        [
            {
                "word": "mock",
                "punctuated_word": "mock",
                "start": duration / 2,
                "end": duration,
                "confidence": 0.9,
                "speaker": 1,
            }
        ],
    )
    return [lead_in, first, last]


def _results(transcript: str, words: list[dict[str, object]]) -> dict[str, object]:
    return {
        "type": "Results",
        "is_final": True,
        "channel": {
            "alternatives": [{"transcript": transcript, "confidence": 0.95, "words": words}]
        },
    }


# --------------------------------------------------------------------------
# A live stream: one connection, the mock's own endpointing, many turns.
# --------------------------------------------------------------------------


class _Live:
    """The mock's turn machinery: loudness in, narrated ``Results`` out.

    Every offset it states is seconds of audio received on this connection,
    which is what the real service counts in and what the connector's clock
    mapping is written against.
    """

    def __init__(self, *, rate: int, diarize: bool) -> None:
        self._rate = rate
        self._diarize = diarize
        self._at = 0.0  # audio received, in seconds
        self._speaking = False
        self._turns = 0
        self._silence = 0.0
        self._since_interim = 0.0
        self._since_segment = 0.0
        self._cursor = 0  # index into _WORDS, so no two turns read alike
        self._segment_start = 0.0
        self._words: list[dict[str, object]] = []
        self._late_end: float | None = None  # an UtteranceEnd owed to a closed turn
        self._last_word_end = 0.0  # what that owed UtteranceEnd will report

    @property
    def seconds(self) -> float:
        """Audio received on this connection, for the closing ``Metadata``."""
        return round(self._at, 4)

    def feed(self, pcm: bytes) -> list[dict[str, object]]:
        """One block of audio; the messages the real service would have sent."""
        block = len(pcm) / 2 / self._rate
        self._at += block
        if _loud(pcm):
            return self._voiced(block)
        if not self._speaking:
            return []
        self._silence += block
        return self.end_turn() if self._silence >= _SILENCE_S else []

    def _voiced(self, block: float) -> list[dict[str, object]]:
        out: list[dict[str, object]] = []
        self._silence = 0.0
        if not self._speaking:
            # The segment is already open: it began where the last one ended,
            # and the silence since then belongs to it, as the service does it.
            self._speaking = True
            self._turns += 1
        self._since_interim += block
        self._since_segment += block
        if self._since_interim >= _INTERIM_S:
            self._since_interim = 0.0
            self._add_word()
            out.append(self.results(is_final=False))
            out.extend(self._owed_utterance_end())
        if self._since_segment >= _SEGMENT_S:
            out.append(self.results(is_final=True))
            self._open_segment()
        return out

    def end_turn(self) -> list[dict[str, object]]:
        """Close the open turn with a ``speech_final``, and owe an UtteranceEnd.

        The ``UtteranceEnd`` is owed rather than sent, because that is what was
        measured: it turns up once the next turn is already producing interims,
        naming a time inside the turn that is now over. See the module
        docstring; sending it here instead would test nothing.
        """
        if not self._speaking:
            return []
        out = [self.results(is_final=True, speech_final=True)]
        self._speaking = False
        self._late_end = self._last_word_end
        self._open_segment()
        return out

    def _owed_utterance_end(self) -> list[dict[str, object]]:
        """The previous turn's ``UtteranceEnd``, arriving inside this one."""
        end, self._late_end = self._late_end, None
        if end is None:
            return []
        return [{"type": "UtteranceEnd", "channel": [0, 1], "last_word_end": end}]

    def finalize(self) -> list[dict[str, object]]:
        """Answer a ``Finalize``: the buffered segment, flagged from_finalize."""
        if not self._speaking:
            return []
        out = [self.results(is_final=True, from_finalize=True)]
        self._speaking = False
        self._open_segment()
        return out

    def results(
        self, *, is_final: bool, speech_final: bool = False, from_finalize: bool = False
    ) -> dict[str, object]:
        transcript = " ".join(str(word["word"]) for word in self._words)
        return {
            "type": "Results",
            "channel_index": [0, 1],
            "start": round(self._segment_start, 4),
            "duration": round(self._at - self._segment_start, 4),
            "is_final": is_final,
            "speech_final": speech_final,
            "from_finalize": from_finalize,
            "channel": {
                "alternatives": [
                    {"transcript": transcript, "confidence": 0.98, "words": list(self._words)}
                ]
            },
        }

    def _open_segment(self) -> None:
        """Settle the segment just sent and start the next one where it ended."""
        self._segment_start = round(self._at, 4)
        self._since_interim = 0.0
        self._since_segment = 0.0
        self._words = []

    def _add_word(self) -> None:
        word = _WORDS[self._cursor % len(_WORDS)]
        self._cursor += 1
        entry: dict[str, object] = {
            "word": word,
            "punctuated_word": word.capitalize() if not self._words else word,
            "start": round(self._at - _INTERIM_S, 4),
            "end": round(self._at, 4),
            "confidence": 0.97,
        }
        self._last_word_end = round(self._at, 4)
        if self._diarize:
            # One speaker per turn, alternating: the real service keeps its
            # numbering for a connection's whole life, which is the thing a
            # test about label consistency across turns has to be able to see.
            entry["speaker"] = (self._turns - 1) % 2
        self._words.append(entry)


async def _live(websocket: ServerConnection, params: dict[str, list[str]]) -> None:
    """Handle one persistent live connection until the client closes it."""
    rates = params.get("sample_rate", [])
    live = _Live(
        rate=int(rates[0]) if rates and rates[0].isdigit() else _DEFAULT_RATE,
        diarize=_flag(params, "diarize"),
    )
    async for message in websocket:
        if isinstance(message, bytes):
            await _send(websocket, live.feed(message))
            continue
        data: object = json.loads(message)
        kind = cast("dict[str, object]", data).get("type") if isinstance(data, dict) else None
        if kind == "Finalize":
            await _send(websocket, live.finalize())
        elif kind == "CloseStream":
            await _send(websocket, live.end_turn())
            await websocket.send(json.dumps({"type": "Metadata", "duration": live.seconds}))
            return
        # KeepAlive is accepted and answered with nothing, as the service does.


async def _send(websocket: ServerConnection, messages: list[dict[str, object]]) -> None:
    for message in messages:
        await websocket.send(json.dumps(message))


def _loud(pcm: bytes) -> bool:
    """Whether a block is speech, by mean absolute sample value."""
    samples = array.array("h")
    samples.frombytes(pcm[: len(pcm) // 2 * 2])
    return bool(samples) and sum(abs(s) for s in samples) / len(samples) >= _SPEECH_LEVEL
