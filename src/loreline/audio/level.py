"""Audio input level metering (pure stdlib; no numpy needed).

Computes peak and RMS amplitude of mono signed-16-bit little-endian PCM as
normalised 0.0-1.0 values, for the UI level meter. ``array`` reads native-endian
int16; all supported targets (x86, Pi ARM64) are little-endian, matching the
capture format used elsewhere.
"""

from __future__ import annotations

import array
import math

_INT16_FULL_SCALE = 32768.0


def levels(pcm: bytes) -> tuple[float, float]:
    """Return ``(peak, rms)`` amplitude of mono s16le ``pcm``, each in 0.0-1.0."""
    if not pcm:
        return 0.0, 0.0
    samples = array.array("h")
    samples.frombytes(pcm[: len(pcm) // 2 * 2])  # drop a dangling odd byte
    count = len(samples)
    if count == 0:
        return 0.0, 0.0
    peak = max(map(abs, samples)) / _INT16_FULL_SCALE
    rms = math.sqrt(sum(s * s for s in samples) / count) / _INT16_FULL_SCALE
    return peak, rms
