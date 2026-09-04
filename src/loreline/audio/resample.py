"""PCM resampling: one-shot (linear) and streaming (soxr).

Isolated here (with the native-dep pyright pragma) because it uses numpy from the
optional ``audio`` extra; keeping it out of the STT backend lets that module stay
fully type-checked even when numpy is not installed (e.g. the light CI lane).
"""

# pyright: reportMissingImports=false, reportUnknownMemberType=false
# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false
# pyright: reportMissingModuleSource=false

from __future__ import annotations

_INT16_MIN = -32768
_INT16_MAX = 32767


def resample_pcm16(pcm: bytes, src_rate: int, dst_rate: int) -> bytes:
    """Linear-resample mono s16le PCM from ``src_rate`` to ``dst_rate``."""
    if src_rate == dst_rate or not pcm:
        return pcm
    import numpy as np  # noqa: PLC0415 - lazy: numpy is an optional audio-extra dep

    samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
    n_out = max(1, round(len(samples) * dst_rate / src_rate))
    x_in = np.linspace(0.0, 1.0, num=len(samples), endpoint=False)
    x_out = np.linspace(0.0, 1.0, num=n_out, endpoint=False)
    resampled = np.interp(x_out, x_in, samples)
    return np.clip(resampled, _INT16_MIN, _INT16_MAX).astype(np.int16).tobytes()


class Pcm16Stream:
    """A continuous mono s16le stream being resampled block by block.

    ``resample_pcm16`` above is right for a finished utterance and wrong for a
    live microphone: it treats every block as a self-contained signal, so each
    block boundary loses the previous block's tail (a click 50 times a second),
    and linear interpolation carries no anti-alias filter, so downsampling folds
    the 8-24 kHz half of a 48 kHz room straight back into the band the VAD and
    the STT model listen to. soxr keeps its filter state across blocks and does
    the filtering, for ~10us per 20ms block - small enough to run on the capture
    device this project targets.

    ``src_rate == dst_rate`` is a pass-through that touches neither numpy nor
    soxr, so a device that already serves the wanted rate needs no native
    resampler at all.
    """

    def __init__(self, src_rate: int, dst_rate: int) -> None:
        self.src_rate = src_rate
        self.dst_rate = dst_rate
        self._passthrough = src_rate == dst_rate
        if self._passthrough:
            return
        try:
            import numpy as np  # noqa: PLC0415 - lazy: optional audio-extra deps
            import soxr  # noqa: PLC0415
        except ImportError as exc:  # pragma: no cover - the audio extra pins soxr
            msg = (
                f"cannot resample {src_rate} Hz capture to {dst_rate} Hz: soxr is "
                "not installed (it ships with the `audio` extra)"
            )
            raise RuntimeError(msg) from exc
        self._np = np
        # HQ is soxr's default and the quality a speech model deserves; the
        # cheaper settings buy microseconds that nothing here needs.
        self._stream = soxr.ResampleStream(src_rate, dst_rate, 1, dtype="int16", quality="HQ")

    def process(self, pcm: bytes) -> bytes:
        """Resample one captured block, continuing from the previous one.

        The output length is only *approximately* the ratio: soxr holds a
        block's tail back until the next one arrives, so early blocks come out
        short and some come out empty. Callers must re-block the result rather
        than assume a fixed size per call.
        """
        if self._passthrough:
            return pcm
        block = self._np.frombuffer(pcm, dtype=self._np.int16)
        return self._stream.resample_chunk(block).tobytes()
