"""Static file serving for the built SvelteKit SPA.

Serves the adapter-static output and falls back to ``index.html`` for client
side routes (paths that don't map to a real asset), so deep links like
``/sessions/<id>`` work on refresh. API and WebSocket routes are registered
before this mount and therefore take precedence.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from starlette.exceptions import HTTPException
from starlette.staticfiles import StaticFiles
from starlette.status import HTTP_404_NOT_FOUND

if TYPE_CHECKING:
    from starlette.responses import Response


_NO_FALLBACK_PREFIXES = ("api/", "ws/")


class SpaStaticFiles(StaticFiles):
    """StaticFiles that falls back to ``index.html`` on 404 (SPA routing).

    A path under ``api/`` or ``ws/`` that reaches this mount didn't match any
    registered route (an unknown/mistyped endpoint) - it must 404, not fall
    back to ``index.html``, or a bad request silently gets a "successful" HTML
    response instead of a real error the client can detect.
    """

    async def get_response(self, path: str, scope: object) -> Response:
        try:
            return await super().get_response(path, scope)  # type: ignore[arg-type]
        except HTTPException as exc:
            if exc.status_code == HTTP_404_NOT_FOUND and not path.startswith(_NO_FALLBACK_PREFIXES):
                return await super().get_response("index.html", scope)  # type: ignore[arg-type]
            raise


def spa_directory() -> Path | None:
    """Return the SPA build directory if it exists.

    Resolves ``frontend/build`` relative to the repository root (four parents up
    from this module: ``src/loreline/web/spa.py``).
    """
    root = Path(__file__).resolve().parents[3]
    build = root / "frontend" / "build"
    return build if (build / "index.html").exists() else None
