"""Mock xAI streaming STT WebSocket server.

Mimics the contract used by ``XaiBackend``: greet with ``transcript.created`` as
soon as the socket is up (the real service does, which is what its health probe
reads), accept binary PCM, then on ``{"type":"audio.done"}`` emit the locked
``transcript.partial`` events for the utterance followed by one
``transcript.done`` carrying the WHOLE transcript, and close.

That last part is the thing worth mocking. The partials and the final event
overlap completely - ``transcript.done`` repeats everything the partials already
said - so a connector that accumulated both would emit each word twice. The
text below is built so that doubling is visible in an assertion rather than
plausible: the done text is exactly the two partials joined, and per-word
speaker labels are attached so inline diarization is exercised at the same time.

Deterministic, so tests can assert wiring (including the
one-connection-per-utterance lifecycle).

Docs: https://docs.x.ai/developers/model-capabilities/audio/speech-to-text
"""

from __future__ import annotations

import json
from typing import cast

from websockets.asyncio.server import ServerConnection

_SAMPLE_RATE = 16000


async def xai_handler(websocket: ServerConnection) -> None:
    """Handle one mock xAI streaming connection (one utterance each)."""
    await websocket.send(json.dumps({"type": "transcript.created"}))
    total_bytes = 0
    async for message in websocket:
        if isinstance(message, bytes):
            total_bytes += len(message)
            continue
        data: object = json.loads(message)
        kind = cast("dict[str, object]", data).get("type") if isinstance(data, dict) else None
        # "Finalize" is the other documented control message; this connector
        # never sends it, and neither branch of the real service ends the
        # session on it, so it is ignored here too.
        if kind == "audio.done":
            if total_bytes:
                for frame in _flush_frames(total_bytes // 2):
                    await websocket.send(json.dumps(frame))
            return


def _flush_frames(samples: int) -> list[dict[str, object]]:
    """One utterance's flush: two locked partials, then the whole transcript."""
    duration = round(samples / _SAMPLE_RATE, 3)
    quarter = duration / 4
    first_words: list[dict[str, object]] = [
        {"text": "xai", "start": 0.0, "end": quarter, "speaker": 0},
        {"text": "mock", "start": quarter, "end": quarter * 2, "speaker": 0},
    ]
    second_words: list[dict[str, object]] = [
        {"text": str(samples), "start": quarter * 2, "end": quarter * 3, "speaker": 1},
        {"text": "samples", "start": quarter * 3, "end": duration, "speaker": 1},
    ]
    return [
        _partial("xai mock", first_words, start=0.0, duration=quarter * 2),
        _partial(f"{samples} samples", second_words, start=quarter * 2, duration=quarter * 2),
        {
            "type": "transcript.done",
            "text": f"xai mock {samples} samples",
            "words": first_words + second_words,
            "duration": duration,
        },
    ]


def _partial(
    text: str, words: list[dict[str, object]], *, start: float, duration: float
) -> dict[str, object]:
    """A locked partial: is_final true, because interim results are off.

    ``speech_final`` marks the end of the utterance rather than of a three
    second chunk, so only the last one carries it.
    """
    return {
        "type": "transcript.partial",
        "text": text,
        "words": words,
        "is_final": True,
        "speech_final": start > 0,
        "start": start,
        "duration": duration,
    }
