"""Mock AssemblyAI Universal-Streaming (v3) WebSocket server.

Mimics the contract ``AssemblyAIBackend`` speaks, in both of the modes it uses
the endpoint in, chosen the way the real one chooses: by the query string.

**One utterance per session** (no ``continuous_partials``), what the utterance
shape uses: accept binary PCM, then on ``{"type":"Terminate"}`` flush final
formatted ``Turn`` messages (per-word speaker labels, millisecond timings)
followed by ``Termination`` and a clean close. Like the real service it may
flush one utterance as SEVERAL end-of-turn messages and resends an already-
ended turn as a formatted duplicate, so that connector accumulates turns and
deduplicates by ``turn_order``.

**One session per capture** (``continuous_partials=true``), what the streaming
shape uses: greet with ``Begin``, then decide the turns from the audio itself
and narrate them the way the real service does - ``SpeechStarted``, growing
partial ``Turn`` messages whose ``transcript`` is cumulative, then an
end-of-turn ``Turn`` and its formatted duplicate. ``ForceEndpoint`` ends the
open turn without ending the session; ``Terminate`` ends it, then sends the
``SpeakerRevision`` pass and ``Termination``. The endpointing rule is loudness
rather than a VAD model, which is enough to let a test drive turns by handing
it loud and quiet frames, and deterministic in a way a model would not be.

Both modes enforce the per-message audio duration window (50-1000 ms) by
closing the session with code 3007 on violation, as the real service does.

Docs: https://www.assemblyai.com/docs/speech-to-text/universal-streaming
"""

from __future__ import annotations

import array
import json
from typing import cast
from urllib.parse import parse_qs, urlparse

from websockets.asyncio.server import ServerConnection

_SAMPLE_RATE = 16000
_MIN_MS = 50
_MAX_MS = 1000
# Mean absolute sample value at or above which a block counts as speech. Well
# clear of the near-silent filler a test sends between turns, and well under a
# normal speaking level.
_SPEECH_LEVEL = 500
# Silence that closes a turn. The real default is 768 ms once speaker labels
# are on; shorter here so a test's turn does not cost a second of frames.
_SILENCE_MS = 600
# One partial per this much speech, so an open turn produces several.
_PARTIAL_MS = 300

_WORDS = ("the", "goblin", "takes", "the", "chest", "and", "runs", "for", "the", "door")


async def assemblyai_handler(websocket: ServerConnection) -> None:
    """Handle one mock AssemblyAI v3 connection, in whichever mode it asked for."""
    request = getattr(websocket, "request", None)
    query = parse_qs(urlparse(getattr(request, "path", "") or "").query)
    rate = int(next(iter(query.get("sample_rate", [])), _SAMPLE_RATE))
    if query.get("continuous_partials") == ["true"]:
        await _stream_session(
            websocket,
            rate,
            speakers=query.get("speaker_labels") == ["true"],
            languages=_languages(query),
        )
        return
    await _utterance_session(websocket, rate)


def _languages(query: dict[str, list[str]]) -> list[str]:
    """``language_codes``, decoded from its JSON-array query encoding.

    Real ``language_codes=["de"]`` in the query string; absent, or any value
    that does not decode to a list, means no language was requested.
    """
    raw = next(iter(query.get("language_codes", [])), None)
    if not raw:
        return []
    decoded: object = json.loads(raw)
    return cast("list[str]", decoded) if isinstance(decoded, list) else []


async def _utterance_session(websocket: ServerConnection, rate: int) -> None:
    """The old mode: one caller-bounded utterance, flushed by Terminate."""
    await websocket.send(json.dumps({"type": "Begin", "id": "mock-session", "expires_at": 0}))
    total_bytes = 0
    async for message in websocket:
        if isinstance(message, bytes):
            if not await _duration_ok(websocket, message, rate):
                return
            total_bytes += len(message)
            continue
        if _kind(message) == "Terminate":
            if total_bytes:
                for turn in _flush_turns(total_bytes // 2):
                    await websocket.send(json.dumps(turn))
            await websocket.send(
                json.dumps({"type": "Termination", "audio_duration_seconds": total_bytes / 32000})
            )
            return


async def _stream_session(
    websocket: ServerConnection, rate: int, *, speakers: bool, languages: list[str]
) -> None:
    """The persistent mode: one session, the server's own endpointing."""
    configuration: dict[str, object] = {"model": "universal-3-5-pro", "speaker_labels": speakers}
    if languages:
        configuration["language_codes"] = languages
    await websocket.send(
        json.dumps(
            {
                "type": "Begin",
                "id": "mock-session",
                "expires_at": 0,
                "configuration": configuration,
            }
        )
    )
    session = _Session(rate, speakers=speakers)
    async for message in websocket:
        if isinstance(message, bytes):
            if not await _duration_ok(websocket, message, rate):
                return
            await _send(websocket, session.feed(message))
            continue
        kind = _kind(message)
        if kind == "ForceEndpoint":
            await _send(websocket, session.end_turn())
        elif kind == "Terminate":
            await _send(websocket, session.end_turn())
            await _send(websocket, session.revisions())
            await websocket.send(
                json.dumps(
                    {
                        "type": "Termination",
                        "audio_duration_seconds": session.seconds,
                        "session_duration_seconds": session.seconds,
                    }
                )
            )
            return


class _Session:
    """The mock's turn machinery: loudness in, narrated turns out."""

    def __init__(self, rate: int, *, speakers: bool) -> None:
        self._rate = rate
        self._speakers = speakers
        self._ms = 0.0  # audio received on this connection, in milliseconds
        self._order = -1
        self._open: float | None = None  # this turn's start, in ms
        self._silence_ms = 0.0
        self._since_partial = 0.0
        self._words = 0
        self._ended: list[int] = []

    @property
    def seconds(self) -> float:
        return self._ms / 1000.0

    def feed(self, pcm: bytes) -> list[dict[str, object]]:
        """One audio message; the messages the real service would have sent."""
        block_ms = len(pcm) / 2 / self._rate * 1000
        self._ms += block_ms
        events: list[dict[str, object]] = []
        if _loud(pcm):
            self._silence_ms = 0.0
            if self._open is None:
                events.append(self._start())
            self._since_partial += block_ms
            if self._since_partial >= _PARTIAL_MS:
                self._since_partial = 0.0
                self._words = min(self._words + 1, len(_WORDS))
                events.append(self._turn(final=False))
        elif self._open is not None:
            self._silence_ms += block_ms
            if self._silence_ms >= _SILENCE_MS:
                events.extend(self.end_turn())
        return events

    def end_turn(self) -> list[dict[str, object]]:
        """Close the open turn, as silence, ForceEndpoint or Terminate does.

        Two messages, the way the real service behaves with ``format_turns``:
        the settled turn, then the same turn again once formatting has run.
        A connector on the streaming shape replaces the first with the second
        rather than joining them, because both name one ``turn_order``.
        """
        if self._open is None:
            return []
        self._words = max(1, self._words)
        events = [self._turn(final=True), self._turn(final=True, formatted=True)]
        self._ended.append(self._order)
        self._open = None
        return events

    def revisions(self) -> list[dict[str, object]]:
        """The end-of-session speaker pass, which only ever moves labels.

        Every ended turn comes back under the *other* speaker, so a test can
        tell a republished turn from the one it replaced by its labels alone.
        """
        if not self._speakers or not self._ended:
            return []
        return [
            {
                "type": "SpeakerRevision",
                "revisions": [
                    {
                        "turn_order": order,
                        "speaker_label": _speaker(order + 1),
                        "words": self._word_list(order, revised=True),
                    }
                    for order in self._ended
                ],
            }
        ]

    def _start(self) -> dict[str, object]:
        self._order += 1
        self._open = self._ms
        self._since_partial = 0.0
        self._words = 0
        return {"type": "SpeechStarted", "timestamp": round(self._ms), "confidence": 0.9}

    def _turn(self, *, final: bool, formatted: bool = False) -> dict[str, object]:
        text = " ".join(_WORDS[: self._words])
        turn: dict[str, object] = {
            "type": "Turn",
            "turn_order": self._order,
            "turn_is_formatted": formatted,
            "end_of_turn": final,
            "end_of_turn_confidence": 1.0 if final else 0.0,
            # Formatting is the only thing the duplicate changes, which is why
            # a connector must not concatenate the two.
            "transcript": f"{text.capitalize()}." if formatted else text,
            "utterance": text if final else "",
            "words": self._word_list(self._order, final=final),
        }
        if final:
            turn["speaker_label"] = _speaker(self._order) if self._speakers else None
        return turn

    def _word_list(
        self, order: int, *, final: bool = True, revised: bool = False
    ) -> list[dict[str, object]]:
        """This turn's words, spread evenly over the audio it covers.

        Partial words carry no ``speaker``: the real service attributes a word
        only once it is final, which is why inline diarization reads finals.
        """
        start = self._open if self._open is not None else 0.0
        span = max(self._ms - start, 1.0) / max(self._words, 1)
        words: list[dict[str, object]] = []
        for index in range(self._words):
            word: dict[str, object] = {
                "text": _WORDS[index],
                "start": round(start + index * span),
                "end": round(start + (index + 1) * span),
                "confidence": 0.9,
                "word_is_final": final,
            }
            if self._speakers and final:
                word["speaker"] = _speaker(order + 1 if revised else order)
            words.append(word)
        return words


def _speaker(order: int) -> str:
    """Which speaker a turn belongs to: alternating, so labels are visible."""
    return "AB"[order % 2]


def _loud(pcm: bytes) -> bool:
    """Whether a block is speech, by mean absolute sample value."""
    samples = array.array("h")
    samples.frombytes(pcm[: len(pcm) // 2 * 2])
    return bool(samples) and sum(abs(s) for s in samples) / len(samples) >= _SPEECH_LEVEL


def _kind(message: str | bytes) -> str | None:
    data: object = json.loads(message)
    return cast("dict[str, object]", data).get("type") if isinstance(data, dict) else None  # pyright: ignore[reportReturnType]


async def _duration_ok(websocket: ServerConnection, message: bytes, rate: int) -> bool:
    """Enforce the endpoint's 50-1000 ms window on one audio message."""
    duration_ms = len(message) / 2 / rate * 1000
    if _MIN_MS <= duration_ms <= _MAX_MS:
        return True
    await websocket.close(
        code=3007,
        reason=f"Input Duration Error: Input Duration Violation: {duration_ms} ms. "
        f"Expected between {_MIN_MS} and {_MAX_MS} ms",
    )
    return False


async def _send(websocket: ServerConnection, events: list[dict[str, object]]) -> None:
    for event in events:
        await websocket.send(json.dumps(event))


def _flush_turns(samples: int) -> list[dict[str, object]]:
    """One utterance's flush: several turns, the first re-sent formatted."""
    duration_ms = round(samples / _SAMPLE_RATE * 1000)
    half = duration_ms // 2
    first_words: list[dict[str, object]] = [
        {"text": "assemblyai", "start": 0, "end": half, "confidence": 0.96, "speaker": "A"}
    ]
    second_words: list[dict[str, object]] = [
        {"text": "mock", "start": half, "end": duration_ms, "confidence": 0.92, "speaker": "B"}
    ]
    return [
        _turn(0, "assemblyai mock", first_words, formatted=False),
        _turn(0, "assemblyai mock", first_words, formatted=True),
        _turn(1, f"{samples} samples", second_words, formatted=True),
    ]


def _turn(
    order: int, transcript: str, words: list[dict[str, object]], *, formatted: bool
) -> dict[str, object]:
    return {
        "type": "Turn",
        "turn_order": order,
        "end_of_turn": True,
        "turn_is_formatted": formatted,
        "transcript": transcript,
        "words": words,
    }
