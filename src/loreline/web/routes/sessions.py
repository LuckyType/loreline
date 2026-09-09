"""Session lifecycle routes: start/stop + listing + transcript fetch + bulk ops."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Sequence
from typing import TYPE_CHECKING

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

from loreline.capabilities import supports
from loreline.export import (
    EXPORTERS,
    canonical_transcript,
    final_rows,
    has_version,
    relabel_speakers,
    to_txt,
    variant_view,
)
from loreline.llm import LLMError, summarize_transcript
from loreline.models import (
    ORIGINAL_VERSION,
    Interaction,
    JobStatus,
    Session,
    SessionStatus,
    TranscriptEvent,
    rebase_transcript,
)
from loreline.reprocess import (
    OriginalVersionError,
    VersionBusyError,
    VersionNotFoundError,
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
    VersionLogs,
)

if TYPE_CHECKING:
    from loreline.web.app import AppState

router = APIRouter(prefix="/api/session", tags=["sessions"], dependencies=[Depends(require_auth)])

_MERGE_MIN_SESSIONS = 2


def _version_rows(events: Sequence[TranscriptEvent], version: str) -> list[TranscriptEvent]:
    """One version's settled rows, or a 404 naming the version that was asked for.

    Every caller that turns a transcript into something a GM pays for or keeps
    reads through here, so the "which version" decision is made once. The 404
    is the point of it: falling back to the original for a version id that does
    not exist would return a plausible file under the wrong name, which is
    exactly the failure this replaced (see :func:`export_session`).
    """
    if not has_version(events, version):
        raise HTTPException(
            status_code=HTTP_404_NOT_FOUND,
            detail=f"unknown transcript version {version!r}",
        )
    return final_rows(variant_view(events, version))


async def _best_available_rows(state: AppState, session_id: str) -> list[TranscriptEvent]:
    """A session's best text: its newest completed re-transcription, else the original.

    "Newest that produced segments" rather than "newest": a job can finish
    having written nothing (a provider that answered with silence, a version
    whose rows were deleted afterwards), and an empty version is not an
    improvement on the capture, it is the loss of it.

    ``DONE`` and nothing else, so a CANCELLED run is skipped here however good
    the part of it that ran was. Its rows are kept and readable on purpose -
    that is what the GM stopped it to look at - but this picks the text a
    caller gets when it did not choose a version, and a transcript that covers
    the first ten minutes of a four-hour session is not it. The GM can still
    name that version explicitly; what they cannot do is be handed it silently.
    """
    events = await state.transcripts.for_session(session_id)
    for job in await state.reprocess_jobs.for_session(session_id):  # newest first
        if job.operation != "transcribe" or job.status is not JobStatus.DONE:
            continue
        rows = final_rows(variant_view(events, job.id))
        if rows:
            return rows
    return final_rows(canonical_transcript(events))


class SessionDetail(BaseModel):
    """A session plus its persisted transcript."""

    session: Session
    transcript: list[TranscriptEvent]
    # Length of the stored WAV, seconds - None when there is none. Computed on
    # every read rather than carried on Session itself: the WAV keeps growing
    # throughout a live capture, so a stored value would go stale, and it is
    # exactly what tells a real recording apart from an errored session's
    # bare-header WAV, which "audio_path is set" alone cannot.
    audio_duration_s: float | None = None


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
    audio_duration_s = None
    if session.audio_path and state.audio_store.exists(session_id):
        # Blocking file I/O (reads the WAV header only) off the event loop.
        audio_duration_s = await asyncio.to_thread(state.audio_store.duration_s, session_id)
    return SessionDetail(session=session, transcript=transcript, audio_duration_s=audio_duration_s)


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


@router.delete("/{session_id}/transcript")
async def delete_transcript_version(request: Request, session_id: str, version: str) -> OkResponse:
    """Delete one re-transcription version: its segments and its job rows.

    "original" is refused here, not merely hidden in the UI: it is the live
    capture and nothing can produce it again. See
    ``ReprocessManager.delete_version`` for what a version drags with it.
    """
    state = get_state(request)
    if await state.sessions.get(session_id) is None:
        raise HTTPException(status_code=HTTP_404_NOT_FOUND, detail="session not found")
    try:
        await state.reprocess.delete_version(session_id, version)
    except VersionNotFoundError as exc:
        raise HTTPException(status_code=HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except (OriginalVersionError, VersionBusyError) as exc:
        raise HTTPException(status_code=HTTP_409_CONFLICT, detail=str(exc)) from exc
    # The version's log file goes with its segments, the same way stored audio
    # goes with a deleted session: a log describing a transcript nobody can
    # read is only a file nobody will ever delete by hand.
    state.log_store.delete_version(session_id, version)
    return OkResponse()


@router.get("/{session_id}/logs")
async def get_version_logs(
    request: Request, session_id: str, version: str = ORIGINAL_VERSION
) -> VersionLogs:
    """One transcript version's stored log ("original" or a transcribe job id).

    These are the lines that produced that version, kept per version because
    the dashboard's ring buffer forgets them within minutes - and the run that
    matters most, the live capture, is the one that can never be repeated.
    """
    state = get_state(request)
    if await state.sessions.get(session_id) is None:
        raise HTTPException(status_code=HTTP_404_NOT_FOUND, detail="session not found")
    try:
        # Blocking file I/O off the event loop, like the other stored artifacts.
        logs = await asyncio.to_thread(state.log_store.read, session_id, version)
    except (OSError, ValueError) as exc:
        # Missing (a version produced before logs were stored, or one whose run
        # emitted nothing) and unreadable both mean the same thing to a caller.
        raise HTTPException(
            status_code=HTTP_404_NOT_FOUND, detail="no logs stored for this version"
        ) from exc
    return VersionLogs(session_id=session_id, version=version, logs=logs)


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
    """Summarize one transcript version with the chosen LLM provider + model.

    ``version`` names the version to summarize and defaults to the live
    capture, so a client that predates the field keeps getting what it always
    got. Everything else about the request is unchanged.

    It exists because summarizing was the most expensive way this app could be
    wrong: the route summarized the original with the version hard-coded, so a
    session whose live capture died half way through and was re-transcribed
    three times still fed the LLM the broken half-transcript - and the GM paid
    for a summary of a session that mostly is not in it.
    """
    state = get_state(request)
    session = await state.sessions.get(session_id)
    if session is None:
        raise HTTPException(status_code=HTTP_404_NOT_FOUND, detail="session not found")
    version = body.version or ORIGINAL_VERSION
    provider = await state.providers.get(body.provider_id)
    if provider is None:
        raise HTTPException(status_code=HTTP_404_NOT_FOUND, detail="provider not found")
    # Asked of the yaml per request, so a capabilities.reload() is honoured.
    if not supports(provider.kind, Interaction.SUMMARIZE):
        raise HTTPException(
            status_code=HTTP_400_BAD_REQUEST, detail="provider is not an LLM provider"
        )
    events = relabel_speakers(
        _version_rows(await state.transcripts.for_session(session_id), version),
        session.speaker_names,
    )
    if not events:
        raise HTTPException(status_code=HTTP_400_BAD_REQUEST, detail="session has no transcript")
    api_key = state.secrets.get(provider.auth_ref) if provider.auth_ref else None
    defaults = await load_action_defaults(state)
    try:
        summary = await summarize_transcript(
            config=provider,
            api_key=api_key,
            model=body.model,
            transcript=to_txt(session, events),
            # Request override first, else the stored default; blank means the
            # model decides for itself.
            reasoning_effort=body.reasoning_effort or defaults.summarize_reasoning_effort or None,
            system_prompt=defaults.summarize_prompt or None,
        )
    except LLMError as exc:
        raise HTTPException(status_code=HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    # The request's model is the only one there is, so what is recorded is by
    # construction what ran. This used to re-derive it here, duplicating the
    # chain inside summarize_transcript so the two could disagree.
    # The version goes in with the provider and the model: a summary is only
    # readable as evidence if the transcript it was made from can be named, and
    # with five versions on a session "the summary" is otherwise ambiguous.
    await state.sessions.set_summary(
        session_id, summary, provider_id=provider.id, model=body.model, version=version
    )
    return SummarizeResult(summary=summary)


@router.get("/{session_id}/export")
async def export_session(
    request: Request, session_id: str, fmt: str = "txt", version: str = ORIGINAL_VERSION
) -> Response:
    """Export one transcript version as txt/md/srt/vtt/json.

    ``version`` is the version the caller is looking at ("original" or a
    transcribe job id), defaulting to the live capture so an old bookmark still
    means what it meant.

    The route had no such parameter at all, and rendered the original with the
    version hard-coded. The whole point of re-transcribing a session with a
    better model is the file you get out of it, so a GM who selected a 1346-
    segment re-transcription and pressed Export got the 683-segment original
    back, in every format, with nothing on screen saying which one it was.

    An unknown version is a 404 rather than a fallback to the original, for the
    same reason a wrong file with no warning is worse than no file.
    """
    exporter = EXPORTERS.get(fmt)
    if exporter is None:
        raise HTTPException(status_code=HTTP_404_NOT_FOUND, detail=f"unknown format {fmt!r}")
    render, media_type, ext = exporter
    state = get_state(request)
    session = await state.sessions.get(session_id)
    if session is None:
        raise HTTPException(status_code=HTTP_404_NOT_FOUND, detail="session not found")
    transcript = relabel_speakers(
        _version_rows(await state.transcripts.for_session(session_id), version),
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
        state.log_store.delete_session(session_id)  # every version's log file
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

    Each source contributes its **newest completed re-transcription that
    produced segments**, falling back to its live capture when it has none (see
    :func:`_best_available_rows`). This used to take every source's original
    unconditionally, which threw away every re-transcription of every part: a
    GM who re-ran two half-sessions through a better model and then merged them
    got the two live captures back, with no way to tell from the merged row.

    The alternative considered and not taken was a per-source version picker in
    the request. It was rejected because the merge dialog does not ask and
    should not have to: a GM merging the two halves of one evening is saying
    "make this one session", not "and by the way use job 2680abb4 for the first
    half". Taking the best text available is what that sentence means, and the
    sources are left intact, so a merge made from the wrong version is undone by
    deleting one row rather than by recovering anything.
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

    parts = [await _best_available_rows(state, src.id) for src in sources]
    # How far each part advances the merged clock. With merged audio that is the
    # part's audio length, so the transcript stays aligned with the concatenated
    # WAV; without it, the part's own last word is all there is to go on.
    spans = (
        durations
        if durations is not None
        else [max((e.end_ts for e in rows), default=0.0) for rows in parts]
    )

    merged = Session(
        id=merged_id,
        status=SessionStatus.COMPLETED,
        started_at=sources[0].started_at,
        # A merged session never had a stop button pressed, so nothing else was
        # ever going to fill this in, and it was left null: the session page
        # computes its duration only when ended_at is set, so a merge showed a
        # start time and no length while the player under it held 37 minutes of
        # audio. The merged timeline is what it runs to.
        ended_at=sources[0].started_at + sum(spans),
        campaign_id=sources[0].campaign_id,
        primary_provider=sources[0].primary_provider,
        diarization=sources[0].diarization,
        audio_path=str(state.audio_store.wav_path(merged_id)) if durations is not None else None,
        # What this row was made from, oldest first. Without it a merge is
        # indistinguishable in the history list from its oldest source: same
        # start time, same status, same provider, and the only way to tell them
        # apart is to open both.
        merged_from=[src.id for src in sources],
    )
    await state.sessions.create(merged)

    names: dict[str, str] = {}
    offset = 0.0
    for src, rows, span in zip(sources, parts, spans, strict=True):
        for event in rows:
            shifted = rebase_transcript(event, -offset)  # negative offset shifts forward
            # The turn id goes with the source session. It is a replace key for
            # a turn still being revised, and these are settled copies that
            # nothing will revise again; carrying it over would only let two
            # merged sessions' turns collide on it.
            # The source tag goes too: a part's rows may come from a re-
            # transcription, and a `reprocess:<job id>` tag on the merged row
            # would name a job belonging to a different session - a version the
            # merged session does not have. They are the merged session's live
            # text now, which is what an untagged row means.
            await state.transcripts.add(
                shifted.model_copy(
                    update={
                        "session_id": merged_id,
                        "turn_id": None,
                        "source": src.primary_provider or src.id,
                    }
                )
            )
        offset += span
        for label, name in src.speaker_names.items():
            names.setdefault(label, name)
    if names:
        await state.sessions.set_speaker_names(merged_id, names)

    return await state.sessions.get(merged_id) or merged
