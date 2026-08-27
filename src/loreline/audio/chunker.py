"""VAD-driven utterance chunker (pure logic, no native deps).

Consumes fixed-size PCM frames plus a speech/no-speech decision per frame and
groups voiced frames into utterances, flushing after a configurable trailing
silence or when a maximum length is reached. A small pre-roll buffer of frames
preceding speech onset is prepended to avoid clipping word starts.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field

# Returns True if a frame contains speech.
SpeechDetector = Callable[[bytes], bool]


@dataclass(frozen=True, slots=True)
class Utterance:
    """A completed speech segment of concatenated PCM frames."""

    pcm: bytes
    start: float
    end: float


@dataclass(slots=True)
class VadChunker:
    """Group voiced frames into utterances.

    Args:
        sample_rate: PCM sample rate (Hz).
        frame_ms: Duration of each fed frame in milliseconds.
        silence_ms: Trailing silence that ends an utterance.
        max_utterance_s: Hard cap; force-flush even without a silence gap.
        pre_roll_ms: Audio kept before speech onset to avoid clipping.
    """

    sample_rate: int = 16000
    frame_ms: int = 20
    silence_ms: int = 800
    max_utterance_s: float = 30.0
    pre_roll_ms: int = 200

    _pre_roll: deque[bytes] = field(init=False, repr=False)
    _voiced: list[bytes] = field(default_factory=list[bytes], init=False, repr=False)
    _in_speech: bool = field(default=False, init=False)
    _silence_run_ms: int = field(default=0, init=False)
    _start_ts: float = field(default=0.0, init=False)
    _last_ts: float = field(default=0.0, init=False)

    def __post_init__(self) -> None:
        pre_roll_frames = max(1, self.pre_roll_ms // self.frame_ms)
        self._pre_roll = deque(maxlen=pre_roll_frames)

    @property
    def _max_ms(self) -> float:
        return self.max_utterance_s * 1000.0

    def feed(self, frame: bytes, *, ts: float, is_speech: bool) -> Utterance | None:
        """Feed one frame; return an Utterance when one completes."""
        self._last_ts = ts
        if not self._in_speech:
            self._pre_roll.append(frame)
            if is_speech:
                self._in_speech = True
                self._silence_run_ms = 0
                self._start_ts = ts - (len(self._pre_roll) - 1) * self.frame_ms / 1000.0
                self._voiced = list(self._pre_roll)
            return None

        # Currently inside an utterance.
        self._voiced.append(frame)
        self._silence_run_ms = 0 if is_speech else self._silence_run_ms + self.frame_ms

        duration_ms = len(self._voiced) * self.frame_ms
        if self._silence_run_ms >= self.silence_ms or duration_ms >= self._max_ms:
            return self._flush(end_ts=ts + self.frame_ms / 1000.0)
        return None

    def flush(self) -> Utterance | None:
        """Force-emit any buffered speech (e.g. on stop)."""
        if self._in_speech and self._voiced:
            return self._flush(end_ts=self._last_ts + self.frame_ms / 1000.0)
        return None

    def _flush(self, *, end_ts: float) -> Utterance:
        pcm = b"".join(self._voiced)
        utterance = Utterance(pcm=pcm, start=self._start_ts, end=end_ts)
        self._voiced = []
        self._in_speech = False
        self._silence_run_ms = 0
        self._pre_roll.clear()
        return utterance
