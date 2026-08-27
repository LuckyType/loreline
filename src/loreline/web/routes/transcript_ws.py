"""WebSocket route streaming live transcript events."""

from __future__ import annotations

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from starlette.status import WS_1008_POLICY_VIOLATION

from loreline.web.auth import COOKIE_NAME, auth_enabled, verify_token
from loreline.web.ws_util import stream_until_disconnected

router = APIRouter(tags=["ws"])


@router.websocket("/ws/transcript")
async def transcript_ws(ws: WebSocket) -> None:
    """Push every published ``TranscriptEvent`` to the client as JSON."""
    settings = ws.app.state.ctx.settings
    if auth_enabled(settings):
        token = ws.cookies.get(COOKIE_NAME)
        if not token or not verify_token(token, settings):
            await ws.close(code=WS_1008_POLICY_VIOLATION)
            return

    session_id = ws.query_params.get("session_id")
    await ws.accept()
    bus = ws.app.state.ctx.manager.transcript_bus
    try:
        async with bus.subscribe() as stream:
            async for event in stream_until_disconnected(ws, stream):
                if session_id and event.session_id != session_id:
                    continue
                await ws.send_text(event.model_dump_json())
    except WebSocketDisconnect:
        return
