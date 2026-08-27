"""Reference self-hosted diarization service (sherpa-onnx).

Runs on a LAN x86 host (off the capture device per D1/D2). Wraps sherpa-onnx
offline speaker diarization behind the HTTP contract expected by Loreline's
``RemoteDiarizer``:

- ``GET  /healthz`` -> ``{"status": "ok"}``
- ``POST /diarize`` (multipart ``file`` = mono WAV) ->
  ``{"segments": [{"start", "end", "speaker"}, ...]}``

Models are configured via environment variables (see README). The sherpa-onnx
import is deferred so the module imports cleanly where the native wheel is
absent (e.g. lint/typecheck in the main project CI).
"""

from __future__ import annotations

import io
import os
import wave

from fastapi import FastAPI, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse

_SEGMENTATION_MODEL = os.environ.get("DIAR_SEGMENTATION_MODEL", "")
_EMBEDDING_MODEL = os.environ.get("DIAR_EMBEDDING_MODEL", "")


def _load_pipeline(num_clusters: int = -1):
    """Build a diarization pipeline; ``num_clusters`` -1 = auto (threshold-based)."""
    import numpy as np  # noqa: PLC0415
    import sherpa_onnx  # noqa: PLC0415

    if not _SEGMENTATION_MODEL or not _EMBEDDING_MODEL:
        msg = "DIAR_SEGMENTATION_MODEL and DIAR_EMBEDDING_MODEL must be set"
        raise RuntimeError(msg)

    config = sherpa_onnx.OfflineSpeakerDiarizationConfig(
        segmentation=sherpa_onnx.OfflineSpeakerSegmentationModelConfig(
            pyannote=sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(
                model=_SEGMENTATION_MODEL
            ),
        ),
        embedding=sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=_EMBEDDING_MODEL),
        clustering=sherpa_onnx.FastClusteringConfig(num_clusters=num_clusters, threshold=0.5),
    )
    return sherpa_onnx.OfflineSpeakerDiarization(config), np


def _resolve_num_clusters(min_speakers: int | None, max_speakers: int | None) -> int:
    """Map Loreline's speaker bounds to sherpa-onnx's exact-cluster count.

    FastClusteringConfig supports either auto clustering (``num_clusters=-1``)
    or an exact count, so only an exact bound (``min == max``) can be honored.
    """
    if min_speakers is not None and max_speakers is not None and min_speakers == max_speakers:
        return max(1, min_speakers)
    return -1


def _read_wav(data: bytes) -> tuple[list[float], int]:
    with wave.open(io.BytesIO(data), "rb") as wav:
        rate = wav.getframerate()
        frames = wav.readframes(wav.getnframes())
    import numpy as np  # noqa: PLC0415

    samples = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
    return samples.tolist(), rate


def create_app() -> FastAPI:
    """Build the sherpa-onnx diarization service app."""
    app = FastAPI(title="loreline-diarization")
    state: dict[str, object] = {}

    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        return JSONResponse({"status": "ok"})

    @app.post("/diarize")
    def diarize(
        file: UploadFile,
        sample_rate: int = Form(16000),
        min_speakers: int | None = Form(None),
        max_speakers: int | None = Form(None),
    ) -> JSONResponse:
        # A plain `def` route runs in Starlette's threadpool instead of the
        # event loop: pipeline.process() below is synchronous ONNX inference
        # that can take real wall-clock time, and this service has no other
        # concurrent work worth protecting the loop for, so offloading it is
        # strictly better than blocking every other in-flight request on it.
        num_clusters = _resolve_num_clusters(min_speakers, max_speakers)
        pipelines: dict[int, tuple[object, object]] = state.get("pipelines", {})  # type: ignore[assignment]
        if num_clusters not in pipelines:
            try:
                pipelines[num_clusters] = _load_pipeline(num_clusters)
            except (RuntimeError, ImportError) as exc:
                raise HTTPException(status_code=503, detail=str(exc)) from exc
            state["pipelines"] = pipelines

        pipeline, np = pipelines[num_clusters]
        samples, rate = _read_wav(file.file.read())
        audio = np.array(samples, dtype=np.float32)
        result = pipeline.process(audio).sort_by_start_time()
        # Raw cluster ids can be sparse (unused clusters leave gaps); renumber to
        # consecutive labels by first appearance so the transcript shows Speaker 0..k-1.
        remap: dict[int, int] = {}
        segments = []
        for seg in result:
            speaker = remap.setdefault(seg.speaker, len(remap))
            segments.append(
                {"start": float(seg.start), "end": float(seg.end), "speaker": f"Speaker {speaker}"}
            )
        return JSONResponse({"segments": segments, "sample_rate": rate})

    return app


app = create_app()
