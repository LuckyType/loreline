"""Mock OpenAI Realtime transcription WebSocket server.

Mimics the transcription-session contract ``OpenAIRealtimeBackend`` speaks, in
both of the modes the real endpoint has, chosen the way the real one chooses
between them: by the ``turn_detection`` the ``session.update`` asks for.

**Manual commit** (``turn_detection: null``), what the utterance shape uses:
accept appends, and answer each ``input_audio_buffer.commit`` with one
``conversation.item.input_audio_transcription.completed``.

**Server VAD** (``turn_detection: {"type": "server_vad", ...}``), what the
streaming shape uses: decide the turns from the audio itself and narrate them,
each event carrying the ``item_id`` and the millisecond offsets the real
service carries. The rule is a loudness threshold rather than a VAD model,
which is enough to let a test drive turns by handing it loud and quiet frames,
and deterministic in a way a model would not be.

**Where the deltas land is the part worth copying.** ``speech_started`` opens
the turn, and then the service says nothing at all until the turn closes:
``speech_stopped``, ``committed``, every ``delta`` of the turn at once, then
``completed``. This mock used to invent one delta per 200 ms of speech, which
made the streaming path look like it showed text while somebody was still
talking. It does not, on this vendor: the ADR 0006 verification run measured a
median time to first interim of 7.6 s against a median turn length of 7.6 s,
which is a turn's whole length, because the interims *are* the turn closing.

**A model that will not stream** refuses the ``session.update`` that asks for
server VAD, exactly as ``gpt-live-transcribe`` and ``gpt-realtime-whisper`` do
against the real endpoint. The refusal is per model (see ``_NO_SERVER_VAD``),
which is how the real service picks, and it can also be forced for any model
with ``?refuse_turn_detection=true`` on the connect URL, for a test that wants
the refusal without naming one of those two.

Deterministic throughout, so tests can assert wiring without the real API.
"""

from __future__ import annotations

import array
import base64
import json
from typing import cast
from urllib.parse import parse_qs, urlparse

from websockets.asyncio.server import ServerConnection

_COMPLETED = "conversation.item.input_audio_transcription.completed"
_DELTA = "conversation.item.input_audio_transcription.delta"

# The rate the connector configures, and what the offsets are counted in. Read
# off the session.update when present so a test can drive the mock at 16 kHz.
_DEFAULT_RATE = 24_000
# Mean absolute sample value at or above which a block counts as speech. Well
# clear of the near-silent filler a test sends between turns, and well under a
# normal speaking level.
_SPEECH_LEVEL = 500
# Silence that closes a turn, matching the connector's server-VAD request.
_SILENCE_MS = 500
# One word of transcript per this much speech. Only the count is decided while
# the speaker talks; the deltas that carry the words all go out when the turn
# closes, which is what the real service does.
_WORD_MS = 200

_WORDS = ("the", "goblin", "takes", "the", "chest", "and", "runs")

# The two models capabilities.yaml routes to this connector, and the two that
# answer a server-VAD session.update with "Turn detection is not supported for
# this transcription model". Verbatim from the real endpoint: the refusal names
# the parameter, which is what the connector reads to tell it apart from every
# other kind of no.
_NO_SERVER_VAD = frozenset({"gpt-live-transcribe", "gpt-realtime-whisper"})
_TURN_DETECTION_REFUSAL: dict[str, object] = {
    "type": "error",
    "error": {
        "type": "invalid_request_error",
        "code": "unknown_parameter",
        "param": "session.audio.input.turn_detection",
        "message": "Turn detection is not supported for this transcription model.",
    },
}


class _Vad:
    """The mock's turn machinery: loudness in, narrated turns out."""

    def __init__(self, rate: int) -> None:
        self._rate = rate
        self._ms = 0.0  # audio received on this connection, in milliseconds
        self._turns = 0
        self.item: str | None = None
        self._silence_ms = 0.0
        self._since_word = 0.0
        self._words = 0

    def feed(self, pcm: bytes) -> list[dict[str, object]]:
        """One appended block; the events the real service would have sent.

        A voiced block grows the turn and says nothing. That silence is the
        behaviour being mocked, not an omission: this vendor narrates a turn
        only once it is over.
        """
        block_ms = len(pcm) / 2 / self._rate * 1000
        self._ms += block_ms
        events: list[dict[str, object]] = []
        if _loud(pcm):
            self._silence_ms = 0.0
            if self.item is None:
                events.append(self._start())
            else:
                self._since_word += block_ms
                if self._since_word >= _WORD_MS:
                    self._since_word = 0.0
                    self._words = min(self._words + 1, len(_WORDS))
        elif self.item is not None:
            self._silence_ms += block_ms
            if self._silence_ms >= _SILENCE_MS:
                events.extend(self.close())
        return events

    def close(self) -> list[dict[str, object]]:
        """End the open turn, if there is one, the way a commit would.

        Everything the turn ever produced arrives here, in the order the real
        service sends it: the speech ended, the buffer was committed, then the
        transcript one delta at a time, then the completed event carrying the
        whole of it.
        """
        item = self.item
        if item is None:
            return []
        words = _WORDS[: max(1, self._words)]
        self.item = None
        return [
            {
                "type": "input_audio_buffer.speech_stopped",
                "audio_end_ms": self._ms,
                "item_id": item,
            },
            {"type": "input_audio_buffer.committed", "item_id": item, "previous_item_id": None},
            *(_delta(item, word, first=index == 0) for index, word in enumerate(words)),
            {
                "type": _COMPLETED,
                "item_id": item,
                "content_index": 0,
                "transcript": " ".join(words),
            },
        ]

    def _start(self) -> dict[str, object]:
        self._turns += 1
        self.item = f"item_{self._turns:03d}"
        self._since_word = 0.0
        self._words = 1
        return {
            "type": "input_audio_buffer.speech_started",
            "audio_start_ms": self._ms,
            "item_id": self.item,
        }


def _delta(item: str, word: str, *, first: bool) -> dict[str, object]:
    """One transcription delta: an increment, not the whole interim so far."""
    return {
        "type": _DELTA,
        "item_id": item,
        "content_index": 0,
        "delta": word if first else f" {word}",
    }


def _loud(pcm: bytes) -> bool:
    """Whether a block is speech, by mean absolute sample value."""
    samples = array.array("h")
    samples.frombytes(pcm[: len(pcm) // 2 * 2])
    return bool(samples) and sum(abs(s) for s in samples) / len(samples) >= _SPEECH_LEVEL


def _dig(update: dict[str, object], *keys: str) -> object:
    """Follow a path of keys through the session payload, or return None."""
    node: object = update
    for key in keys:
        if not isinstance(node, dict):
            return None
        node = cast("dict[str, object]", node).get(key)  # pyright: ignore[reportUnnecessaryCast]
    return node


def _refuses_turn_detection(websocket: ServerConnection, model: object) -> bool:
    """Whether this session would be refused server VAD, as the real one is.

    By model, which is how the real endpoint decides, or by a query flag for a
    test that wants the refusal without naming a particular model.
    """
    request = websocket.request
    query = parse_qs(urlparse(request.path).query) if request is not None else {}
    if query.get("refuse_turn_detection") == ["true"]:
        return True
    return isinstance(model, str) and model in _NO_SERVER_VAD


async def openai_realtime_handler(websocket: ServerConnection) -> None:
    """Handle one mock Realtime transcription connection."""
    chunks = 0
    vad: _Vad | None = None
    async for message in websocket:
        if isinstance(message, bytes):
            continue
        data = cast("dict[str, object]", json.loads(message))
        kind = data.get("type")
        if kind == "session.update":
            detection = _dig(data, "session", "audio", "input", "turn_detection")
            model = _dig(data, "session", "audio", "input", "transcription", "model")
            if detection and _refuses_turn_detection(websocket, model):
                # The whole session.update is voided, which is what makes this
                # a refusal rather than a session with the turns left to us.
                await websocket.send(json.dumps(_TURN_DETECTION_REFUSAL))
                continue
            rate = _dig(data, "session", "audio", "input", "format", "rate")
            vad = _Vad(rate if isinstance(rate, int) else _DEFAULT_RATE) if detection else None
            # Ack the session config (lets a health probe confirm acceptance fast).
            await websocket.send(json.dumps({"type": "transcription_session.updated"}))
        elif kind == "input_audio_buffer.append":
            chunks += 1
            if vad is not None:
                await _send(websocket, vad.feed(base64.b64decode(str(data.get("audio", "")))))
        elif kind == "input_audio_buffer.commit":
            if vad is not None:
                # Server VAD commits its own turns, so an explicit commit is
                # the connector flushing the last one as the session ends.
                await _send(websocket, vad.close())
            else:
                # Emit the completed event but keep the session open for more
                # utterances (the backend reuses one connection for all of them).
                await websocket.send(
                    json.dumps(
                        {"type": _COMPLETED, "transcript": f"openai realtime mock ({chunks})"}
                    )
                )


async def _send(websocket: ServerConnection, events: list[dict[str, object]]) -> None:
    for event in events:
        await websocket.send(json.dumps(event))
