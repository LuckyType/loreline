"""WebSocket route streaming live service logs."""

from __future__ import annotations

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from starlette.status import WS_1008_POLICY_VIOLATION

from loreline.web.auth import COOKIE_NAME, auth_enabled, verify_token
from loreline.web.ws_util import stream_until_disconnected

router = APIRouter(tags=["ws"])


@router.websocket("/ws/logs")
async def logs_ws(ws: WebSocket) -> None:
    """Replay the running capture's buffered log lines, then stream new ones.

    Scoped to the capture that is running *right now*, and silent when none is:
    this feed sits next to the transcript on the dashboard, where every line is
    read as "this is what the microphone is doing". A re-transcription of last
    week's session scrolling through it says the opposite of the truth about
    the recording in progress. The lines are not lost - every session keeps its
    own log file per transcript version (see ``loreline.persistence.LogStore``),
    which is where a finished run is read back from.
    """
    settings = ws.app.state.ctx.settings
    if auth_enabled(settings):
        token = ws.cookies.get(COOKIE_NAME)
        if not token or not verify_token(token, settings):
            await ws.close(code=WS_1008_POLICY_VIOLATION)
            return

    await ws.accept()
    ctx = ws.app.state.ctx
    broadcaster = ctx.log_broadcaster
    manager = ctx.manager
    try:
        # Subscribe before snapshotting history so no line slips through the gap;
        # the seq id then de-duplicates lines that are in both.
        async with broadcaster.bus.subscribe() as stream:
            history = broadcaster.history()
            last_seq = history[-1].seq if history else 0
            # Re-read per record rather than once: a session that starts (or
            # stops) while this socket is open changes what belongs here.
            for record in history:
                if record.is_capture_line(manager.current_session_id()):
                    await ws.send_text(record.line)
            async for record in stream_until_disconnected(ws, stream):
                if record.seq <= last_seq:
                    continue
                if not record.is_capture_line(manager.current_session_id()):
                    continue
                await ws.send_text(record.line)
    except WebSocketDisconnect:
        return
