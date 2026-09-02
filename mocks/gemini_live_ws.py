"""Mock Gemini Live API WebSocket server (``BidiGenerateContent``).

Mirrors the protocol ``GeminiLiveBackend`` speaks, as documented at
https://ai.google.dev/gemini-api/docs/live-api/live-transcribe: a ``setup``
message answered with ``setupComplete``, then ``realtimeInput`` audio chunks
(base64 PCM), then ``audioStreamEnd`` - flushed as an interim frame (which
connectors must ignore), the finalized transcript split across two
``inputTranscription`` frames (which they must concatenate), and a
``turnComplete``. Deterministic so tests can assert wiring; it cannot vouch
for the real service, which is exactly why the model stays hidden until a
verification run (see loreline.stt.catalog).
"""

from __future__ import annotations

import base64
import json
from typing import cast

from websockets.asyncio.server import ServerConnection


def _content(payload: dict[str, object]) -> str:
    return json.dumps({"serverContent": payload})


async def gemini_live_handler(websocket: ServerConnection) -> None:
    """Handle one mock Live session (one utterance each)."""
    samples = 0
    async for message in websocket:
        if isinstance(message, bytes):
            continue
        data = cast("dict[str, object]", json.loads(message))
        if "setup" in data:
            await websocket.send(json.dumps({"setupComplete": {}}))
            continue
        realtime = data.get("realtimeInput")
        if not isinstance(realtime, dict):
            continue
        realtime_map = cast("dict[str, object]", realtime)
        audio = realtime_map.get("audio")
        if isinstance(audio, dict):
            encoded = cast("dict[str, object]", audio).get("data")
            if isinstance(encoded, str):
                samples += len(base64.b64decode(encoded)) // 2
        if realtime_map.get("audioStreamEnd"):
            # Interim first: a connector that reads it would double the text.
            await websocket.send(_content({"interimInputTranscription": {"text": "gemini l"}}))
            await websocket.send(_content({"inputTranscription": {"text": "gemini live mock "}}))
            await websocket.send(_content({"inputTranscription": {"text": f"{samples} samples"}}))
            await websocket.send(_content({"turnComplete": True}))
            return
