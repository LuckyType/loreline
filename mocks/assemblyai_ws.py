"""Mock AssemblyAI Universal-Streaming (v3) WebSocket server.

Mimics the contract used by ``AssemblyAIBackend``: on connect send ``Begin``,
accept binary PCM, then on ``{"type":"ForceEndpoint"}`` flush a final formatted
``Turn`` (per-word speaker labels, millisecond timings) while keeping the session
open - so one connection serves multiple utterances. A final
``{"type":"Terminate"}`` replies with ``Termination`` and closes.

Docs: https://www.assemblyai.com/docs/speech-to-text/universal-streaming
"""

from __future__ import annotations

import json
from typing import cast

from websockets.asyncio.server import ServerConnection


async def assemblyai_handler(websocket: ServerConnection) -> None:
    """Handle one mock AssemblyAI v3 streaming connection (reused across utterances)."""
    await websocket.send(json.dumps({"type": "Begin", "id": "mock-session", "expires_at": 0}))
    total_bytes = 0
    async for message in websocket:
        if isinstance(message, bytes):
            total_bytes += len(message)
            continue
        data: object = json.loads(message)
        kind = cast("dict[str, object]", data).get("type") if isinstance(data, dict) else None
        if kind == "ForceEndpoint":
            await websocket.send(json.dumps(_turn(total_bytes // 2)))
            total_bytes = 0  # next utterance starts fresh on the same connection
        elif kind == "Terminate":
            await websocket.send(
                json.dumps({"type": "Termination", "audio_duration_seconds": total_bytes / 32000})
            )
            return


def _turn(samples: int) -> dict[str, object]:
    duration_ms = round(samples / 16000 * 1000)
    half = duration_ms // 2
    return {
        "type": "Turn",
        "turn_order": 0,
        "end_of_turn": True,
        "turn_is_formatted": True,
        "transcript": f"assemblyai mock {samples} samples",
        "words": [
            {"text": "assemblyai", "start": 0, "end": half, "confidence": 0.96, "speaker": "A"},
            {"text": "mock", "start": half, "end": duration_ms, "confidence": 0.92, "speaker": "B"},
        ],
    }
