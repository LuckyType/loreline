"""Speaker diarization: providers and transcript merging.

Diarization is decoupled from STT (D2): speaker turns are produced independently
(inline from a cloud STT's per-word labels, or from a remote sherpa-onnx
service) and merged onto transcript words by time overlap.
"""

from __future__ import annotations

from loreline.diarization.base import DiarizationProvider, NoopDiarizer
from loreline.diarization.merge import assign_speakers, segments_from_words
from loreline.diarization.provider import create_diarizer
from loreline.diarization.remote import RemoteDiarizer

# NOTE: OpenAIDiarizer is intentionally not eagerly imported here; create_diarizer
# imports it lazily so the diarization package has no hard dependency on it.

__all__ = [
    "DiarizationProvider",
    "NoopDiarizer",
    "RemoteDiarizer",
    "assign_speakers",
    "create_diarizer",
    "segments_from_words",
]
