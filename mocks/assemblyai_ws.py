"""Mock AssemblyAI Universal-Streaming (v3) WebSocket server.

Mimics the contract used by ``AssemblyAIBackend``: on connect send ``Begin``,
accept binary PCM, then on ``{"type":"Terminate"}`` flush final formatted
``Turn`` messages (per-word speaker labels, millisecond timings) followed by
``Termination`` and a clean close. Like the real service it enforces the
per-message audio duration window (50-1000 ms) by closing the session with
code 3007 on violation, may flush one utterance as SEVERAL end-of-turn
messages, and resends an already-ended turn as a formatted duplicate -
connectors must accumulate turns and deduplicate by ``turn_order``.

Docs: https://www.assemblyai.com/docs/speech-to-text/universal-streaming
"""

from __future__ import annotations

import json
from typing import cast

from websockets.asyncio.server import ServerConnection

_SAMPLE_RATE = 16000
_MIN_MS = 50
_MAX_MS = 1000


async def assemblyai_handler(websocket: ServerConnection) -> None:
    """Handle one mock AssemblyAI v3 streaming connection (one utterance each)."""
    await websocket.send(json.dumps({"type": "Begin", "id": "mock-session", "expires_at": 0}))
    total_bytes = 0
    async for message in websocket:
        if isinstance(message, bytes):
            duration_ms = len(message) / 2 / _SAMPLE_RATE * 1000
            if not _MIN_MS <= duration_ms <= _MAX_MS:
                await websocket.close(
                    code=3007,
                    reason=f"Input Duration Error: Input Duration Violation: {duration_ms} ms. "
                    f"Expected between {_MIN_MS} and {_MAX_MS} ms",
                )
                return
            total_bytes += len(message)
            continue
        data: object = json.loads(message)
        kind = cast("dict[str, object]", data).get("type") if isinstance(data, dict) else None
        if kind == "Terminate":
            if total_bytes:
                for turn in _flush_turns(total_bytes // 2):
                    await websocket.send(json.dumps(turn))
            await websocket.send(
                json.dumps({"type": "Termination", "audio_duration_seconds": total_bytes / 32000})
            )
            return


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
