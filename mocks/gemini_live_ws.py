"""Mock Gemini Live API WebSocket server (``BidiGenerateContent``).

Replays the frame sequence a real session sends, recorded during the
verification run of ``gemini-3.5-transcribe-live``: ``setup`` answered with
``setupComplete``, then, per turn, cumulative ``interimInputTranscription``
partials (which connectors must not add to the transcript), one
``inputTranscription`` final, ``generationComplete`` to close the turn, and
the empty padding frames that follow it.

Two turns, placed where the real service puts them: the first closes while
audio is still being sent, the second only after ``audioStreamEnd``. That is
the distinction the connector's receive loop rests on, and a mock that
answered only at the end could not express it.

The real service never sends ``turnComplete`` and never closes the socket
itself, and neither does this. Deterministic, so tests can assert wiring; what
it cannot vouch for is server-side VAD, which is what let an earlier
docs-shaped mock look right while a real session returned nothing.
"""

from __future__ import annotations

import base64
import json
from typing import cast

from websockets.asyncio.server import ServerConnection
from websockets.exceptions import ConnectionClosed


def _content(payload: dict[str, object]) -> str:
    return json.dumps({"serverContent": payload})


def turn_frames(text: str, *, trailing_empties: int) -> list[str]:
    """One recorded turn: partials, final, end of turn, padding.

    ``trailing_empties`` is two mid-session (one closes the turn, one heralds
    the next) and one for the turn the session ends on.
    """
    return [
        _content({"interimInputTranscription": {"text": text.split(maxsplit=1)[0]}}),
        _content({"interimInputTranscription": {"text": text}}),
        _content({"inputTranscription": {"text": text}}),
        _content({"generationComplete": True}),
        *(_content({}) for _ in range(trailing_empties)),
    ]


async def _send(websocket: ServerConnection, frames: list[str]) -> None:
    """Send frames, ignoring a client that has already hung up.

    A connector is entitled to close as soon as the turn it was waiting for
    ends, which can be mid-way through this turn's trailing padding.
    """
    for frame in frames:
        try:
            await websocket.send(frame)
        except ConnectionClosed:
            return


async def gemini_live_handler(websocket: ServerConnection) -> None:
    """Handle one mock Live session (one utterance each)."""
    samples = 0
    mid_turn_sent = False
    async for message in websocket:
        if isinstance(message, bytes):
            continue
        data = cast("dict[str, object]", json.loads(message))
        if "setup" in data:
            await _send(websocket, [json.dumps({"setupComplete": {}}), _content({})])
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
            if not mid_turn_sent:
                mid_turn_sent = True
                await _send(websocket, turn_frames("gemini live mock", trailing_empties=2))
        if realtime_map.get("audioStreamEnd"):
            await _send(websocket, turn_frames(f"{samples} samples", trailing_empties=1))
            # Deliberately no close and no further frame: the real service
            # goes quiet here and leaves the socket open, so returning (which
            # would close it) would hand the connector an end-of-session
            # signal that production never gives it.
