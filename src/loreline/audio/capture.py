"""Audio capture source.

``SoundDeviceSource`` is the production frame source backed by PortAudio (the
optional ``audio`` extra), yielding fixed-size timestamped PCM frames. Capture
orchestration (VAD + chunking + the continuous recording) lives in
``loreline.session.manager`` so it shares the session's bounded-queue decoupling.
"""

# pyright: reportMissingImports=false, reportUnknownMemberType=false
# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false
# pyright: reportMissingModuleSource=false, reportUnknownLambdaType=false
# pyright: reportUnknownParameterType=false, reportMissingParameterType=false

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import AsyncIterator

from loreline.logging import get_logger

log = get_logger(__name__)


class SoundDeviceSource:
    """Frame source backed by a PortAudio input stream (sounddevice)."""

    def __init__(
        self,
        *,
        device: int | str | None = None,
        sample_rate: int = 16000,
        frame_ms: int = 20,
    ) -> None:
        self._device = device
        self._sample_rate = sample_rate
        self._blocksize = int(sample_rate * frame_ms / 1000)
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    async def frames(self) -> AsyncIterator[tuple[bytes, float]]:
        import sounddevice as sd  # noqa: PLC0415

        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=256)

        def offer(frame: bytes) -> None:
            # Drop the oldest frame rather than raising QueueFull, which would
            # dump the raw PCM bytes into asyncio's exception log. The capture
            # task normally keeps this drained; this only bites on a transient stall.
            if queue.full():
                with contextlib.suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
            queue.put_nowait(frame)

        def callback(indata, _frames, _time, status) -> None:
            if status:  # pragma: no cover - device warnings
                log.warning("audio.status", status=str(status))
            loop.call_soon_threadsafe(offer, bytes(indata))

        def open_stream():
            return sd.RawInputStream(
                samplerate=self._sample_rate,
                blocksize=self._blocksize,
                device=self._device,
                channels=1,
                dtype="int16",
                callback=callback,
            )

        try:
            stream = open_stream()
        except sd.PortAudioError as exc:
            # See loreline.audio.portaudio: a long-running process can have a
            # perfectly good device fail to open after the OS audio subsystem
            # changes underneath it. One reinit-and-retry clears that without
            # requiring an app restart; a second failure is a real problem
            # (missing device, genuinely busy, ...) and propagates as-is.
            log.warning("audio.capture.open_failed", device=self._device, error=str(exc))
            from loreline.audio.portaudio import reinitialize  # noqa: PLC0415

            reinitialize()
            stream = open_stream()
        with stream:
            log.info("audio.capture.start", device=self._device, rate=self._sample_rate)
            while not self._stop.is_set():
                try:
                    frame = await asyncio.wait_for(queue.get(), timeout=0.5)
                except TimeoutError:
                    continue
                yield frame, time.monotonic()
        log.info("audio.capture.stop")
