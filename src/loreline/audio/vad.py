"""Speech detection (VAD).

Defines the ``SpeechDetector`` protocol used by the chunker and a silero-vad
backed implementation. Silero (ONNX) is an optional native dependency from the
``audio`` extra.
"""

# pyright: reportMissingImports=false, reportUnknownMemberType=false
# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false
# pyright: reportMissingModuleSource=false, reportUnknownParameterType=false
# pyright: reportAttributeAccessIssue=false, reportPrivateImportUsage=false

from __future__ import annotations

_SR_16K = 16000
_WINDOW_16K = 512
_WINDOW_8K = 256
_INT16_FULL_SCALE = 32768.0


class SileroVad:
    """Voice activity detector backed by silero-vad (ONNX).

    Frames must be 16 kHz (or 8 kHz) mono int16 PCM. silero only accepts a fixed
    analysis window - 512 samples at 16 kHz, 256 at 8 kHz - and raises if given
    anything else. Capture frames rarely match that exactly (e.g. 20 ms = 320
    samples), so incoming PCM is buffered and fed to the model one full window at
    a time. Leftover samples are retained for the next call, and the most recent
    window's decision is returned. The model is stateful across windows, which is
    the intended streaming usage.
    """

    def __init__(self, *, sample_rate: int = 16000, threshold: float = 0.5) -> None:
        import numpy as np  # noqa: PLC0415
        import torch  # noqa: PLC0415
        from silero_vad import load_silero_vad  # noqa: PLC0415

        self._np = np
        self._torch = torch
        self._sample_rate = sample_rate
        self._threshold = threshold
        self._window = _WINDOW_16K if sample_rate >= _SR_16K else _WINDOW_8K
        self._buffer = np.zeros(0, dtype=np.int16)
        self._last = False
        self._model = load_silero_vad(onnx=True)

    def is_speech(self, frame: bytes) -> bool:
        """Return True if the buffered PCM most recently contained speech."""
        audio = self._np.frombuffer(frame, dtype=self._np.int16)
        if audio.size:
            self._buffer = self._np.concatenate([self._buffer, audio])
        while self._buffer.size >= self._window:
            window = self._buffer[: self._window]
            self._buffer = self._buffer[self._window :]
            tensor = self._torch.from_numpy(window.astype("float32") / _INT16_FULL_SCALE)
            prob = float(self._model(tensor, self._sample_rate).item())
            self._last = prob >= self._threshold
        return self._last
