"""Request-scoped dependencies extracting shared app state."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import Request

if TYPE_CHECKING:
    from loreline.reprocess import ReprocessManager
    from loreline.session import SessionManager
    from loreline.web.app import AppState


def get_state(request: Request) -> AppState:
    """Return the shared :class:`AppState` attached to ``app.state.ctx``."""
    state: AppState = request.app.state.ctx
    return state


def get_manager(request: Request) -> SessionManager:
    """Return the active :class:`SessionManager`."""
    return get_state(request).manager


def get_reprocess(request: Request) -> ReprocessManager:
    """Return the :class:`ReprocessManager`."""
    return get_state(request).reprocess
