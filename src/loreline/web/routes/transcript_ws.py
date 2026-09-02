"""WebSocket route streaming live transcript events."""

from __future__ import annotations

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from starlette.status import WS_1008_POLICY_VIOLATION

from loreline.models import DIARIZE_SOURCE_PREFIX, REPROCESS_SOURCE_PREFIX, TranscriptEvent
from loreline.web.auth import COOKIE_NAME, auth_enabled, verify_token
from loreline.web.ws_util import stream_until_disconnected

router = APIRouter(tags=["ws"])


def _wanted(event: TranscriptEvent, session_id: str | None, active_id: str | None) -> bool:
    """Whether a subscriber should be sent ``event``.

    With ``session_id`` the client asked for one session and gets all of its
    versions, the live capture and every re-processing run alike; it routes
    them by ``event.source``, which names the version each belongs to.

    Without it the client is the dashboard's live view, where an event means
    "this was just said into the microphone". Only the running capture belongs
    there: a re-transcription replaying stored audio would otherwise interleave
    a past session's words with the present one's, at whatever speed the
    provider happens to return them.
    """
    if session_id is not None:
        return event.session_id == session_id
    if active_id is None or event.session_id != active_id:
        return False
    return not event.source.startswith((REPROCESS_SOURCE_PREFIX, DIARIZE_SOURCE_PREFIX))


@router.websocket("/ws/transcript")
async def transcript_ws(ws: WebSocket) -> None:
    """Push published ``TranscriptEvent``s to the client as JSON.

    ``session_id`` selects one session; without it the socket carries the
    running capture only (see :func:`_wanted`).
    """
    settings = ws.app.state.ctx.settings
    if auth_enabled(settings):
        token = ws.cookies.get(COOKIE_NAME)
        if not token or not verify_token(token, settings):
            await ws.close(code=WS_1008_POLICY_VIOLATION)
            return

    session_id = ws.query_params.get("session_id")
    await ws.accept()
    manager = ws.app.state.ctx.manager
    bus = manager.transcript_bus
    try:
        async with bus.subscribe() as stream:
            async for event in stream_until_disconnected(ws, stream):
                if not _wanted(event, session_id, manager.current_session_id()):
                    continue
                await ws.send_text(event.model_dump_json())
    except WebSocketDisconnect:
        return
