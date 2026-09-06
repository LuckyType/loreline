"""Mock xAI batch STT server.

Implements the subset of xAI's audio API used by ``XaiBatchBackend``:

- ``GET  /v1/models``  -> the chat catalogue, which is what the health probe and
  the summarize picker read on this vendor (there is no STT entry in it, and
  that is the point: see capabilities.yaml)
- ``POST /v1/stt``     -> ``{"text", "language", "duration", "words"}``

The transcript is deterministic and echoes the audio duration, the language and
any key terms, so a test can assert the whole request reached the server rather
than only that a request was made.

Two things it enforces, because both are how the real endpoint differs from the
OpenAI-shaped ones next to it:

- there is NO ``model`` field, and a request that sends one is rejected. A
  connector that helpfully added one would pass against a lenient mock and fail
  against api.x.ai.
- ``file`` must be the last multipart field, which this checks by reading the
  raw body rather than the parsed form, since parsing throws the order away.

Docs: https://docs.x.ai/developers/model-capabilities/audio/speech-to-text
"""

# pyright: reportUnusedFunction=false

from __future__ import annotations

import io
import re
import wave
from collections.abc import Sequence

from fastapi import FastAPI, Request, UploadFile
from fastapi.responses import JSONResponse

# Field name out of one multipart part's Content-Disposition header. The
# lookbehind is load bearing: the audio part reads
# `name="file"; filename="utterance.wav"`, and without it the filename counts
# as a second field and is always the last one found.
_FIELD_NAME = re.compile(rb'(?<!file)name="([^"]+)"')


def _wav_seconds(data: bytes) -> float:
    try:
        with wave.open(io.BytesIO(data), "rb") as wav:
            frames = wav.getnframes()
            rate = wav.getframerate() or 1
            return round(frames / rate, 3)
    except (wave.Error, EOFError):
        return 0.0


def _field_order(body: bytes) -> list[str]:
    """The multipart field names, in the order they were written."""
    return [name.decode("utf-8", "replace") for name in _FIELD_NAME.findall(body)]


def _one(value: object) -> str | None:
    """One form value as text, or None. Uploads are not values."""
    return value if isinstance(value, str) else None


def create_app(*, models: Sequence[str] = ("grok-4.6",)) -> FastAPI:
    """Build the mock xAI audio app.

    ``models`` is the chat catalogue served at ``/v1/models``: xAI lists no
    speech model there at all, so the default names a chat one, which is what
    makes an accidental "offer the /models list in the transcription picker"
    regression visible in a test.
    """
    app = FastAPI(title="mock-xai")

    @app.get("/v1/models")
    async def list_models() -> JSONResponse:
        return JSONResponse(
            {"object": "list", "data": [{"id": m, "object": "model"} for m in models]}
        )

    @app.post("/v1/stt")
    async def stt(request: Request) -> JSONResponse:
        # The raw body first, then the parsed form. Order matters: Starlette
        # caches the body once read and re-parses the form out of that cache,
        # while parsing first consumes the stream and leaves nothing to inspect
        # - and the two rules below are about the encoding, not the values.
        order = _field_order(await request.body())
        form = await request.form()
        if "model" in order:
            return JSONResponse({"error": {"message": "unknown field: model"}}, status_code=422)
        if order and order[-1] != "file":
            return JSONResponse(
                {"error": {"message": f"file must be the last field, got {order[-1]!r}"}},
                status_code=422,
            )
        upload = form.get("file")
        if not isinstance(upload, UploadFile):
            return JSONResponse({"error": {"message": "no audio"}}, status_code=422)
        language = _one(form.get("language"))
        diarize = _one(form.get("diarize")) == "true"
        keyterm = [str(value) for value in form.getlist("keyterm")]
        seconds = _wav_seconds(await upload.read())
        text = f"[{language or 'auto'}] mock transcription {seconds}s"
        if keyterm:
            text += f" (keyterm: {', '.join(keyterm)})"
        return JSONResponse(
            {
                "text": text,
                "language": language or "en",
                "duration": seconds,
                "words": _words(text, seconds, diarize=diarize),
            }
        )

    return app


def _words(text: str, seconds: float, *, diarize: bool) -> list[dict[str, object]]:
    """One word entry per token, evenly spaced, speakers alternating per half.

    Speakers only under ``diarize``, exactly as the real service: the field is
    absent otherwise, which is what lets a connector tell "nobody attributed
    this word" from "speaker zero".
    """
    tokens = text.split()
    if not tokens:
        return []
    step = (seconds or 1.0) / len(tokens)
    words: list[dict[str, object]] = []
    for index, token in enumerate(tokens):
        word: dict[str, object] = {
            "text": token,
            "start": round(index * step, 3),
            "end": round((index + 1) * step, 3),
        }
        if diarize:
            word["speaker"] = 0 if index < len(tokens) / 2 else 1
        words.append(word)
    return words


app = create_app()
