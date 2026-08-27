"""Re-processing routes: enqueue jobs + query their status."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.exceptions import HTTPException
from starlette.status import HTTP_404_NOT_FOUND, HTTP_409_CONFLICT

from loreline.models import ReprocessJob
from loreline.reprocess import (
    AudioMissingError,
    ProviderNotFoundError,
    SessionNotFoundError,
)
from loreline.web.auth import require_auth
from loreline.web.deps import get_reprocess, get_state
from loreline.web.schemas import ReprocessRequest

router = APIRouter(
    prefix="/api/reprocess", tags=["reprocess"], dependencies=[Depends(require_auth)]
)


@router.post("", status_code=202)
async def enqueue_reprocess(request: Request, body: ReprocessRequest) -> ReprocessJob:
    """Enqueue a post-session re-processing job."""
    manager = get_reprocess(request)
    try:
        return await manager.enqueue(body)
    except (SessionNotFoundError, ProviderNotFoundError) as exc:
        raise HTTPException(status_code=HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except AudioMissingError as exc:
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
