"""Mock Deepgram streaming WebSocket server.

Mimics the Deepgram live transcription contract used by ``DeepgramBackend``:
accept binary linear16 PCM, then a ``{"type":"CloseStream"}`` control message
that finalizes everything buffered - like the real service, one utterance is
flushed as SEVERAL final ``Results`` frames (an empty near-silent lead-in
first, then the content splits, with per-word speaker labels), followed by
``Metadata`` and a clean close. Connectors must accumulate the finals, not
take the first. Deterministic so tests can assert wiring (including the
one-connection-per-utterance lifecycle).
"""

from __future__ import annotations

import json
from typing import cast

from websockets.asyncio.server import ServerConnection


async def deepgram_handler(websocket: ServerConnection) -> None:
    """Handle one mock Deepgram streaming connection (one utterance each)."""
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
