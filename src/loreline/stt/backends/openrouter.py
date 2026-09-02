"""OpenRouter transcription backend.

OpenRouter's STT is an OpenAI-compatible ``POST /audio/transcriptions``, so this
reuses :class:`OpenAICompatBackend` wholesale and only supplies the endpoint,
the attribution headers and a ``vendor/model`` default.

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
# Cheap, fast and multilingual - a sane default for replaying a whole session.
_DEFAULT_MODEL = "openai/whisper-large-v3-turbo"

# Same leaderboard attribution headers the chat and video connectors send.
_HEADERS = {
    "HTTP-Referer": "https://github.com/LuckyType/loreline",
    "X-Title": "Loreline",
}


@register(ProviderKind.OPENROUTER_STT)
def _factory(  # pyright: ignore[reportUnusedFunction]
    config: ProviderConfig, secrets: SecretStore
) -> OpenAICompatBackend:
    api_key = secrets.get(config.auth_ref) if config.auth_ref else None
    return OpenAICompatBackend(
        config,
        api_key=api_key,
        default_base_url=_BASE_URL,
        default_model=_DEFAULT_MODEL,
        extra_headers=_HEADERS,
    )
