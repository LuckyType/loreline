"""OpenRouter transcription backend.

OpenRouter's STT is an OpenAI-compatible ``POST /audio/transcriptions``, so this
reuses :class:`OpenAICompatBackend` wholesale; the gateway base, the
leaderboard attribution headers and the ``/key`` health probe (``/models`` is
public there and would call any key healthy) are all on this kind's batch
transcription surface in capabilities.yaml. The model arrives resolved from
the registry, which for this kind means an id in OpenRouter's ``vendor/model``
form - a bare OpenAI model name is not a valid id here.

**Re-processing only.** OpenRouter offers no streaming, realtime or websocket
transcription - a single request/response file upload is the entire API. It is
therefore excluded from live capture (see
``loreline.capabilities.supports_live_capture``, which explains the reasoning)
and reachable only through post-session re-processing, where stored audio is
replayed utterance by utterance with no deadline.

It also returns no speaker labels, so it can serve the ``transcribe``
re-processing operation but never ``diarize`` - that stays with the sherpa-onnx
diarizer.
"""

from __future__ import annotations

from loreline.models import ProviderConfig, ProviderKind
from loreline.secrets import SecretStore
from loreline.stt.backends.openai_compat import OpenAICompatBackend
from loreline.stt.base import secret_for
from loreline.stt.registry import register


@register(ProviderKind.OPENROUTER)
def _factory(  # pyright: ignore[reportUnusedFunction]
    config: ProviderConfig, secrets: SecretStore, model: str | None
) -> OpenAICompatBackend:
    return OpenAICompatBackend(config, model=model, api_key=secret_for(config, secrets))
