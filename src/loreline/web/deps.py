"""Request-scoped dependencies extracting shared app state."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import Request

from loreline.web.schemas import ActionDefaults

if TYPE_CHECKING:
    from loreline.persistence import SettingsRepository
    from loreline.reprocess import ReprocessManager
    from loreline.session import SessionManager
    from loreline.web.app import AppState

ACTION_DEFAULTS_KEY = "action_defaults"  # kv_settings key for the per-action defaults


def get_state(request: Request) -> AppState:
    """Return the shared :class:`AppState` attached to ``app.state.ctx``."""
    state: AppState = request.app.state.ctx
    return state


async def read_action_defaults(settings: SettingsRepository) -> ActionDefaults:
    """Read the stored per-action defaults (blank model when never saved).

    Takes the repository rather than the app state so it can be called from
    where the state does not exist yet: ``create_app`` hands the reprocess
    manager a reader for the default diarizer endpoint while it is still
    building the state the routes get.
    """
    raw = await settings.get(ACTION_DEFAULTS_KEY)
    return ActionDefaults.model_validate_json(raw) if raw else ActionDefaults()


async def load_action_defaults(state: AppState) -> ActionDefaults:
    """The stored per-action defaults, off the app state a route has in hand."""
    return await read_action_defaults(state.settings_repo)


def get_manager(request: Request) -> SessionManager:
    """Return the active :class:`SessionManager`."""
    return get_state(request).manager


def get_reprocess(request: Request) -> ReprocessManager:
    """Return the :class:`ReprocessManager`."""
    return get_state(request).reprocess
