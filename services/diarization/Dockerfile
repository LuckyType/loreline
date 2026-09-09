# Reference diarization service: sherpa-onnx offline speaker diarization.
FROM python:3.12-slim

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py ./

ENV DIAR_SEGMENTATION_MODEL="" \
    DIAR_EMBEDDING_MODEL=""

EXPOSE 8001
# --timeout-graceful-shutdown, because this container was measured being
# SIGKILLed on `docker stop`: uvicorn waits for in-flight requests before it
# exits, a diarization in flight can be minutes of native inference with no
# cancellation point in it, and Docker's own grace period is ten seconds. With
# a bound, uvicorn stops waiting and exits on its own; the worker thread the
# inference runs on is a daemon thread, so the process is free to leave it
# behind. A run interrupted that way is lost, which is the right trade: the
# caller has long since timed out, and the alternative measured in practice is
# an exit 137 and a service that never answered SIGTERM at all.
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8001", \
     "--timeout-graceful-shutdown", "5"]
