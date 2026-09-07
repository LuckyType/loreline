"""The last N seconds of captured audio, sliceable by session time.

The utterance path never needed this: an ``Utterance`` arrives carrying its own
PCM, so whatever wants that audio again (the remote diarizer) already has it.
A streaming connector decides its turns from a feed nobody cut up, and says so
after the fact, in offsets. Something has to still be holding the audio those
offsets name, which is this.

Bounded on purpose, and by duration rather than by turn: an hour of 16 kHz
mono is 115 MB, and nothing here ever wants more than the turn that just
closed. Anything older is on disk in the session's WAV, where a re-process
reads it.
"""

from __future__ import annotations

_BYTES_PER_SAMPLE = 2  # mono s16le, the one format capture writes


class RollingPcm:
    """A window of mono s16le PCM, addressed by capture-clock seconds.

    Frames go in with the timestamp the capture loop gave them; a slice comes
    out for any span still inside the window. A span that has partly aged out
    returns the part that is left rather than nothing, because a short clip is
    a worse diarization than a long one and no clip at all is none - together
    with where that part really begins, which is not where it was asked for.
    """

    def __init__(self, sample_rate: int, *, seconds: float) -> None:
        self.sample_rate = sample_rate
        self._limit = int(sample_rate * seconds) * _BYTES_PER_SAMPLE
        self._buffer = bytearray()
        # Capture timestamp of the first sample still held. None until the
        # first frame, and moved forward every time the head is trimmed.
        self._start: float | None = None

    def append(self, pcm: bytes, ts: float) -> None:
        """Add one captured frame, dropping whatever no longer fits."""
        if self._start is None:
            self._start = ts
        self._buffer += pcm
        excess = len(self._buffer) - self._limit
        if excess > 0:
            del self._buffer[:excess]
            self._start += excess / _BYTES_PER_SAMPLE / self.sample_rate

    def slice(self, start: float, end: float) -> tuple[bytes, float]:
        """The audio between two capture-clock instants, and where it begins.

        The second value is where the returned audio really sits on the capture
        clock, which is later than ``start`` for a span whose beginning has
        aged out of the window. A caller that ships this to the diarizer gets
        0-based segments back and has to shift them by what it actually sent:
        shifting by what it asked for instead puts every label of a long turn
        early by however much of it was lost.

        ``(b"", 0.0)`` where nothing of the span is left, or where the span is
        empty or inverted.
        """
        origin = self._start
        if origin is None or end <= start:
            return b"", 0.0
        first = max(0, self._at(start, origin))
        last = min(len(self._buffer), self._at(end, origin))
        if last <= first:
            return b"", 0.0
        held_from = origin + first / _BYTES_PER_SAMPLE / self.sample_rate
        return bytes(self._buffer[first:last]), held_from

    def _at(self, ts: float, origin: float) -> int:
        """Where a capture-clock instant sits in the buffer, sample-aligned."""
        return round((ts - origin) * self.sample_rate) * _BYTES_PER_SAMPLE
