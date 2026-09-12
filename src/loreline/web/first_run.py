"""The gate that keeps an unclaimed instance from serving anything but its setup.

An unclaimed instance has no password, so ``require_auth`` is a no-op on every
route: without this, a box that had just come up for the first time would serve
its transcripts, its provider keys and its session controls to anything that
could reach the port, for as long as it took somebody to open the wizard. The
gate is therefore a whole-app refusal rather than a dependency added route by
route, because the failure mode of the second is a route that forgot to add it.

It is written as raw ASGI rather than as a ``BaseHTTPMiddleware`` because the
WebSocket routes need refusing too, and ``BaseHTTPMiddleware`` never sees them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from starlette.responses import JSONResponse
from starlette.status import HTTP_403_FORBIDDEN, WS_1008_POLICY_VIOLATION

from loreline.web.setup import setup_required

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Receive, Scope, Send

    from loreline.settings import Settings

#: Everything the app serves that is not the browser application itself. The
#: SPA is deliberately not in here: the wizard is a page of it, so an unclaimed
#: instance has to serve its own front end to be claimable at all. ``/docs``
#: and ``/openapi.json`` are, because "only the setup routes answer" is easier
#: to hold to than a list of what is harmless to expose.
_GUARDED_PREFIXES = ("/api/", "/ws/", "/docs", "/redoc", "/openapi.json")

#: The two things an unclaimed instance must still answer: the wizard's own
#: routes, and the liveness probe an orchestrator polls before anybody has
#: opened a browser at all.
_ALLOWED_PREFIXES = ("/api/setup/", "/api/system/livez")

REFUSAL = (
    "this Loreline has not been set up yet: open it in a browser and enter the "
    "setup code from its startup log to claim it"
)


def is_blocked(path: str) -> bool:
    """Whether this path is one an unclaimed instance must refuse."""
    if path.startswith(_ALLOWED_PREFIXES):
        return False
    return path.startswith(_GUARDED_PREFIXES)


class FirstRunGate:
    """Refuse every API and socket route while the instance is unclaimed.

    Holds the live ``Settings`` object rather than reading it per request from
    the app state, because the claim mutates that same object in place: the
    request that sets the password is the request after which this gate opens,
    with no restart in between.
    """

    def __init__(self, app: ASGIApp, settings: Settings) -> None:
        self._app = app
        self._settings = settings

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket") or not setup_required(self._settings):
            await self._app(scope, receive, send)
            return
        if not is_blocked(scope.get("path", "")):
            await self._app(scope, receive, send)
            return
        if scope["type"] == "websocket":
            # A close sent before an accept is how ASGI says "refused"; the
            # server turns it into an HTTP 403 for the handshake.
            await receive()
            await send({"type": "websocket.close", "code": WS_1008_POLICY_VIOLATION})
            return
        response = JSONResponse({"detail": REFUSAL}, status_code=HTTP_403_FORBIDDEN)
        await response(scope, receive, send)
