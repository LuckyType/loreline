"""Mock OpenAI-compatible transcription server.

Implements the subset of the OpenAI Audio API used by ``OpenAICompatBackend``
(and therefore Speaches / whisper.cpp servers):

- ``GET  /v1/models`` -> model list (used by health checks and by a connector
  whose provider row names no model)
- ``POST /v1/audio/transcriptions`` -> ``{"text": ...}``

The returned text is deterministic and echoes the model, the uploaded audio
duration and any ``prompt`` (glossary) so tests can assert wiring end to end.

``model`` is required, as it is on the real thing: Speaches answers a request
without it ``422 {"type": "missing", "loc": ["body", "model"], "msg": "Field
required"}`` even when it has exactly one model loaded. This mock used to
default the field, which let a connector that omitted it pass here and fail
against the server this repo ships a compose service for.
"""

# pyright: reportUnusedFunction=false

from __future__ import annotations

import io
import wave
from collections.abc import Sequence

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


def create_app(*, models: Sequence[str] = ("whisper-1",)) -> FastAPI:
    """Build the mock OpenAI-compatible transcription app.

    ``models`` is what the server has loaded, which is the operator's choice on
    a self-hosted box: one is the ordinary case, several is the ambiguous one a
    connector with no configured model has to resolve, and none is a server
    that has nothing to transcribe with.
    """
    app = FastAPI(title="mock-openai-compat")

    @app.get("/v1/models")
    async def list_models() -> JSONResponse:
        return JSONResponse(
            {"object": "list", "data": [{"id": m, "object": "model"} for m in models]}
        )

    @app.post("/v1/audio/transcriptions")
    async def transcriptions(
        file: UploadFile,
        model: str = Form(...),
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
