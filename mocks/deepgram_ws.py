"""Mock Deepgram streaming WebSocket server.

Mimics the Deepgram live transcription contract used by ``DeepgramBackend``:
accept binary linear16 PCM, then a ``{"type":"Finalize"}`` control message that
flushes a final ``Results`` message (per-word speaker labels) while keeping the
connection open - so one connection serves multiple utterances. A final
``{"type":"CloseStream"}`` sends ``Metadata`` and closes. Deterministic so tests
can assert wiring (including connection reuse).
"""

from __future__ import annotations

import json
from typing import cast

from websockets.asyncio.server import ServerConnection


async def deepgram_handler(websocket: ServerConnection) -> None:
    """Handle one mock Deepgram streaming connection (reused across utterances)."""
    total_bytes = 0
    async for message in websocket:
        if isinstance(message, bytes):
            total_bytes += len(message)
            continue
        data: object = json.loads(message)
        kind = cast("dict[str, object]", data).get("type") if isinstance(data, dict) else None
        if kind == "Finalize":
            await websocket.send(json.dumps(_results(total_bytes // 2)))
            total_bytes = 0  # next utterance starts fresh on the same connection
        elif kind == "CloseStream":
            await websocket.send(json.dumps({"type": "Metadata", "duration": total_bytes / 32000}))
            return


def _results(samples: int) -> dict[str, object]:
    duration = round(samples / 16000, 3)
    return {
        "type": "Results",
        "is_final": True,
        "start": 0.0,
        "duration": duration,
        "channel": {
            "alternatives": [
                {
                    "transcript": f"deepgram mock {samples} samples",
                    "confidence": 0.95,
                    "words": [
                        {
                            "word": "deepgram",
                            "punctuated_word": "Deepgram",
                            "start": 0.0,
                            "end": duration / 2,
                            "confidence": 0.95,
                            "speaker": 0,
                        },
                        {
                            "word": "mock",
                            "punctuated_word": "mock",
                            "start": duration / 2,
                            "end": duration,
                            "confidence": 0.9,
                            "speaker": 1,
                        },
                    ],
                }
            ]
        },
    }
