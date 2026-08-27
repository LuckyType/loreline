"""Mock diarization service.

Implements the contract used by ``RemoteDiarizer``:
``POST /diarize`` (multipart WAV) -> ``{"segments": [...]}`` and ``GET /healthz``.
Returns deterministic alternating-speaker segments derived from the audio length
so tests can assert the merge pipeline end to end.
"""

# pyright: reportUnusedFunction=false

from __future__ import annotations

import io
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
    """Build the mock diarization app."""
    app = FastAPI(title="mock-diarization")

    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        return JSONResponse({"status": "ok"})

    @app.post("/diarize")
    async def diarize(
        file: UploadFile,
        sample_rate: int = Form(16000),
        min_speakers: int | None = Form(None),
        max_speakers: int | None = Form(None),
    ) -> JSONResponse:
        _ = (sample_rate, min_speakers, max_speakers)
        duration = _wav_seconds(await file.read())
        half = round(duration / 2, 3)
        segments = [
            {"start": 0.0, "end": half, "speaker": "Speaker 0"},
            {"start": half, "end": round(duration, 3), "speaker": "Speaker 1"},
        ]
        return JSONResponse({"segments": segments})

    return app


app = create_app()
