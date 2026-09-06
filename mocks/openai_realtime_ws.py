"""Mock OpenAI Realtime transcription WebSocket server.

Mimics the transcription-session contract ``OpenAIRealtimeBackend`` speaks, in
both of the modes the real endpoint has, chosen the way the real one chooses
between them: by the ``turn_detection`` the ``session.update`` asks for.

**Manual commit** (``turn_detection: null``), what the utterance shape uses:
accept appends, and answer each ``input_audio_buffer.commit`` with one
``conversation.item.input_audio_transcription.completed``.

**Server VAD** (``turn_detection: {"type": "server_vad", ...}``), what the
streaming shape uses: decide the turns from the audio itself and narrate them,
``speech_started`` then ``delta``s then ``speech_stopped``, ``committed`` and
``completed``, each carrying the ``item_id`` and the millisecond offsets the
real service carries. The rule is a loudness threshold rather than a VAD model,
which is enough to let a test drive turns by handing it loud and quiet frames,
and deterministic in a way a model would not be.

Deterministic throughout, so tests can assert wiring without the real API.
"""

from __future__ import annotations

import array
import base64
import json
from typing import cast

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
# One delta per this much speech, so a turn produces several interims.
_DELTA_MS = 200

_WORDS = ("the", "goblin", "takes", "the", "chest", "and", "runs")


class _Vad:
    """The mock's turn machinery: loudness in, narrated turns out."""

    def __init__(self, rate: int) -> None:
        self._rate = rate
        self._ms = 0.0  # audio received on this connection, in milliseconds
        self._turns = 0
        self.item: str | None = None
        self._silence_ms = 0.0
        self._since_delta = 0.0
        self._words = 0

    def feed(self, pcm: bytes) -> list[dict[str, object]]:
        """One appended block; the events the real service would have sent."""
        block_ms = len(pcm) / 2 / self._rate * 1000
        self._ms += block_ms
        events: list[dict[str, object]] = []
        if _loud(pcm):
            self._silence_ms = 0.0
            if self.item is None:
                events.append(self._start())
            else:
                self._since_delta += block_ms
                if self._since_delta >= _DELTA_MS:
                    self._since_delta = 0.0
                    events.append(self._delta())
        elif self.item is not None:
            self._silence_ms += block_ms
            if self._silence_ms >= _SILENCE_MS:
                events.extend(self.close())
        return events

    def close(self) -> list[dict[str, object]]:
        """End the open turn, if there is one, the way a commit would."""
        item = self.item
        if item is None:
            return []
        text = " ".join(_WORDS[: max(1, self._words)])
        self.item = None
        return [
            {
                "type": "input_audio_buffer.speech_stopped",
                "audio_end_ms": self._ms,
                "item_id": item,
            },
            {"type": "input_audio_buffer.committed", "item_id": item, "previous_item_id": None},
            {"type": _COMPLETED, "item_id": item, "content_index": 0, "transcript": text},
        ]

    def _start(self) -> dict[str, object]:
        self._turns += 1
        self.item = f"item_{self._turns:03d}"
        self._since_delta = 0.0
        self._words = 0
        return {
            "type": "input_audio_buffer.speech_started",
            "audio_start_ms": self._ms,
            "item_id": self.item,
        }

    def _delta(self) -> dict[str, object]:
        word = _WORDS[min(self._words, len(_WORDS) - 1)]
        self._words += 1
        return {
            "type": _DELTA,
            "item_id": self.item,
            "content_index": 0,
            "delta": word if self._words == 1 else f" {word}",
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
