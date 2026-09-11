"""Video-generation routes: model catalog, scene conversion, enqueue, status, playback.

The generation itself is asynchronous (minutes), so ``POST /api/video`` returns
a 202 with the job row and the client polls ``GET /api/video?session_id=…``.
See :mod:`loreline.video.jobs`.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.exceptions import HTTPException
from fastapi.responses import FileResponse
from starlette.status import (
    HTTP_400_BAD_REQUEST,
    HTTP_404_NOT_FOUND,
    HTTP_409_CONFLICT,
    HTTP_502_BAD_GATEWAY,
)

from loreline.llm import LLMError, summarize_transcript
from loreline.models import JobStatus, VideoJob, VideoModelInfo
from loreline.video import (
    EmptyPromptError,
    ProviderNotFoundError,
    ProviderNotVideoCapableError,
    SessionNotFoundError,
    supports_video,
)
from loreline.web.auth import require_auth
from loreline.web.deps import get_state, load_action_defaults
from loreline.web.generation import llm_target, scene_prompt
from loreline.web.schemas import OkResponse, SceneRequest, SceneResult, VideoGenerateRequest

router = APIRouter(prefix="/api/video", tags=["video"], dependencies=[Depends(require_auth)])


@router.get("/models")
async def video_models(request: Request, provider_id: str) -> list[VideoModelInfo]:
    """Video models a provider offers, with the parameters each one accepts.

    Drives the generate dialog's controls: durations, resolutions and aspect
    ratios differ per model, so the form is built from this rather than from a
    fixed list. Best-effort - an unreachable provider yields an empty list, and
    the dialog says so instead of failing the page.
    """
    state = get_state(request)
    provider = await state.providers.get(provider_id)
    if provider is None:
        raise HTTPException(status_code=HTTP_404_NOT_FOUND, detail="provider not found")
    if not supports_video(provider.kind):
        raise HTTPException(
            status_code=HTTP_400_BAD_REQUEST, detail="provider cannot generate video"
        )
    return await state.video.list_models(provider)


@router.post("", status_code=202)
async def enqueue_video(request: Request, body: VideoGenerateRequest) -> VideoJob:
    """Start a video generation; returns immediately with a queued job."""
    manager = get_state(request).video
    try:
        return await manager.enqueue(body)
    except (SessionNotFoundError, ProviderNotFoundError) as exc:
        raise HTTPException(status_code=HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ProviderNotVideoCapableError as exc:
        raise HTTPException(status_code=HTTP_409_CONFLICT, detail=str(exc)) from exc
    except EmptyPromptError as exc:
        raise HTTPException(status_code=HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.post("/scene")
async def write_video_scene(request: Request, body: SceneRequest) -> SceneResult:
    """Condense a recap into the single scene a video model can render.

    The dialog seeds its prompt from the session summary, which is a chapter: a
    video model renders a few seconds of one shot and has no use for the other
    twenty minutes. Rewriting it by hand was the only way out, and the dialog's
    own warning said as much without offering anything.

    Foreground, unlike the generation it feeds: it is one short completion, and
    the answer has to land in the box where it can be read and edited before
    any money is spent on a video. So it returns the scene rather than a job.
    """
    text = body.text.strip()
    if not text:
        raise HTTPException(status_code=HTTP_400_BAD_REQUEST, detail="there is no text to convert")
    state = get_state(request)
    provider, api_key = await llm_target(state, body.provider_id)
    defaults = await load_action_defaults(state)
    try:
        scene = await summarize_transcript(
            config=provider,
            api_key=api_key,
            model=body.model,
            transcript=text,
            system_prompt=await scene_prompt(state, body.style),
            instruction="Turn this session recap into one scene:",
            reasoning_effort=body.reasoning_effort or defaults.summarize_reasoning_effort or None,
        )
    except LLMError as exc:
        raise HTTPException(status_code=HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    return SceneResult(scene=scene.strip(), model=body.model)


@router.get("")
async def list_video_jobs(request: Request, session_id: str) -> list[VideoJob]:
    """List a session's video jobs, newest first."""
    return await get_state(request).video_jobs.for_session(session_id)


@router.get("/{job_id}")
async def get_video_job(request: Request, job_id: str) -> VideoJob:
    """Return a single video job (the client polls this while it runs)."""
    job = await get_state(request).video_jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=HTTP_404_NOT_FOUND, detail="job not found")
    return job


@router.get("/{job_id}/content")
async def get_video_content(request: Request, job_id: str) -> FileResponse:
    """Serve a finished job's video file for playback/download.

    Served from local storage rather than redirecting upstream: OpenRouter's
    result URLs expire, and the whole point of downloading the bytes at job
    completion was that the video keeps working afterwards.
    """
    state = get_state(request)
    job = await state.video_jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=HTTP_404_NOT_FOUND, detail="job not found")
    if job.status is not JobStatus.DONE:
        raise HTTPException(status_code=HTTP_409_CONFLICT, detail="video is not ready")
    if not state.video_store.exists(job_id):
        raise HTTPException(status_code=HTTP_404_NOT_FOUND, detail="video file is missing")
    return FileResponse(
        state.video_store.path(job_id),
        media_type="video/mp4",
        filename=f"{job.session_id}-{job.id}.mp4",
    )


@router.delete("/{job_id}")
async def delete_video_job(request: Request, job_id: str) -> OkResponse:
    """Delete a video job and its stored file."""
    state = get_state(request)
    job = await state.video_jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=HTTP_404_NOT_FOUND, detail="job not found")
    await state.video.delete(job_id)
    return OkResponse()
