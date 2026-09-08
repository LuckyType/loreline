"""WebSocket route streaming live service logs."""

from __future__ import annotations

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from starlette.status import WS_1008_POLICY_VIOLATION

from loreline.logbus import LogRecord
from loreline.web.auth import COOKIE_NAME, auth_enabled, verify_token
from loreline.web.ws_util import stream_until_disconnected

router = APIRouter(tags=["ws"])


@router.websocket("/ws/logs")
async def logs_ws(ws: WebSocket) -> None:
    """Replay the live session's buffered log lines, then stream new ones.

    Scoped to the one session the live view is about, and silent when there is
    none: this feed sits next to the transcript on the dashboard, where every
    line is read as "this is what the microphone is doing". A re-transcription
    of last week's session scrolling through it says the opposite of the truth
    about the recording in progress. The lines are not lost - every session
    keeps its own log file per transcript version (see
    ``loreline.persistence.LogStore``), which is where a finished run is read
    back from.

    That scope runs to the end of the teardown, not to the end of the
    recording: ``SessionManager.live_view_session_id`` outlives the runtime by
    exactly the stretch in which a session reports what it is doing about being
    stopped, which is the stretch the GM is staring at this pane through.
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

    def keep(record: LogRecord) -> bool:
        """Whether this line belongs in the pane, asked as it is written.

        Handed to the bus instead of being run in the loop below, so the answer
        is the one that held when the line was written rather than the one that
        happens to hold when this task is next scheduled. For the last lines of
        a teardown the two differ: they are written while the session is still
        the one on show and read a moment after it has stopped being (see
        :meth:`EventBus.subscribe`). Either way the live view is re-read per
        line, because a session that starts - or finishes ending - while this
        socket is open changes what belongs here.
        """
        return record.is_capture_line(manager.live_view_session_id())

    try:
        # Subscribe before snapshotting history so no line slips through the gap;
        # the seq id then de-duplicates lines that are in both.
        async with broadcaster.bus.subscribe(wanted=keep) as stream:
            history = broadcaster.history()
            last_seq = history[-1].seq if history else 0
            # History is judged against the view as it is *now*: these lines are
            # already written, and what a socket connecting this moment should
            # be shown is whatever the panes are about this moment.
            for record in history:
                if record.is_capture_line(manager.live_view_session_id()):
                    await ws.send_text(record.line)
            async for record in stream_until_disconnected(ws, stream):
                if record.seq <= last_seq:
                    continue
                await ws.send_text(record.line)
    except WebSocketDisconnect:
        return
