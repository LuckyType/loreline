"""Core domain models (pydantic v2)."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

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
    OPENROUTER = "openrouter"  # OpenRouter LLM gateway for summaries ("vendor/model" ids)


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


class ModelPrice(BaseModel):
    """What a model costs, in **USD per million tokens**.

    The source (OpenRouter's ``/models``) quotes USD per *single* token as a
    decimal string - "0.000003". That is unreadable in a picker and lossy as a
    float, so :mod:`loreline.stt.catalog` parses it as ``Decimal`` and scales
    it here to the per-million figure every price list actually uses.

    ``min_prompt_tokens`` is None on the base price and set on a tier that only
    applies above a prompt-length threshold - see ModelInfo.price_tiers.
    """

    prompt: float | None = None  # input
    completion: float | None = None  # output
    min_prompt_tokens: int | None = None


class ModelInfo(BaseModel):
    """One entry in a provider's model list.

    Only ``id`` is ever guaranteed: a curated catalog entry (Deepgram, Google)
    and a plain OpenAI ``/models`` row carry nothing else, and the pickers must
    render those exactly as before. Everything below is filled in when the
    provider volunteers it - in practice OpenRouter, which is the only endpoint
    here that publishes prices at all.
    """

    id: str
    context_length: int | None = None
    pricing: ModelPrice | None = None
    # Prices that replace `pricing` above a prompt-length threshold, cheapest
    # threshold first. Long transcripts cross these - Claude Sonnet 4.5 doubles
    # above 200k prompt tokens - so a picker showing only the base price would
    # understate the cost of exactly the sessions this app produces.
    price_tiers: list[ModelPrice] = Field(default_factory=list["ModelPrice"])


class OpenRouterRouting(BaseModel):
    """OpenRouter provider-routing preferences. Ignored for every other kind.

    OpenRouter fans one model id out across several upstream providers that
    differ in price, speed and data policy, and picks between them itself.
    These narrow or reorder that pool; they are sent verbatim as the
    ``provider`` object of the chat-completions body - see
    https://openrouter.ai/docs/features/provider-routing.

    Every field defaults to OpenRouter's own default, and
    :func:`loreline.llm.routing_payload` omits any field still sitting at it,
    so a provider nobody has configured routing for keeps behaving exactly as
    before - and a default OpenRouter later changes stays theirs to change.

    Note the two privacy switches are not the same thing: ``data_collection``
    excludes providers that may *store or train on* the prompt, while ``zdr``
    additionally requires an endpoint under a Zero Data Retention agreement.
    Either can leave a given model with no eligible provider at all, in which
    case OpenRouter rejects the request rather than quietly falling back - the
    settings UI says so next to the switches.
    """

    sort: Literal["price", "throughput", "latency"] | None = None
    data_collection: Literal["allow", "deny"] = "allow"
    zdr: bool = False


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
    # OpenRouter-only, and None until the GM opts in - so the seven STT kinds'
    # stored JSON gains no field they have no use for.
    routing: OpenRouterRouting | None = None
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
# ``loreline.reprocess.jobs``). A session's transcript exists in *versions*:
# the live capture ("original", rows tagged with the provider id that produced
# them) and one per re-transcription job (rows tagged
# ``REPROCESS_SOURCE_PREFIX + job_id``). A diarization pass relabels ONE
# version's rows into a copy tagged ``DIARIZE_SOURCE_PREFIX + version`` that
# supersedes that version's raw rows on read (see
# ``loreline.export.variant_view``).
ORIGINAL_VERSION = "original"
DIARIZE_SOURCE_PREFIX = "diarize:"
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
    summary_provider: str | None = None  # provider id the summary came from
    summary_model: str | None = None  # model the summary was generated with


class ReprocessJob(BaseModel):
    """A post-session re-transcription/re-diarization job."""

    id: str
    session_id: str
    provider_id: str
    operation: str = "transcribe"  # "transcribe" (re-STT) | "diarize" (per version)
    model: str | None = None  # the model the job ran with ("transcribe" only)
    target: str = ORIGINAL_VERSION  # transcript version a "diarize" job relabels
    diarization: DiarizationConfig = Field(default_factory=DiarizationConfig)
    status: JobStatus = JobStatus.QUEUED
    created_at: float
    started_at: float | None = None
    finished_at: float | None = None
    segments_added: int = 0
    error: str | None = None
