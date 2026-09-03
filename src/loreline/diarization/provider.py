"""Factory selecting a diarization provider from session config."""

from __future__ import annotations

from loreline.diarization.base import DiarizationProvider, NoopDiarizer
from loreline.diarization.remote import RemoteDiarizer
from loreline.models import DiarizationConfig, DiarizationMode


def create_diarizer(
    config: DiarizationConfig, *, openai_api_key: str | None = None
) -> DiarizationProvider:
    """Build a diarization provider for the configured mode.

    ``inline`` returns a no-op diarizer: speaker labels already arrive on STT
    words, and segments are derived via ``segments_from_words`` at merge time.
    ``remote`` calls a self-hosted sherpa-onnx service. ``none`` produces no
    segments.

    ``openai_api_key`` is a key the caller has already resolved from the secret
    store - the reprocess manager reads it off a configured OpenAI provider row
    rather than requiring a separate ``OPENAI_API_KEY``. None means "nothing
    stored", and ``OpenAIDiarizer`` then falls back to the env var itself, so
    precedence is stored-then-env whoever asks. Passing it here rather than
    building the diarizer at the call site keeps this the only place the OpenAI
    diarizer is constructed.
    """
    if config.mode == DiarizationMode.REMOTE:
        if not config.endpoint:
            msg = "remote diarization requires an endpoint"
            raise ValueError(msg)
        return RemoteDiarizer(config.endpoint)
    if config.mode == DiarizationMode.OPENAI:
        from loreline.diarization.openai_diarizer import OpenAIDiarizer  # noqa: PLC0415

        return OpenAIDiarizer(api_key=openai_api_key)
    return NoopDiarizer()
