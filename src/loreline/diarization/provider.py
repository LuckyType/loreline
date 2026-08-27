"""Factory selecting a diarization provider from session config."""

from __future__ import annotations

from loreline.diarization.base import DiarizationProvider, NoopDiarizer
from loreline.diarization.remote import RemoteDiarizer
from loreline.models import DiarizationConfig, DiarizationMode


def create_diarizer(config: DiarizationConfig) -> DiarizationProvider:
    """Build a diarization provider for the configured mode.

    ``inline`` returns a no-op diarizer: speaker labels already arrive on STT
    words, and segments are derived via ``segments_from_words`` at merge time.
    ``remote`` calls a self-hosted sherpa-onnx service. ``none`` produces no
    segments.
    """
    if config.mode == DiarizationMode.REMOTE:
        if not config.endpoint:
            msg = "remote diarization requires an endpoint"
            raise ValueError(msg)
        return RemoteDiarizer(config.endpoint)
    if config.mode == DiarizationMode.OPENAI:
        from loreline.diarization.openai_diarizer import OpenAIDiarizer  # noqa: PLC0415

        return OpenAIDiarizer()
    return NoopDiarizer()
