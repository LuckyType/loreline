"""Session lifecycle routes: start/stop + listing + transcript fetch + bulk ops."""

from __future__ import annotations

import asyncio
import uuid

from fastapi import APIRouter, Depends, Request
from fastapi.exceptions import HTTPException
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel
from starlette.status import (
    HTTP_400_BAD_REQUEST,
    HTTP_404_NOT_FOUND,
    HTTP_409_CONFLICT,
    HTTP_502_BAD_GATEWAY,
)

from loreline.export import EXPORTERS, canonical_transcript, relabel_speakers, to_txt, variant_view
from loreline.llm import DEFAULT_MODEL, LLMError, summarize_transcript
from loreline.models import (
    ORIGINAL_VERSION,
    ProviderKind,
    Session,
    SessionStatus,
    TranscriptEvent,
    rebase_transcript,
)
from loreline.session import (
    ProviderDisabledError,
    ProviderNotFoundError,
    SessionActiveError,
    SessionConfigError,
)
from loreline.web.auth import require_auth
from loreline.web.deps import get_manager, get_state, load_action_defaults
from loreline.web.routes.audio import INPUT_DEVICE_KEY, parse_device
from loreline.web.schemas import (
    OkResponse,
    SessionIds,
    SpeakerNamesUpdate,
    StartSessionRequest,
    SummarizeRequest,
    SummarizeResult,
)

router = APIRouter(prefix="/api/session", tags=["sessions"], dependencies=[Depends(require_auth)])

_MERGE_MIN_SESSIONS = 2


class SessionDetail(BaseModel):
    """A session plus its persisted transcript."""

    session: Session
    transcript: list[TranscriptEvent]


@router.post("/start", status_code=201)
async def start_session(request: Request, body: StartSessionRequest) -> Session:
    """Start a capture session (using the saved default mic when none is supplied)."""
    manager = get_manager(request)
    if body.device is None:
        body.device = parse_device(await get_state(request).settings_repo.get(INPUT_DEVICE_KEY))
    try:
        return await manager.start(body)
    except SessionActiveError as exc:
        raise HTTPException(
            status_code=HTTP_409_CONFLICT, detail="a session is already running"
        ) from exc
    except ProviderNotFoundError as exc:
        raise HTTPException(status_code=HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ProviderDisabledError as exc:
        raise HTTPException(status_code=HTTP_409_CONFLICT, detail=str(exc)) from exc
    except SessionConfigError as exc:
        raise HTTPException(status_code=HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.post("/stop")
async def stop_session(request: Request) -> Session:
    """Stop the active capture session."""
    session = await get_manager(request).stop()
    if session is None:
        raise HTTPException(status_code=HTTP_409_CONFLICT, detail="no active session")
    return session


@router.get("")
async def list_sessions(request: Request) -> list[Session]:
    """Return all sessions, newest first."""
    return await get_state(request).sessions.list()


@router.get("/{session_id}")
async def get_session(request: Request, session_id: str) -> SessionDetail:
    """Return a session and its transcript."""
    state = get_state(request)
    session = await state.sessions.get(session_id)
    if session is None:
        raise HTTPException(status_code=HTTP_404_NOT_FOUND, detail="session not found")
    transcript = canonical_transcript(await state.transcripts.for_session(session_id))
    return SessionDetail(session=session, transcript=transcript)


@router.get("/{session_id}/transcript")
async def get_transcript_version(
    request: Request, session_id: str, version: str = ORIGINAL_VERSION
) -> list[TranscriptEvent]:
    """One transcript version's segments ("original" or a transcribe job id).

    Returns the version's diarized relabeling when one exists, its raw rows
    otherwise - same rule the canonical view applies to the original.
    """
    state = get_state(request)
    if await state.sessions.get(session_id) is None:
        raise HTTPException(status_code=HTTP_404_NOT_FOUND, detail="session not found")
    return variant_view(await state.transcripts.for_session(session_id), version)


@router.put("/{session_id}/speakers")
async def set_speaker_names(
    request: Request, session_id: str, body: SpeakerNamesUpdate
) -> OkResponse:
    """Set the per-session speaker rename map (applied in the transcript view + exports)."""
    state = get_state(request)
    if await state.sessions.get(session_id) is None:
        raise HTTPException(status_code=HTTP_404_NOT_FOUND, detail="session not found")
    await state.sessions.set_speaker_names(session_id, body.names)
    return OkResponse()


@router.post("/{session_id}/summarize")
async def summarize_session(
    request: Request, session_id: str, body: SummarizeRequest
) -> SummarizeResult:
    """Summarize a session's transcript with the chosen LLM provider + model."""
    state = get_state(request)
    session = await state.sessions.get(session_id)
    if session is None:
        raise HTTPException(status_code=HTTP_404_NOT_FOUND, detail="session not found")
    provider = await state.providers.get(body.provider_id)
    if provider is None:
        raise HTTPException(status_code=HTTP_404_NOT_FOUND, detail="provider not found")
    if provider.kind is not ProviderKind.OPENAI_CHAT:
        raise HTTPException(
            status_code=HTTP_400_BAD_REQUEST, detail="provider is not an LLM provider"
        )
    events = relabel_speakers(
        canonical_transcript(await state.transcripts.for_session(session_id)),
        session.speaker_names,
    )
    if not events:
        raise HTTPException(status_code=HTTP_400_BAD_REQUEST, detail="session has no transcript")
    api_key = state.secrets.get(provider.auth_ref) if provider.auth_ref else None
    # Resolve the model the same way summarize_transcript does, so the stored
    # provenance records what actually ran, not just what was requested.
    chosen_model = body.model or provider.model or DEFAULT_MODEL
    defaults = await load_action_defaults(state)
    try:
        summary = await summarize_transcript(
            config=provider,
            api_key=api_key,
            model=body.model,
            transcript=to_txt(session, events),
            system_prompt=defaults.summarize_prompt or None,
        )
    except LLMError as exc:
        raise HTTPException(status_code=HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    await state.sessions.set_summary(
        session_id, summary, provider_id=provider.id, model=chosen_model
    )
    return SummarizeResult(summary=summary)


@router.get("/{session_id}/export")
async def export_session(request: Request, session_id: str, fmt: str = "txt") -> Response:
    """Export a session's transcript as txt/md/srt/vtt/json."""
    exporter = EXPORTERS.get(fmt)
    if exporter is None:
        raise HTTPException(status_code=HTTP_404_NOT_FOUND, detail=f"unknown format {fmt!r}")
    render, media_type, ext = exporter
    state = get_state(request)
    session = await state.sessions.get(session_id)
    if session is None:
        raise HTTPException(status_code=HTTP_404_NOT_FOUND, detail="session not found")
    transcript = relabel_speakers(
        canonical_transcript(await state.transcripts.for_session(session_id)),
        session.speaker_names,
    )
    body = render(session, transcript)
    return Response(
        content=body,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{session_id}.{ext}"'},
    )


@router.get("/{session_id}/audio")
async def download_session_audio(request: Request, session_id: str) -> FileResponse:
    """Download the stored session audio (WAV)."""
    state = get_state(request)
    session = await state.sessions.get(session_id)
    if session is None:
        raise HTTPException(status_code=HTTP_404_NOT_FOUND, detail="session not found")
    if not state.audio_store.exists(session_id):
        raise HTTPException(status_code=HTTP_404_NOT_FOUND, detail="no audio for session")
    return FileResponse(
        state.audio_store.wav_path(session_id),
        media_type="audio/wav",
        filename=f"{session_id}.wav",
    )


@router.post("/delete")
async def delete_sessions(request: Request, body: SessionIds) -> OkResponse:
    """Delete the given sessions, including their transcript and stored audio."""
    state = get_state(request)
    active = state.manager.current_session_id()
    for session_id in body.ids:
        if session_id == active:
            continue  # never delete the running session
        await state.transcripts.delete_session(session_id)
        state.audio_store.delete(session_id)
        await state.sessions.delete(session_id)
    return OkResponse()


@router.post("/merge")
async def merge_sessions(request: Request, body: SessionIds) -> Session:
    """Merge the selected sessions' transcripts - and audio - into a new session.

    Parts are concatenated oldest-first, each part's timestamps offset so they run
    back-to-back; speaker rename maps are unioned; originals are left intact. When
    every source has stored audio (at one shared sample rate), the WAVs and
    utterance indexes are concatenated too, so the merged session can be
    re-processed, re-diarized, and downloaded like any other.
    """
    state = get_state(request)
    sources = [s for s in [await state.sessions.get(i) for i in body.ids] if s is not None]
    if len(sources) < _MERGE_MIN_SESSIONS:
        raise HTTPException(status_code=HTTP_409_CONFLICT, detail="merge needs at least 2 sessions")
    # The running session's WAV + index now exist (and grow) throughout capture,
    # so without this guard a merge would snapshot a torn copy of live audio.
    if any(s.id == state.manager.current_session_id() for s in sources):
        raise HTTPException(status_code=HTTP_409_CONFLICT, detail="cannot merge a running session")
    sources.sort(key=lambda s: s.started_at)

    merged_id = uuid.uuid4().hex

    def _merge_audio() -> list[float] | None:
        """Concatenate source audio; per-part durations, or None when impossible."""
        if not all(state.audio_store.exists(s.id) for s in sources):
            return None
        try:
            state.audio_store.merge([s.id for s in sources], merged_id)
        except ValueError:  # e.g. mixed sample rates - merge the transcripts only
            return None
        return [state.audio_store.duration_s(s.id) for s in sources]

    # Blocking file I/O (copying whole session WAVs) off the event loop.
    durations = await asyncio.to_thread(_merge_audio)

    merged = Session(
        id=merged_id,
        status=SessionStatus.COMPLETED,
        started_at=sources[0].started_at,
        campaign_id=sources[0].campaign_id,
        primary_provider=sources[0].primary_provider,
        diarization=sources[0].diarization,
        audio_path=str(state.audio_store.wav_path(merged_id)) if durations is not None else None,
    )
    await state.sessions.create(merged)

    names: dict[str, str] = {}
    offset = 0.0
    for i, src in enumerate(sources):
        events = canonical_transcript(await state.transcripts.for_session(src.id))
        for event in events:
            shifted = rebase_transcript(event, -offset)  # negative offset shifts forward
            await state.transcripts.add(shifted.model_copy(update={"session_id": merged_id}))
        if durations is not None:
            # With merged audio, parts advance by their audio length so the
            # transcript stays aligned with the concatenated WAV.
            offset += durations[i]
        else:
            offset += max((event.end_ts for event in events), default=0.0)
        for label, name in src.speaker_names.items():
            names.setdefault(label, name)
    if names:
        await state.sessions.set_speaker_names(merged_id, names)

    return await state.sessions.get(merged_id) or merged
