"""Re-processing routes: enqueue jobs, query their status, stop them early."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.exceptions import HTTPException
from starlette.status import (
    HTTP_404_NOT_FOUND,
    HTTP_409_CONFLICT,
    HTTP_503_SERVICE_UNAVAILABLE,
)

from loreline.models import ReprocessJob
from loreline.reprocess import (
    AudioMissingError,
    DiarizerUnreachableError,
    JobNotCancellableError,
    JobNotFoundError,
    ProviderNotFoundError,
    SessionNotFoundError,
    TargetNotFoundError,
)
from loreline.web.auth import require_auth
from loreline.web.deps import get_reprocess, get_state
from loreline.web.schemas import ReprocessRequest

router = APIRouter(
    prefix="/api/reprocess", tags=["reprocess"], dependencies=[Depends(require_auth)]
)


@router.post("", status_code=202)
async def enqueue_reprocess(request: Request, body: ReprocessRequest) -> ReprocessJob:
    """Enqueue a post-session re-processing job.

    503 for a diarizer that is not answering, because that is what it means:
    the request is fine and the machine it needs is not there. It is also the
    one refusal here that clears up by itself, so the message names the
    endpoint and invites another press rather than describing a bad request.
    """
    manager = get_reprocess(request)
    try:
        return await manager.enqueue(body)
    except (SessionNotFoundError, ProviderNotFoundError, TargetNotFoundError) as exc:
        raise HTTPException(status_code=HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except AudioMissingError as exc:
        raise HTTPException(status_code=HTTP_409_CONFLICT, detail=str(exc)) from exc
    except DiarizerUnreachableError as exc:
        raise HTTPException(status_code=HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc


@router.post("/{job_id}/cancel")
async def cancel_reprocess_job(request: Request, job_id: str) -> ReprocessJob:
    """Stop a queued or running job, keeping every segment it already wrote.

    409, naming the state it finished in, for a job that is already over. That
    is a race the caller can genuinely lose rather than a mistake: the session
    page polls every 1.5s, so a run can reach the end between the poll that
    drew the Cancel button and the press that arrives here, and "too late, it
    is done" is a different answer from "too late, it failed".

    The job comes back as it stands the moment the request is answered, which
    is not always its final state: a re-transcription stops at the end of the
    utterance it is on (see ``ReprocessManager.cancel``), so this can honestly
    answer "running" for a moment longer. The page keeps polling while the row
    says so and settles it without a second press.
    """
    manager = get_reprocess(request)
    try:
        return await manager.cancel(job_id)
    except JobNotFoundError as exc:
        raise HTTPException(status_code=HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except JobNotCancellableError as exc:
        raise HTTPException(status_code=HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.get("/{job_id}")
async def get_reprocess_job(request: Request, job_id: str) -> ReprocessJob:
    """Return a single re-processing job."""
    job = await get_state(request).reprocess_jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=HTTP_404_NOT_FOUND, detail="job not found")
    return job


@router.get("")
async def list_reprocess_jobs(request: Request, session_id: str) -> list[ReprocessJob]:
    """List re-processing jobs for a session, newest first."""
    return await get_state(request).reprocess_jobs.for_session(session_id)
