"""OpenRouter transcription backend.

OpenRouter's STT is an OpenAI-compatible ``POST /audio/transcriptions``, so this
reuses :class:`OpenAICompatBackend` wholesale and only supplies the endpoint
and the attribution headers. The model arrives resolved from the registry,
which for this kind means an id in OpenRouter's ``vendor/model`` form - a bare
OpenAI model name is not a valid id here.

**Re-processing only.** OpenRouter offers no streaming, realtime or websocket
transcription - a single request/response file upload is the entire API. It is
therefore excluded from live capture (see
``loreline.capabilities.LIVE_CAPTURE_EXCLUDED``, which explains the reasoning)
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
from loreline.stt.registry import register

_BASE_URL = "https://openrouter.ai/api/v1"
# Not /models: verified live, OpenRouter serves its whole catalogue to an
# anonymous caller (425 models, no Authorization header), so probing it would
# call any key healthy, including none. /key describes the calling key itself.
# The chat connector asks the same route for the same reason; see
# loreline.llm._OPENROUTER_HEALTH_PATH.
_HEALTH_PATH = "/key"

# Same leaderboard attribution headers the chat and video connectors send.
_HEADERS = {
    "HTTP-Referer": "https://github.com/LuckyType/loreline",
    "X-Title": "Loreline",
}


@register(ProviderKind.OPENROUTER)
def _factory(  # pyright: ignore[reportUnusedFunction]
    config: ProviderConfig, secrets: SecretStore, model: str | None
) -> OpenAICompatBackend:
    api_key = secrets.get(config.auth_ref) if config.auth_ref else None
    return OpenAICompatBackend(
        config,
        model=model,
        api_key=api_key,
        default_base_url=_BASE_URL,
        extra_headers=_HEADERS,
        health_path=_HEALTH_PATH,
    )
