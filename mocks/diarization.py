"""Mock diarization service.

Implements the contract used by ``RemoteDiarizer``: ``POST /diarize``
(multipart WAV) -> ``{"segments": [...]}``, ``GET /healthz`` and
``DELETE /sessions/{id}``. Returns deterministic alternating-speaker segments
derived from the audio length so tests can assert the merge pipeline end to
end.

A ``session_id`` is recorded rather than acted on: the real service remembers a
session's voices and answers with labels stable across calls, which needs the
embedding models this mock exists to avoid. What a test can assert here is that
the app sends the id and drops the session afterwards, which is the part that
lives in ``RemoteDiarizer``.

It does answer the two fields the client reads about the service itself, since
those are contract rather than modelling: ``session_memory`` in ``/healthz``,
which says the build understands ``session_id`` at all (an older image accepts
the field and ignores it, and the probe grades that as degraded), and a
``generation`` per app instance, which the client watches for the restart that
renumbers a session's speakers.
"""

# pyright: reportUnusedFunction=false

from __future__ import annotations

import io
import secrets
import wave

from fastapi import FastAPI, Form, UploadFile
from fastapi.responses import JSONResponse


def _wav_seconds(data: bytes) -> float:
    try:
        with wave.open(io.BytesIO(data), "rb") as wav:
            return wav.getnframes() / (wav.getframerate() or 1)
    except (wave.Error, EOFError):
        return 0.0


def create_app() -> FastAPI:
    """Build the mock diarization app.

    ``seen_sessions`` on the returned app lists every ``session_id`` it was
    sent, and ``deleted_sessions`` every one it was asked to forget, in order.
    """
    app = FastAPI(title="mock-diarization")
    app.state.seen_sessions = []
    app.state.deleted_sessions = []
    # Per app, not per module: a test that builds two of these is two services,
    # and the second one restarting is precisely what a generation is for.
    generation: str = secrets.token_hex(8)
    app.state.generation = generation

    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        return JSONResponse({"status": "ok", "session_memory": True, "generation": generation})

    @app.delete("/sessions/{session_id}")
    async def forget_session(session_id: str) -> JSONResponse:
        deleted: list[str] = app.state.deleted_sessions
        deleted.append(session_id)
        return JSONResponse({"deleted": True})

    @app.post("/diarize")
    async def diarize(
        file: UploadFile,
        sample_rate: int = Form(16000),
        min_speakers: int | None = Form(None),
        max_speakers: int | None = Form(None),
        session_id: str | None = Form(None),
    ) -> JSONResponse:
        _ = (sample_rate, min_speakers, max_speakers)
        if session_id is not None:
            seen: list[str] = app.state.seen_sessions
            seen.append(session_id)
        duration = _wav_seconds(await file.read())
        half = round(duration / 2, 3)
        segments = [
            {"start": 0.0, "end": half, "speaker": "Speaker 0"},
            {"start": half, "end": round(duration, 3), "speaker": "Speaker 1"},
        ]
        return JSONResponse({"segments": segments, "generation": generation})

    return app


app = create_app()
