"""Linear PCM resampling.

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
