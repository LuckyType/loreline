# Loreline Diarization Service (sherpa-onnx)

Self-hosted speaker diarization for Loreline. Runs on a LAN x86 host (off the
capture device). Exposes the HTTP contract consumed by Loreline's
`RemoteDiarizer`.

## Endpoints

- `GET /healthz` -> `{"status": "ok"}`
- `POST /diarize` (multipart `file` = mono WAV) -> `{"segments": [{start, end, speaker}, ...]}`

Speaker labels are consecutive (`Speaker 0..k-1`) regardless of raw cluster ids. If
`min_speakers` and `max_speakers` are both sent and equal, that exact cluster count
is enforced (otherwise clustering is automatic).

## Models

sherpa-onnx needs a segmentation model (pyannote) and a speaker-embedding model.
Download from the sherpa-onnx model releases and point the service at them:

```bash
export DIAR_SEGMENTATION_MODEL=/models/sherpa-onnx-pyannote-segmentation-3-0/model.onnx
export DIAR_EMBEDDING_MODEL=/models/3dspeaker_speech_eres2net_base_sv_zh-cn_3dspeaker_16k.onnx
```

## Run

```bash
pip install -r requirements.txt
uvicorn app:app --host 0.0.0.0 --port 8001
```

Or via Docker (mount the model directory):

```bash
docker build -t loreline-diarization .
docker run --rm -p 8001:8001 \
  -e DIAR_SEGMENTATION_MODEL=/models/seg.onnx \
  -e DIAR_EMBEDDING_MODEL=/models/emb.onnx \
  -v /path/to/models:/models loreline-diarization
```

If the models are not configured, `/diarize` returns HTTP 503. Use
`mocks/diarization.py` for offline development/tests.
