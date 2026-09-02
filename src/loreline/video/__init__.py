"""Video generation from a session summary (OpenRouter ``/videos``).

:mod:`loreline.video.client` speaks the API, :mod:`loreline.video.jobs` owns
the long-running job and its polling loop, and :mod:`loreline.video.store`
keeps the finished files next to session audio under ``data_dir``.
"""

from loreline.video.client import VideoError, list_video_models, supports_video
from loreline.video.jobs import (
    EmptyPromptError,
    ProviderNotFoundError,
    ProviderNotVideoCapableError,
    SessionNotFoundError,
    VideoManager,
)
from loreline.video.store import VideoStore

__all__ = [
    "EmptyPromptError",
    "ProviderNotFoundError",
    "ProviderNotVideoCapableError",
    "SessionNotFoundError",
    "VideoError",
    "VideoManager",
    "VideoStore",
    "list_video_models",
    "supports_video",
]
