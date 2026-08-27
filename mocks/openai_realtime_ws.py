"""Mock OpenAI Realtime transcription WebSocket server.

Mimics the transcription-session contract used by ``OpenAIRealtimeBackend``:
accept a ``session.update``, one or more ``input_audio_buffer.append`` chunks and
an ``input_audio_buffer.commit``, then reply with a single
``conversation.item.input_audio_transcription.completed`` event. Deterministic so
tests can assert wiring without the real OpenAI API.
"""

from __future__ import annotations

import json
from typing import cast

from websockets.asyncio.server import ServerConnection

_COMPLETED = "conversation.item.input_audio_transcription.completed"


async def openai_realtime_handler(websocket: ServerConnection) -> None:
    """Handle one mock Realtime transcription connection."""
    chunks = 0
    async for message in websocket:
        if isinstance(message, bytes):
            continue
        data = cast("dict[str, object]", json.loads(message))
        kind = data.get("type")
        if kind == "session.update":
            # Ack the session config (lets a health probe confirm acceptance fast).
            await websocket.send(json.dumps({"type": "transcription_session.updated"}))
        elif kind == "input_audio_buffer.append":
            chunks += 1
        elif kind == "input_audio_buffer.commit":
            # Emit the completed event but keep the session open for more utterances
            # (the backend reuses one connection across the whole session).
            await websocket.send(
                json.dumps({"type": _COMPLETED, "transcript": f"openai realtime mock ({chunks})"})
            )
