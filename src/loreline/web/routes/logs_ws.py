"""WebSocket route streaming live service logs."""

from __future__ import annotations

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from starlette.status import WS_1008_POLICY_VIOLATION

from loreline.web.auth import COOKIE_NAME, auth_enabled, verify_token
from loreline.web.ws_util import stream_until_disconnected

router = APIRouter(tags=["ws"])


@router.websocket("/ws/logs")
async def logs_ws(ws: WebSocket) -> None:
    """Replay buffered log lines, then stream new ones live."""
    settings = ws.app.state.ctx.settings
    if auth_enabled(settings):
        token = ws.cookies.get(COOKIE_NAME)
        if not token or not verify_token(token, settings):
            await ws.close(code=WS_1008_POLICY_VIOLATION)
            return

    await ws.accept()
    broadcaster = ws.app.state.ctx.log_broadcaster
    try:
        # Subscribe before snapshotting history so no line slips through the gap;
        # the seq id then de-duplicates lines that are in both.
        async with broadcaster.bus.subscribe() as stream:
            history = broadcaster.history()
            last_seq = history[-1][0] if history else 0
            for _seq, line in history:
                await ws.send_text(line)
            async for seq, line in stream_until_disconnected(ws, stream):
                if seq <= last_seq:
                    continue
                await ws.send_text(line)
    except WebSocketDisconnect:
        return
