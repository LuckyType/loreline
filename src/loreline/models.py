"""Core domain models (pydantic v2)."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator


class ProviderKind(StrEnum):
    """Supported provider kinds (STT + LLM)."""

    DEEPGRAM = "deepgram"
    OPENAI = "openai"  # OpenAI cloud: realtime + batch transcription, and summaries
    # Any OpenAI-compatible endpoint you host yourself: Speaches, whisper.cpp,
    # Ollama, LM Studio, vLLM. Batch transcription and/or chat, per the server.
    OPENAI_COMPAT = "openai_compat"
    ASSEMBLYAI = "assemblyai"
    GEMINI = "gemini"  # Gemini API transcription (accepts a plain API key)
    OPENROUTER = "openrouter"  # OpenRouter gateway: transcription, summaries and video
    # xAI's own API: streaming + batch transcription, chat summaries and Grok
    # Imagine video. Reachable through the OpenRouter gateway as well, but that
    # is a different row with a different key, not a different vendor.
    XAI = "xai"


class Interaction(StrEnum):
    """What a provider is being asked to do.

    Providers are not interchangeable across these: a chat model cannot accept
    audio, a transcription model cannot write a summary, and only a video model
    generates video. Every model picker is scoped by one of these so a
    combination that cannot work is never offered - see
    :mod:`loreline.capabilities`.
    """

    TRANSCRIBE = "transcribe"
    SUMMARIZE = "summarize"
    VIDEO = "video"


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


class SessionOrigin(StrEnum):
    """Where a session's audio came from.

    An import is not a second kind of session: it produces the same continuous
    WAV and the same utterance index a capture does, and every feature reads
    them the same way (see ``docs/adr/0008``). What this records is the one
    thing that differs, that nobody ever listened to an import live, so its
    "original" transcript version is empty by construction and no re-run can
    ever fill it. Readers that would otherwise present that emptiness as a
    failed capture check this instead.
    """

    CAPTURE = "capture"
    IMPORT = "import"


class JobStatus(StrEnum):
    """Lifecycle state of a re-processing job.

    ``CANCELLED`` is terminal and is deliberately not ``ERROR``. A GM watching
    a re-transcription fill up can tell within a minute or two whether the
    model is worth the rest of the recording, and stopping it there is a
    decision, not a failure: nothing broke, the run did exactly what it was
    told, and everything it had written by then is kept and still readable
    (see :meth:`loreline.reprocess.jobs.ReprocessManager.cancel`). Every reader
    therefore has to be explicit about which question it is asking - "did this
    fail" means ``ERROR`` alone, "is this still going" means ``QUEUED`` or
    ``RUNNING`` alone - because a cancelled run answers no to both.

    Video jobs share this enum and never reach ``CANCELLED``: a generation is
    one remote call this app only polls, so there is nothing here to stop.
    """

    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    ERROR = "error"
    CANCELLED = "cancelled"


class ModelPrice(BaseModel):
    """What a model costs, in **USD per million tokens**.

    The source (OpenRouter's ``/models``) quotes USD per *single* token as a
    decimal string - "0.000003". That is unreadable in a picker and lossy as a
    float, so :mod:`loreline.catalog` parses it as ``Decimal`` and scales
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
    # Whether this model works with a *streaming* connector. Curated per model
    # (see loreline.capabilities.is_realtime_model) because it is a property of
    # the model AND the transport: Deepgram's whisper-* are batch-only, OpenAI's
    # gpt-live-transcribe is realtime-only, and Gemini's -live variant needs the
    # Live API entirely. None where the provider publishes no such distinction.
    realtime: bool | None = None
    # Whether "Inline (from STT)" diarization yields real speakers for this
    # model - see loreline.capabilities.supports_inline_diarization. The
    # pickers refuse that mode when this is False.
    inline_diarization: bool = False
    # Whether the model accepts a reasoning-effort setting. Read from the
    # provider's own parameter metadata where it publishes one (OpenRouter
    # does); False elsewhere, since guessing would show a control that silently
    # does nothing.
    supports_reasoning: bool = False
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
    # No `model` field, deliberately. One row serves every interaction its kind
    # declares - an OpenRouter provider transcribes, summarizes and generates
    # video - so a single stored model is the wrong answer for at least two of
    # them, and it was silently overriding the per-action defaults that already
    # do this job properly (ActionDefaults.stt_model / summarize_model /
    # video_model). The model is now chosen per request instead, required by
    # every action route; ``favorite_models`` is what a row still carries, as a
    # shortlist rather than a choice.
    favorite_models: list[str] = Field(default_factory=list[str])  # picked from the live model list
    sample_rate: int = 16000
    language: str = "de"
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
    """One segment of one transcript version: what was said, when, by whom.

    Two shapes produce these (see ``docs/adr/0006``). The utterance path emits
    one final event per ``Utterance`` and nothing else. The streaming path
    emits a growing interim (``is_final=False``) while a vendor's turn is open
    and one final when it closes, all of them carrying the same ``turn_id``, so
    the final replaces its interims rather than piling up behind them.

    ``turn_id`` is that replace key, and it is the whole key: a reader
    (the transcript table, either live feed) replaces a held event with an
    arriving one when both name the same ``turn_id``, and appends otherwise.
    None on the utterance path, where every event is settled when it is
    published and nothing ever replaces anything.

    A start timestamp would not do instead. It is stable across a turn's
    interims for a vendor that reports server-VAD offsets, which OpenAI does,
    but Deepgram and AssemblyAI both revise a turn's start as their endpointing
    refines, and a key that silently stops matching appends a second row rather
    than failing.
    """

    session_id: str
    source: str  # provider id, GAP_SOURCE, or a REPROCESS/DIARIZE tag
    text: str
    words: list[Word] = Field(default_factory=list[Word])
    speaker: str | None = None
    start_ts: float
    end_ts: float
    is_final: bool = False
    turn_id: str | None = None


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

# ``TranscriptEvent.source`` for the marker a streaming connector leaves where a
# dead connection swallowed audio (see ``loreline.stt.streaming``). A whole
# value rather than a prefix, because there is nothing to vary: one session's
# gaps are all the same kind of thing.
#
# It is a source rather than a flag so that everything already routing by source
# routes it for free: it belongs to the live capture's version, so the session
# page and the dashboard show it, and it is not text anybody said, so
# ``loreline.export.final_rows`` keeps it out of exports, summaries and the
# rows a diarize job relabels.
GAP_SOURCE = "gap"


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


class CampaignPlayer(BaseModel):
    """One seat at the table: the person, and the character they play.

    Both sides are optional and at least one has to be filled, because a GM
    knows them at different moments: the character before the player has a
    name for their fighter, the player when somebody is rolling for an absent
    friend. What is refused is a row with neither, which is a blank line the
    list would carry forever without ever biasing a recognizer or telling a
    model anything.

    The order of the rows is priority order, the same as a glossary's: the cast
    goes into :meth:`GlossaryRepository.get_effective` ahead of every other
    term, and a model's ceiling is spent from the head.
    """

    player: str = ""
    character: str = ""

    @field_validator("player", "character")
    @classmethod
    def _trimmed(cls, value: str) -> str:
        return value.strip()

    @model_validator(mode="after")
    def _somebody_is_named(self) -> CampaignPlayer:
        if not self.player and not self.character:
            raise ValueError("name the player, the character, or both")
        return self


class Campaign(BaseModel):
    """A campaign: the thing a session belongs to, and what collects its memory.

    Sessions have carried a ``campaign_id`` since the first schema, but nothing
    listed the campaigns, so the id was a string a GM could only set by hand
    and the app could only print back. A campaign is a row now: it has a name
    somebody chose, it owns a glossary (the ``glossaries`` table is keyed by
    the same id), and it is where a recap, an extraction and a "previously on"
    accumulate into something worth reading a year later.

    ``recap_prompt`` overrides the built-in recap instructions for this
    campaign only - a table that plays in German, or one that wants its recaps
    in character, says so once here rather than in every dialog.

    ``players`` is the cast at this table, in priority order. It is the one
    thing about a campaign that both halves of the app want: the names go to
    the STT ahead of every glossary term, and they tell an LLM which of the
    characters in a transcript are played rather than run.
    """

    id: str
    name: str
    created_at: float
    notes: str = ""
    recap_prompt: str = ""
    players: list[CampaignPlayer] = Field(default_factory=list[CampaignPlayer])


class CampaignSummary(BaseModel):
    """A campaign as the list page needs it: the row, plus what it holds.

    The two counts are the whole reason the list is not just the rows: "which
    of these am I actually playing" is answered by how many sessions it has and
    when the last one was, and neither is on the campaign itself.
    """

    campaign: Campaign
    sessions: int = 0
    last_session_at: float | None = None


# What a generated text is *about*, per session. One table holds them all (see
# migration v22), because every one of these is the same shape - a body, the
# model that wrote it, when - and the next one should be a new value here
# rather than a new table with its own repository and its own routes.
DOCUMENT_RECAP = "recap"
DOCUMENT_EXTRACTION = "extraction"
# The campaign-level one: what to read at the table before the next session.
DOCUMENT_PREVIOUSLY_ON = "previously_on"


class SessionDocument(BaseModel):
    """One generated text about one session, and what produced it.

    ``version`` is the transcript version it was read from, for the same reason
    ``Session.summary_version`` exists: a session holds the live capture plus
    one version per re-transcription, and a recap that cannot name the
    transcript behind it cannot be judged against it.
    """

    session_id: str
    kind: str
    body: str
    provider_id: str | None = None
    model: str | None = None
    version: str | None = None
    created_at: float


class CampaignDocument(BaseModel):
    """One generated text about a whole campaign (currently ``previously_on``)."""

    campaign_id: str
    kind: str
    body: str
    provider_id: str | None = None
    model: str | None = None
    created_at: float


class SearchHit(BaseModel):
    """One transcript line a search matched, with enough context to open it.

    ``snippet`` is the matched text with the hits wrapped in ``[`` and ``]`` -
    produced by FTS5's own ``snippet()`` where there is an index, and assembled
    around the match where the fallback ran, so a reader cannot tell which path
    answered them. ``version`` is what the session page's ``?v=`` takes and
    ``start_ts`` what its ``?t=`` takes, so a hit is a link to the line.
    """

    session_id: str
    started_at: float
    campaign_id: str | None = None
    version: str = ORIGINAL_VERSION
    speaker: str | None = None
    start_ts: float
    snippet: str


class ExtractedCharacter(BaseModel):
    """A person the session named: a player character or one of the GM's."""

    name: str
    kind: Literal["pc", "npc"] = "npc"
    notes: str = ""

    @field_validator("kind", mode="before")
    @classmethod
    def _unknown_kind_is_an_npc(cls, value: object) -> object:
        """Anything that is not literally "pc" is an NPC.

        A model asked for an enum answers with "PC", "player", "player
        character" and "Player Character" across four runs of the same prompt.
        Failing the whole extraction over the casing of one character's kind
        would throw away the other fifty names in it, and the distinction that
        matters - is this one of ours - survives the coercion.
        """
        if isinstance(value, str):
            text = value.strip().lower()
            return "pc" if text in ("pc", "player", "player character", "pcs") else "npc"
        return value


class ExtractedEntity(BaseModel):
    """A place, an item or a faction the session named."""

    name: str
    notes: str = ""


class ExtractedQuest(BaseModel):
    """A thread the session opened, advanced or closed."""

    title: str
    status: str = ""
    notes: str = ""


class SessionExtraction(BaseModel):
    """The structured names one session's transcript yielded.

    Every list defaults to empty, on purpose: a model that answers with four of
    the six keys has still said something useful about the session, and the
    missing ones mean "none of these came up", which is a true statement about
    plenty of sessions.
    """

    characters: list[ExtractedCharacter] = Field(default_factory=list[ExtractedCharacter])
    places: list[ExtractedEntity] = Field(default_factory=list[ExtractedEntity])
    items: list[ExtractedEntity] = Field(default_factory=list[ExtractedEntity])
    factions: list[ExtractedEntity] = Field(default_factory=list[ExtractedEntity])
    quests: list[ExtractedQuest] = Field(default_factory=list[ExtractedQuest])
    decisions: list[str] = Field(default_factory=list[str])


class MergedEntity(BaseModel):
    """One name across every session of a campaign that mentioned it.

    ``kind`` carries whatever secondary word the group has: "pc" or "npc" for a
    character, a quest's status for a quest, blank for a place, an item or a
    faction. One shape for all six groups, so the campaign page renders one
    list six times rather than six lists that drift apart.
    """

    name: str
    kind: str = ""
    notes: str = ""
    session_ids: list[str] = Field(default_factory=list[str])


class CampaignEntities(BaseModel):
    """Every session's extraction in a campaign, merged by name."""

    characters: list[MergedEntity] = Field(default_factory=list[MergedEntity])
    places: list[MergedEntity] = Field(default_factory=list[MergedEntity])
    items: list[MergedEntity] = Field(default_factory=list[MergedEntity])
    factions: list[MergedEntity] = Field(default_factory=list[MergedEntity])
    quests: list[MergedEntity] = Field(default_factory=list[MergedEntity])
    decisions: list[MergedEntity] = Field(default_factory=list[MergedEntity])


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
    # Transcript version the summary was made from ("original" or a transcribe
    # job id). None on a summary written before versions were recorded, which
    # is not the same as "original" and is why it is nullable rather than
    # defaulted: a session with five versions cannot be asked to guess which
    # one an old summary read.
    summary_version: str | None = None
    # Source session ids a merged row was made from, oldest first; empty for a
    # captured session. A merge otherwise looks exactly like its oldest source
    # in the history list - same start time, same status, same provider - so
    # this is what lets a reader tell the two apart without opening both.
    merged_from: list[str] = Field(default_factory=list[str])
    origin: SessionOrigin = SessionOrigin.CAPTURE
    """Whether this recording was captured here or imported from a file.

    Defaulted rather than required because every session that existed before
    imports did was a capture, and because a capture never has to say so."""
    import_name: str | None = None
    """The uploaded file's own name, for an import; None for a capture.

    The only name the recording ever had, and the only thing that tells four
    phone recordings of the same evening apart in the history list."""


class VideoModelInfo(BaseModel):
    """A video-generation model and the parameters it actually accepts.

    Straight off OpenRouter's ``GET /videos/models``. The lists are the point:
    every video model takes a *different* subset of durations, resolutions and
    aspect ratios, so the request form has to be built from the chosen model
    rather than offering one fixed set of controls and hoping. A None list
    means "this model does not take that parameter at all" - which is not the
    same as an empty list, and is why these are nullable rather than
    defaulting to [].
    """

    id: str
    name: str
    description: str | None = None
    supported_durations: list[int] | None = None  # seconds
    supported_resolutions: list[str] | None = None  # "720p", "4K", …
    supported_aspect_ratios: list[str] | None = None  # "16:9", …
    supported_sizes: list[str] | None = None  # explicit "WxH", where offered instead
    # Capability flags - whether the model takes these optional knobs at all.
    generate_audio: bool = False
    seed: bool = False


class VideoJob(BaseModel):
    """One video generation, from enqueue to a playable file on disk.

    Video generation is slow (minutes) and asynchronous at the source:
    OpenRouter returns a job id immediately and the result is polled. This row
    is the local mirror of that remote job, so a GM can close the tab, and so a
    finished video survives the signed URL OpenRouter hands back - which
    expires (there is a literal ``expired`` state upstream). ``remote_id`` is
    the upstream handle; ``video_path`` is set once the bytes are downloaded
    into the local store.
    """

    id: str
    session_id: str
    provider_id: str
    model: str
    prompt: str
    scene_model: str | None = None
    """The model that condensed ``scene_source`` into ``prompt``, when one did.

    Kept with the job rather than recomputed, because it is the only record
    that the prompt was written by a model at all: None means the GM wrote or
    edited it, and both halves are cleared the moment they touch it."""
    scene_source: str | None = None
    """The text the scene was condensed from, as the box held it."""
    duration: int | None = None
    resolution: str | None = None
    aspect_ratio: str | None = None
    generate_audio: bool = False
    seed: int | None = None
    status: JobStatus = JobStatus.QUEUED
    remote_id: str | None = None
    video_path: str | None = None
    created_at: float
    started_at: float | None = None
    finished_at: float | None = None
    error: str | None = None


class ReprocessJob(BaseModel):
    """A post-session re-transcription/re-diarization job."""

    id: str
    session_id: str
    provider_id: str
    operation: str = "transcribe"  # "transcribe" (re-STT) | "diarize" (per version)
    model: str | None = None  # the model the job ran with ("transcribe" only)
    use_glossary: bool = True  # whether the run fed the campaign glossary to the backend
    target: str = ORIGINAL_VERSION  # transcript version a "diarize" job relabels
    diarization: DiarizationConfig = Field(default_factory=DiarizationConfig)
    status: JobStatus = JobStatus.QUEUED
    created_at: float
    started_at: float | None = None
    finished_at: float | None = None
    segments_added: int = 0
    has_speakers: bool = False
    """Whether any row this job wrote carries a speaker label.

    Set as the rows are written, so a running re-transcription reports it as
    soon as its first labelled segment lands. It is what lets the version list
    say "diarized" about a version it has not loaded: a re-transcription that
    ran with a diarizer, or against a vendor that labels speakers itself, has
    labelled rows and no diarize job to show for them, and the page used to
    print "Not diarized" over lines that plainly named who spoke.
    """
    error: str | None = None
