"""Audio capture source.

``SoundDeviceSource`` is the production frame source backed by PortAudio (the
optional ``audio`` extra), yielding fixed-size timestamped PCM frames at the
rate the rest of the pipeline works in, whatever rate the device itself runs
at. Capture orchestration (VAD + chunking + the continuous recording) lives in
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
from typing import Any

from loreline.audio.resample import Pcm16Stream
from loreline.logging import get_logger

log = get_logger(__name__)

_BYTES_PER_SAMPLE = 2  # mono s16le


class SoundDeviceSource:
    """Frame source backed by a PortAudio input stream (sounddevice).

    The device is opened at *its own* default rate and the frames are resampled
    to ``sample_rate`` on the way out. Asking PortAudio for the STT provider's
    rate directly is what a laptop or Pi microphone typically cannot give: most
    non-USB inputs serve only their native rate on the raw ALSA path, so the
    open fails - and it used to fail after the session had already been declared
    started, leaving a ticking "Recording" timer over a WAV that never grew past
    its 44-byte header.

    Callers see none of that: ``frames()`` yields fixed-size ``sample_rate``
    frames exactly as before, and a device that already runs at that rate is
    passed through untouched.
    """

    def __init__(
        self,
        *,
        device: int | str | None = None,
        sample_rate: int = 16000,
        frame_ms: int = 20,
    ) -> None:
        self._device = device
        self._sample_rate = sample_rate
        self._frame_ms = frame_ms
        self._frame_bytes = int(sample_rate * frame_ms / 1000) * _BYTES_PER_SAMPLE
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    def _device_rate(self, sd: Any) -> int:
        """The rate the device itself runs at, or the wanted rate if unknown.

        Same number the device picker shows (``InputDevice.default_samplerate``).
        A device PortAudio cannot describe is not necessarily one it cannot
        open, so an unanswerable query falls back to the wanted rate rather than
        failing the capture here.
        """
        try:
            info = sd.query_devices(self._device, "input")
            rate = round(float(info["default_samplerate"]))
        except Exception as exc:
            log.warning("audio.capture.rate_unknown", device=self._device, error=str(exc))
            return self._sample_rate
        return rate if rate > 0 else self._sample_rate

    def _open_stream(self, sd: Any, callback: Any, device_rate: int) -> Any:
        """Open the input stream at ``device_rate``, with one retry.

        See loreline.audio.portaudio: a long-running process can have a
        perfectly good device fail to open after the OS audio subsystem changes
        underneath it. One reinit-and-retry clears that without requiring an app
        restart; a second failure is a real problem (missing device, genuinely
        busy, ...) and propagates as-is.
        """

        def attempt() -> Any:
            return sd.RawInputStream(
                samplerate=device_rate,
                blocksize=max(1, int(device_rate * self._frame_ms / 1000)),
                device=self._device,
                channels=1,
                dtype="int16",
                callback=callback,
            )

        try:
            return attempt()
        except sd.PortAudioError as exc:
            log.warning(
                "audio.capture.open_failed",
                device=self._device,
                rate=device_rate,
                error=str(exc),
            )
            from loreline.audio.portaudio import reinitialize  # noqa: PLC0415

            reinitialize()
            return attempt()

    async def preflight(self) -> None:
        """Open the real device once and close it again, raising if it refuses.

        Called by ``SessionManager.start`` before there is a session to be wrong
        about, so a microphone that cannot be captured is a failed start request
        the GM sees immediately rather than a session that looks alive and
        records nothing. It goes through ``_device_rate`` / ``_open_stream`` -
        the same two calls ``frames()`` makes - because a check that tests
        something easier than the real thing is a check that passes while the
        capture still fails.
        """
        await asyncio.to_thread(self._probe)

    def _probe(self) -> None:
        """Blocking half of :meth:`preflight` (opening a device is not async)."""
        import sounddevice as sd  # noqa: PLC0415

        device_rate = self._device_rate(sd)
        # Built and thrown away with the stream: a resampler that cannot be
        # built would otherwise surface as silence at capture time.
        Pcm16Stream(device_rate, self._sample_rate)
        with self._open_stream(sd, lambda *_args: None, device_rate):
            pass
        log.info("audio.capture.preflight_ok", device=self._device, device_rate=device_rate)

    async def frames(self) -> AsyncIterator[tuple[bytes, float]]:
        import sounddevice as sd  # noqa: PLC0415

        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=256)

        def offer(block: bytes) -> None:
            # Drop the oldest block rather than raising QueueFull, which would
            # dump the raw PCM bytes into asyncio's exception log. The capture
            # task normally keeps this drained; this only bites on a transient stall.
            if queue.full():
                with contextlib.suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
            queue.put_nowait(block)

        def callback(indata, _frames, _time, status) -> None:
            if status:  # pragma: no cover - device warnings
                log.warning("audio.status", status=str(status))
            loop.call_soon_threadsafe(offer, bytes(indata))

        device_rate = self._device_rate(sd)
        # Built before the stream is opened, so a resampler that cannot be built
        # does not leak an open device.
        resampler = Pcm16Stream(device_rate, self._sample_rate)
        stream = self._open_stream(sd, callback, device_rate)
        # soxr hands back whatever whole samples its filter has ready, so the
        # resampled blocks do not line up with the frames downstream expects.
        # Re-blocking here keeps the fixed frame size the only thing a caller
        # has to know about this source.
        pending = bytearray()
        with stream:
            log.info(
                "audio.capture.start",
                device=self._device,
                rate=self._sample_rate,
                device_rate=device_rate,
            )
            while not self._stop.is_set():
                try:
                    block = await asyncio.wait_for(queue.get(), timeout=0.5)
                except TimeoutError:
                    continue
                pending += resampler.process(block)
                while len(pending) >= self._frame_bytes:
                    frame = bytes(pending[: self._frame_bytes])
                    del pending[: self._frame_bytes]
                    yield frame, time.monotonic()
        log.info("audio.capture.stop")
