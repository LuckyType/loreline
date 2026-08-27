"""Mock OpenAI-compatible transcription server.

Implements the subset of the OpenAI Audio API used by ``OpenAICompatBackend``
(and therefore Speaches / whisper.cpp servers):

- ``GET  /v1/models`` -> model list (used by health checks)
- ``POST /v1/audio/transcriptions`` -> ``{"text": ...}``

The returned text is deterministic and echoes the uploaded audio duration and
any ``prompt`` (glossary) so tests can assert wiring end to end.
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
            frames = wav.getnframes()
            rate = wav.getframerate() or 1
            return round(frames / rate, 3)
    except (wave.Error, EOFError):
        return 0.0


def create_app() -> FastAPI:
    """Build the mock OpenAI-compatible transcription app."""
    app = FastAPI(title="mock-openai-compat")

    @app.get("/v1/models")
    async def models() -> JSONResponse:
        return JSONResponse({"object": "list", "data": [{"id": "whisper-1", "object": "model"}]})

    @app.post("/v1/audio/transcriptions")
    async def transcriptions(
        file: UploadFile,
        model: str = Form("whisper-1"),
        language: str = Form("de"),
        response_format: str = Form("json"),
        prompt: str | None = Form(None),
    ) -> JSONResponse:
        seconds = _wav_seconds(await file.read())
        text = f"[{model}/{language}] mock transcription {seconds}s"
        if prompt:
            text += f" (prompt: {prompt})"
        if response_format == "text":
            return JSONResponse(text)
        return JSONResponse({"text": text})

    return app


app = create_app()
