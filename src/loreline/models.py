"""Core domain models (pydantic v2)."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class ProviderKind(StrEnum):
    """Supported provider kinds (STT + LLM)."""

    DEEPGRAM = "deepgram"
    OPENAI = "openai"
    OPENAI_COMPAT = "openai_compat"  # Speaches / whisper.cpp / any OpenAI-compatible endpoint
    ASSEMBLYAI = "assemblyai"
    GOOGLE = "google"  # Cloud Speech-to-Text v2 (gRPC, OAuth2 only)
    GEMINI = "gemini"  # Gemini API transcription (accepts a plain API key)
    VOSK = "vosk"  # self-hosted vosk-server
    OPENAI_CHAT = "openai_chat"  # LLM chat for summaries (OpenAI / Ollama / LM Studio / vLLM)


class Protocol(StrEnum):
    """Transport protocol used by a provider connector."""

    WS = "ws"
    GRPC = "grpc"
    HTTP_SSE = "http_sse"
    HTTP_BATCH = "http_batch"


class DiarizationMode(StrEnum):
    """How speaker labels are produced."""

    INLINE = "inline"  # from a cloud STT that returns speaker labels
    REMOTE = "remote"  # self-hosted diarization service (e.g. sherpa-onnx)
    OPENAI = "openai"  # OpenAI batch diarization (gpt-4o-transcribe-diarize)
    NONE = "none"


class SessionStatus(StrEnum):
    """Lifecycle state of a capture session."""

    IDLE = "idle"
    CAPTURING = "capturing"
    STOPPING = "stopping"
    COMPLETED = "completed"
    ERROR = "error"


class JobStatus(StrEnum):
    """Lifecycle state of a re-processing job."""

    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    ERROR = "error"


class ProviderCaps(BaseModel):
    """Declared capabilities of a provider connector."""

    streaming: bool = True
    inline_diarization: bool = False
    vocab_param: str | None = None  # e.g. "keyterm", "word_boost", "speech_contexts"


class ProviderConfig(BaseModel):
    """User-configured STT provider instance (duplicates allowed)."""

    id: str
    name: str
    kind: ProviderKind
    base_url: str | None = None
    auth_ref: str | None = Field(
        default=None,
        description="Secret name in the secret store; never the raw key.",
    )
    protocol: Protocol
    model: str | None = None
    favorite_models: list[str] = Field(default_factory=list[str])  # picked from the live model list
    sample_rate: int = 16000
    language: str = "de"
    capabilities: ProviderCaps = Field(default_factory=ProviderCaps)
    enabled: bool = True


class DiarizationConfig(BaseModel):
    """Diarization settings for a session."""

    mode: DiarizationMode = DiarizationMode.NONE
    endpoint: str | None = None  # remote sherpa-onnx service URL
    min_speakers: int | None = None
    max_speakers: int | None = None


class SpeakerSegment(BaseModel):
    """A contiguous span attributed to a single speaker."""

    start: float
    end: float
    speaker: str


class Word(BaseModel):
    """A single recognized word with timing."""

    text: str
    start: float
    end: float
    confidence: float | None = None
    speaker: str | None = None


class TranscriptEvent(BaseModel):
    """A transcript segment emitted by a backend.

    Interim events may be upgraded to ``is_final`` and gain speaker labels once
    diarization completes.
    """

    session_id: str
    source: str  # provider id, or a REPROCESS_SOURCE_PREFIX/DIARIZE_SOURCE tag
    text: str
    words: list[Word] = Field(default_factory=list[Word])
    speaker: str | None = None
    start_ts: float
    end_ts: float
    is_final: bool = False


# ``TranscriptEvent.source`` tags used by post-session re-processing (see
# ``loreline.reprocess.jobs``) to distinguish alternate/derived transcript rows
# from the live capture, so read paths can select the canonical view (see
# ``loreline.export.canonical_transcript``).
DIARIZE_SOURCE = "diarize"
REPROCESS_SOURCE_PREFIX = "reprocess:"


def rebase_transcript(event: TranscriptEvent, offset: float) -> TranscriptEvent:
    """Shift an event's timestamps to be relative to a session origin.

    Capture timestamps are ``time.monotonic()`` values; subtracting the session's
    ``started_mono`` makes stored/exported times session-relative (0 = session
    start), which is what the UI, exports (SRT/VTT) and "seconds into session"
    all expect. ``offset == 0`` (e.g. legacy rows) is a no-op.
    """
    if not offset:
        return event
    return event.model_copy(
        update={
            "start_ts": event.start_ts - offset,
            "end_ts": event.end_ts - offset,
            "words": [
                word.model_copy(update={"start": word.start - offset, "end": word.end - offset})
                for word in event.words
            ],
        }
    )


class Glossary(BaseModel):
    """Per-campaign custom vocabulary (spell / character / place names)."""

    campaign_id: str
    terms: list[str] = Field(default_factory=list[str])


# Reserved campaign id for the always-on default word list (merged into every session).
DEFAULT_GLOSSARY_CAMPAIGN = "_default"


class Session(BaseModel):
    """A capture session record."""

    id: str
    status: SessionStatus = SessionStatus.IDLE
    started_at: float  # wall-clock epoch seconds (UTC) at session start
    started_mono: float = 0.0  # time.monotonic() captured at the same instant
    ended_at: float | None = None  # wall-clock epoch seconds (UTC) at stop
    campaign_id: str | None = None
    primary_provider: str | None = None
    fallback_provider: str | None = None
    diarization: DiarizationConfig = Field(default_factory=DiarizationConfig)
    audio_path: str | None = None
    speaker_names: dict[str, str] = Field(default_factory=dict[str, str])  # {label: display name}
    summary: str | None = None  # LLM-generated session summary (on demand)


class ReprocessJob(BaseModel):
    """A post-session re-transcription/re-diarization job."""

    id: str
    session_id: str
    provider_id: str
    operation: str = "transcribe"  # "transcribe" (re-STT) | "diarize" (session-wide)
    diarization: DiarizationConfig = Field(default_factory=DiarizationConfig)
    status: JobStatus = JobStatus.QUEUED
    created_at: float
    started_at: float | None = None
    finished_at: float | None = None
    segments_added: int = 0
    error: str | None = None
