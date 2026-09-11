"""Session lifecycle routes: start/stop + listing + transcript fetch + bulk ops."""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import AsyncIterator, Sequence
from typing import TYPE_CHECKING, Annotated, NamedTuple

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.exceptions import HTTPException
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field, ValidationError
from starlette.status import (
    HTTP_400_BAD_REQUEST,
    HTTP_404_NOT_FOUND,
    HTTP_409_CONFLICT,
    HTTP_413_CONTENT_TOO_LARGE,
    HTTP_422_UNPROCESSABLE_CONTENT,
    HTTP_502_BAD_GATEWAY,
    HTTP_503_SERVICE_UNAVAILABLE,
)

from loreline.audio.decode import IMPORTABLE_SUFFIXES, DecodeError
from loreline.export import (
    EXPORTERS,
    canonical_transcript,
    final_rows,
    has_version,
    relabel_speakers,
    to_txt,
    variant_view,
)
from loreline.llm import LLMError, extract_entities, summarize_transcript
from loreline.models import (
    DOCUMENT_EXTRACTION,
    DOCUMENT_RECAP,
    ORIGINAL_VERSION,
    JobStatus,
    Session,
    SessionDocument,
    SessionExtraction,
    SessionStatus,
    TranscriptEvent,
    rebase_transcript,
)
from loreline.reprocess import (
    DiarizerUnreachableError,
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
from loreline.session.importing import (
    IndexingUnavailableError,
    UploadTooLargeError,
    import_recording,
)
from loreline.web.auth import require_auth
from loreline.web.deps import get_manager, get_state, load_action_defaults
from loreline.web.generation import cast_note, llm_target, recap_prompt
from loreline.web.routes.audio import INPUT_DEVICE_KEY, parse_device
from loreline.web.schemas import (
    CampaignAssignment,
    GenerateRequest,
    ImportedSession,
    ImportTranscribeOptions,
    OkResponse,
    ReprocessRequest,
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

# How much of an upload is read per iteration. Big enough that a 500 MB
# recording is not 500,000 awaits, small enough that nothing here ever holds a
# meaningful slice of the file in memory.
_UPLOAD_CHUNK = 1 << 20


async def _require_version(
    state: AppState, session_id: str, events: Sequence[TranscriptEvent], version: str
) -> None:
    """Raise a 404 naming ``version`` unless it is a transcript this session has.

    :func:`has_version` answers from the rows, and for a version that has run
    the rows are the version. They are not the whole story for a
    re-transcription still in flight: the session page lists it and lets the
    GM select it the moment it is queued, and it has no rows until its first
    utterance comes back. Guarding on the rows alone turned that selection
    into a 404 for a version that exists. So a transcribe job row for this
    session counts too; a job id from another session does not, any more
    than a typo does.
    """
    if has_version(events, version):
        return
    job = await state.reprocess_jobs.get(version)
    if job is not None and job.session_id == session_id and job.operation == "transcribe":
        return
    raise HTTPException(
        status_code=HTTP_404_NOT_FOUND,
        detail=f"unknown transcript version {version!r}",
    )


async def _version_rows(state: AppState, session_id: str, version: str) -> list[TranscriptEvent]:
    """One version's settled rows, or a 404 naming the version that was asked for.

    Every caller that turns a transcript into something a GM pays for or keeps
    reads through here, so the "which version" decision is made once. The 404
    is the point of it: falling back to the original for a version id that does
    not exist would return a plausible file under the wrong name, which is
    exactly the failure this replaced (see :func:`export_session`).
    """
    events = await state.transcripts.for_session(session_id)
    await _require_version(state, session_id, events, version)
    return final_rows(variant_view(events, version))


class _BestRows(NamedTuple):
    """What one merge part contributes: its rows, and the provider that produced them."""

    rows: list[TranscriptEvent]
    provider_id: str


async def _best_available_rows(state: AppState, session: Session) -> _BestRows:
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

    The provider comes back with the rows because the two are decided
    together. A merged row is stamped with the provider that produced it (see
    :func:`merge_sessions`), and for a re-transcription that is the job's
    provider, not the session's: a session captured with A and re-transcribed
    with B used to land in the merge stamped A, naming a provider that never
    heard those words.
    """
    events = await state.transcripts.for_session(session.id)
    for job in await state.reprocess_jobs.for_session(session.id):  # newest first
        if job.operation != "transcribe" or job.status is not JobStatus.DONE:
            continue
        rows = final_rows(variant_view(events, job.id))
        if rows:
            return _BestRows(rows, job.provider_id)
    return _BestRows(
        final_rows(canonical_transcript(events)), session.primary_provider or session.id
    )


class SessionDetail(BaseModel):
    """A session plus its persisted transcript and its generated texts."""

    session: Session
    transcript: list[TranscriptEvent]
    # The recap and the extraction, when they exist. Served with the session
    # rather than behind a second call because the summary card draws all three
    # together: a page that fetched them separately would render a session that
    # has a recap as one that has none, for as long as the second request takes.
    documents: list[SessionDocument] = Field(default_factory=list[SessionDocument])
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


def _import_options(raw: str | None) -> ImportTranscribeOptions | None:
    """Parse the request's optional "transcribe now" block.

    It arrives as a JSON object in a form field because the rest of the request
    is a file: multipart carries flat values, and the block holds a
    ``diarization`` object, so there is no flat spelling of it that the dialog
    and this could both agree on. Absent and blank both mean "just store it".
    """
    text = (raw or "").strip()
    if not text:
        return None
    try:
        return ImportTranscribeOptions.model_validate_json(text)
    except ValidationError as exc:
        raise HTTPException(
            status_code=HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(
                "transcribe must be a JSON object naming a provider and a model: "
                f"{exc.errors(include_url=False)}"
            ),
        ) from exc


def _refuse_an_upload_that_cannot_fit(request: Request, limit: int) -> None:
    """Answer 413 before a single byte is read, when the client says how big it is.

    The body is still counted while it is written (see ``receive_upload``),
    which is what actually enforces the ceiling; this only spares both ends the
    minutes a doomed 3 GB upload would otherwise spend. The declared length
    covers the multipart framing as well as the file, which makes this a few
    hundred bytes stricter than the real check and never looser.
    """
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > limit:
        raise HTTPException(
            status_code=HTTP_413_CONTENT_TOO_LARGE,
            detail=f"this recording is larger than the {limit // (1024 * 1024)} MB import limit",
        )


async def _upload_chunks(file: UploadFile) -> AsyncIterator[bytes]:
    """The uploaded file, a chunk at a time."""
    while chunk := await file.read(_UPLOAD_CHUNK):
        yield chunk


async def _transcribe_the_import(
    state: AppState, session_id: str, options: ImportTranscribeOptions
) -> str:
    """Queue the import's first transcription, or undo the import.

    An ordinary re-processing job, which is the whole of ADR 0008: the session
    exists and has stored audio, so this is the same call the New transcription
    dialog makes on a captured session, and the version it produces is the
    import's transcript.

    A refusal here rolls the session back rather than leaving it behind with
    the error. The GM asked for one thing - a transcribed recording - and a
    half-done answer would have them upload the file a second time and be left
    with two sessions, one of which is silent. The provider is checked before
    the upload is read at all, so what reaches here is the rare late failure (a
    diarizer that went away between the two), not a typo.
    """
    try:
        job = await state.reprocess.enqueue(
            ReprocessRequest(
                session_id=session_id,
                provider_id=options.provider_id,
                model=options.model,
                use_glossary=options.use_glossary,
                diarization=options.diarization,
            )
        )
    except DiarizerUnreachableError as exc:
        await _erase_session(state, session_id)
        raise HTTPException(status_code=HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    except ValueError as exc:  # every other refusal enqueue makes
        await _erase_session(state, session_id)
        raise HTTPException(status_code=HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return job.id


@router.post("/import", status_code=201)
async def import_session_recording(
    request: Request,
    file: Annotated[
        UploadFile,
        File(
            description=(
                "The recording to import. Decoded from "
                f"{', '.join(IMPORTABLE_SUFFIXES)} and anything else ffmpeg reads."
            )
        ),
    ],
    started_at: Annotated[float | None, Form()] = None,
    campaign_id: Annotated[str | None, Form()] = None,
    transcribe: Annotated[str | None, Form()] = None,
) -> ImportedSession:
    """Import a recording made elsewhere as a session (multipart upload).

    This is the feature that lets a GM use Loreline with no box and no
    microphone: whatever they recorded on their phone becomes a session that
    every other feature works on unchanged, because it is stored as exactly
    what a capture stores (see ``docs/adr/0008``).

    ``started_at`` is epoch seconds and defaults to now - the dialog seeds it
    from the file's own modification time, which is the closest thing a
    recording carries to when the evening was. ``campaign_id`` names the
    campaign the session joins, and is checked like the provider below: an id
    no campaign answers to is the unresolvable string ``docs/adr/0009`` exists
    to end, and it would also decide which glossary the transcription in this
    same request runs with. ``transcribe`` is a JSON object (``provider_id``, ``model``,
    ``use_glossary``, ``diarization``) that starts the first transcription in
    the same request; leave it out to store the recording and decide later.

    The refusals, and why each is the code it is: 413 for a file past
    ``LORELINE_IMPORT_MAX_MB``, 422 for audio this server cannot decode - which
    includes the one failure that is about the box rather than the file, a
    format that needs ffmpeg where ffmpeg is not installed - and 503 when the
    speech detector that cuts a recording into utterances is unavailable, since
    that is the one a retry can win after somebody fixes the deployment.
    """
    state = get_state(request)
    options = _import_options(transcribe)
    if options is not None and await state.providers.get(options.provider_id) is None:
        # Before the upload rather than after it: a typo in the provider must
        # not cost the GM a ten minute upload to discover.
        raise HTTPException(
            status_code=HTTP_404_NOT_FOUND, detail=f"unknown provider {options.provider_id!r}"
        )
    campaign = (campaign_id or "").strip() or None
    if campaign is not None and await state.campaigns.get(campaign) is None:
        # Same check and the same reason as the provider above, and the same
        # one PUT /api/session/{id}/campaign makes.
        raise HTTPException(status_code=HTTP_404_NOT_FOUND, detail="campaign not found")
    _refuse_an_upload_that_cannot_fit(request, state.settings.import_max_bytes)
    try:
        session = await import_recording(
            _upload_chunks(file),
            name=file.filename or "recording",
            started_at=started_at,
            campaign_id=campaign,
            sessions=state.sessions,
            audio_store=state.audio_store,
            settings=state.settings,
        )
    except UploadTooLargeError as exc:
        raise HTTPException(status_code=HTTP_413_CONTENT_TOO_LARGE, detail=str(exc)) from exc
    except DecodeError as exc:
        raise HTTPException(status_code=HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
    except IndexingUnavailableError as exc:
        raise HTTPException(status_code=HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    job_id = await _transcribe_the_import(state, session.id, options) if options else None
    return ImportedSession(session=session, job_id=job_id)


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
    return SessionDetail(
        session=session,
        transcript=transcript,
        documents=await state.documents.for_session(session_id),
        audio_duration_s=audio_duration_s,
    )


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
    events = await state.transcripts.for_session(session_id)
    # A typo is a 404 here for the same reason it is on export: an empty
    # transcript looks exactly like a version that captured nothing, and this
    # route answered every misspelt version id with one.
    await _require_version(state, session_id, events, version)
    return variant_view(events, version)


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
    version = body.version or ORIGINAL_VERSION
    state, session, events = await _transcript_for_generation(request, session_id, version)
    # Which provider row may run this, and the key it runs with: the same
    # question the recap and the extraction ask, answered in one place (see
    # loreline.web.generation) rather than three times over.
    provider, api_key = await llm_target(state, body.provider_id)
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
            # Who is at this table, when the session is in a campaign that says
            # so. Blank otherwise, and blank changes nothing about the request.
            cast_line=await cast_note(state, session.campaign_id),
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


@router.put("/{session_id}/campaign")
async def set_session_campaign(
    request: Request, session_id: str, body: CampaignAssignment
) -> Session:
    """Put a session in a campaign, or take it out of one (null).

    An id no campaign answers to is a 404 rather than a stored string: that is
    exactly the state this feature exists to end - a ``campaign_id`` nothing
    can resolve, rendering as a raw hex id in the History table and reaching a
    glossary nobody can find.
    """
    state = get_state(request)
    session = await state.sessions.get(session_id)
    if session is None:
        raise HTTPException(status_code=HTTP_404_NOT_FOUND, detail="session not found")
    campaign_id = (body.campaign_id or "").strip() or None
    if campaign_id is not None and await state.campaigns.get(campaign_id) is None:
        raise HTTPException(status_code=HTTP_404_NOT_FOUND, detail="campaign not found")
    await state.campaigns.assign(session_id, campaign_id)
    return await state.sessions.get(session_id) or session


@router.post("/{session_id}/recap")
async def write_session_recap(
    request: Request, session_id: str, body: GenerateRequest
) -> SessionDocument:
    """Write the player-facing recap of one transcript version.

    The same call the summary makes, with different instructions and a
    different place to put the answer. They are two texts about one session
    because they are for two readers: a summary is the GM's index of what
    happened, a recap is what the table is told a week later, and a prompt that
    tries to be both produces a bulleted list of NPCs nobody reads aloud.

    Which instructions run is the campaign's, else the stored default, else the
    built-in text - see :func:`loreline.web.generation.recap_prompt`.
    """
    state, session, events = await _transcript_for_generation(request, session_id, body.version)
    provider, api_key = await llm_target(state, body.provider_id)
    defaults = await load_action_defaults(state)
    try:
        text = await summarize_transcript(
            config=provider,
            api_key=api_key,
            model=body.model,
            transcript=to_txt(session, events),
            system_prompt=await recap_prompt(state, session.campaign_id),
            instruction="Write the recap of this session, from its transcript:",
            cast_line=await cast_note(state, session.campaign_id),
            reasoning_effort=body.reasoning_effort or defaults.summarize_reasoning_effort or None,
        )
    except LLMError as exc:
        raise HTTPException(status_code=HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    return await _store_document(
        state, session_id, DOCUMENT_RECAP, text, provider.id, body.model, body.version
    )


@router.post("/{session_id}/extract")
async def extract_session_entities(
    request: Request, session_id: str, body: GenerateRequest
) -> SessionExtraction:
    """Extract the names this session used, as structured data.

    Stored as a document like the recap, and returned parsed rather than as the
    stored JSON string: the campaign page merges these across sessions, and a
    wire type the browser has to parse out of a string is a type the browser
    does not really have.
    """
    state, session, events = await _transcript_for_generation(request, session_id, body.version)
    provider, api_key = await llm_target(state, body.provider_id)
    defaults = await load_action_defaults(state)
    try:
        extraction = await extract_entities(
            config=provider,
            api_key=api_key,
            model=body.model,
            transcript=to_txt(session, events),
            # The generation this helps most: without it the pc/npc split is a
            # guess from how much each name was said.
            cast_line=await cast_note(state, session.campaign_id),
            reasoning_effort=body.reasoning_effort or defaults.summarize_reasoning_effort or None,
        )
    except LLMError as exc:
        raise HTTPException(status_code=HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    await _store_document(
        state,
        session_id,
        DOCUMENT_EXTRACTION,
        extraction.model_dump_json(),
        provider.id,
        body.model,
        body.version,
    )
    return extraction


async def _transcript_for_generation(
    request: Request, session_id: str, version: str | None
) -> tuple[AppState, Session, list[TranscriptEvent]]:
    """The session and the rows a generation will read, or the HTTP error.

    Shared by the recap and the extraction, and identical to what summarize
    does with the same request: the named version's settled rows with the
    session's speaker renames applied, 404 for a version this session does not
    have, 400 for a session with nothing in it. The three used to be one copy
    each, which is how summarize came to name its version and the others did
    not."""
    state = get_state(request)
    session = await state.sessions.get(session_id)
    if session is None:
        raise HTTPException(status_code=HTTP_404_NOT_FOUND, detail="session not found")
    events = relabel_speakers(
        await _version_rows(state, session_id, version or ORIGINAL_VERSION),
        session.speaker_names,
    )
    if not events:
        raise HTTPException(status_code=HTTP_400_BAD_REQUEST, detail="session has no transcript")
    return state, session, events


async def _store_document(
    state: AppState,
    session_id: str,
    kind: str,
    body: str,
    provider_id: str,
    model: str,
    version: str | None,
) -> SessionDocument:
    """Persist one generated text, replacing the previous one of its kind."""
    document = SessionDocument(
        session_id=session_id,
        kind=kind,
        body=body,
        provider_id=provider_id,
        model=model,
        version=version or ORIGINAL_VERSION,
        created_at=time.time(),
    )
    await state.documents.put_session_document(document)
    return document


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
        await _version_rows(state, session_id, version), session.speaker_names
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


async def _erase_session(state: AppState, session_id: str) -> None:
    """Remove one session and everything stored under its name.

    One function rather than a loop body, because two callers have to agree on
    what "gone" means: the delete endpoint, and the rollback an import does
    when the transcription it was asked for could not be started (see
    :func:`_transcribe_the_import`). A rollback that forgot the WAV would leave
    the largest artifact of all orphaned, with no row left to name it.
    """
    await state.transcripts.delete_session(session_id)
    state.audio_store.delete(session_id)
    state.log_store.delete_session(session_id)  # every version's log file
    # The video job rows would go with the session row anyway (the table
    # cascades), and that was the problem: a cascade takes no file with it, so
    # every generated .mp4 of a deleted session stayed on disk with nothing
    # left that named it.
    await state.video.delete_session(session_id)
    await state.sessions.delete(session_id)


@router.post("/delete")
async def delete_sessions(request: Request, body: SessionIds) -> OkResponse:
    """Delete the given sessions: transcript, stored audio, logs and generated videos."""
    state = get_state(request)
    active = state.manager.current_session_id()
    for session_id in body.ids:
        if session_id == active:
            continue  # never delete the running session
        await _erase_session(state, session_id)
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

    parts = [await _best_available_rows(state, src) for src in sources]
    # How far each part advances the merged clock. With merged audio that is the
    # part's audio length, so the transcript stays aligned with the concatenated
    # WAV; without it, the part's own last word is all there is to go on.
    spans = (
        durations
        if durations is not None
        else [max((e.end_ts for e in part.rows), default=0.0) for part in parts]
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
    for src, part, span in zip(sources, parts, spans, strict=True):
        for event in part.rows:
            shifted = rebase_transcript(event, -offset)  # negative offset shifts forward
            # The turn id goes with the source session. It is a replace key for
            # a turn still being revised, and these are settled copies that
            # nothing will revise again; carrying it over would only let two
            # merged sessions' turns collide on it. Dropping it is also what
            # keeps the (session, source, turn) upsert key out of the way:
            # NULL turn ids never match, so two parts stamped with the same
            # provider below append rather than overwrite each other.
            # The source tag goes too: a part's rows may come from a re-
            # transcription, and a `reprocess:<job id>` tag on the merged row
            # would name a job belonging to a different session - a version the
            # merged session does not have. They are the merged session's live
            # text now, which is what an untagged row means, and the provider
            # they are stamped with is the one that produced them: the job's
            # for a re-transcription, the capture's for an original.
            await state.transcripts.add(
                shifted.model_copy(
                    update={
                        "session_id": merged_id,
                        "turn_id": None,
                        "source": part.provider_id,
                    }
                )
            )
        offset += span
        for label, name in src.speaker_names.items():
            names.setdefault(label, name)
    if names:
        await state.sessions.set_speaker_names(merged_id, names)

    return await state.sessions.get(merged_id) or merged
