"""First-run routes: what is left to do, and the claim that does the first of it.

``GET /api/setup/state`` is the one route an unclaimed instance answers without
a session, because the browser has to be able to ask "do I belong to anyone
yet" before it can have one. ``POST /api/setup/claim`` is the transition out of
that state. Both are reachable when claimed too: the wizard's later steps are
resumable, and a claim attempt on a claimed instance has to say so rather than
quietly overwriting the password.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response
from fastapi.exceptions import HTTPException
from starlette.status import (
    HTTP_400_BAD_REQUEST,
    HTTP_401_UNAUTHORIZED,
    HTTP_409_CONFLICT,
    HTTP_429_TOO_MANY_REQUESTS,
)

from loreline.web.auth import client_address, require_auth, set_session_cookie
from loreline.web.deps import get_state
from loreline.web.schemas import ClaimRequest, SetupState
from loreline.web.setup import (
    claim_instance,
    instance_claimed,
    password_problem,
    setup_required,
    verify_setup_code,
)

router = APIRouter(prefix="/api/setup", tags=["setup"])

#: kv_settings key: set once the wizard has been finished or skipped through.
SETUP_COMPLETE_KEY = "setup_complete"


async def _state(request: Request) -> SetupState:
    """The current setup state, read fresh from settings and the database."""
    state = get_state(request)
    settings = state.settings
    if instance_claimed(settings):
        which = "claimed"
    elif settings.first_run_setup:
        which = "unclaimed"
    else:
        which = "open"
    return SetupState(
        state=which,
        provider_configured=bool(await state.providers.list()),
        wizard_complete=await state.settings_repo.get(SETUP_COMPLETE_KEY) == "1",
    )


@router.get("/state")
async def setup_state(request: Request) -> SetupState:
    """Which state this instance is in and which steps remain.

    Unauthenticated on purpose: an unclaimed instance has no way to
    authenticate anybody, and this is what the browser reads to decide whether
    to send the visitor to the wizard. It never carries the setup code, and
    nothing that would let a caller learn a credential, an endpoint or a
    session - only whether configuration exists.
    """
    return await _state(request)


@router.post("/claim")
async def claim(request: Request, body: ClaimRequest, response: Response) -> SetupState:
    """Take ownership of an unclaimed instance and sign the browser in.

    The setup code is what makes this safe to expose. Without one, the first
    stranger to reach the box on the LAN or the tailnet would own the
    transcripts and the provider keys, so a wrong code is refused with the
    same per-client backoff the login route uses - the same limiter object, so
    a brute force cannot get a fresh budget by switching between the two
    routes, and the same ``client_address`` key, so a reverse proxy does not
    turn every browser at the table into one shared bucket.

    A successful claim leaves the browser signed in. Bouncing to the login form
    to retype the password chosen one second earlier is a step that exists only
    because the code was written in the other order.
    """
    state = get_state(request)
    settings = state.settings
    if not setup_required(settings):
        raise HTTPException(
            status_code=HTTP_409_CONFLICT,
            detail="this instance has already been set up; sign in with its password",
        )
    key = client_address(request, settings)
    if not state.login_limiter.allowed(key):
        raise HTTPException(
            status_code=HTTP_429_TOO_MANY_REQUESTS, detail="too many attempts, try again shortly"
        )
    if not verify_setup_code(body.setup_code, state.secrets):
        state.login_limiter.record_failure(key)
        # Says what the code is for and where to find it, and nothing about the
        # code itself: no length, no prefix, no "close".
        raise HTTPException(
            status_code=HTTP_401_UNAUTHORIZED,
            detail="that setup code is not right; it is printed in this instance's startup log",
        )
    # After the code, so that mistyping the password twice does not spend an
    # attempt from a budget that exists to slow down guessing the code.
    problem = password_problem(body.password, body.password_confirm)
    if problem:
        raise HTTPException(status_code=HTTP_400_BAD_REQUEST, detail=problem)
    state.login_limiter.record_success(key)
    claim_instance(settings, state.secrets, body.password)
    set_session_cookie(response, request, settings)
    return await _state(request)


@router.post("/complete", dependencies=[Depends(require_auth)])
async def complete(request: Request) -> SetupState:
    """Record that the wizard has been finished, or skipped through.

    Every step after the claim is optional, so this says "stop asking", not
    "everything is configured". A GM who skipped both still lands on a working
    dashboard, and the steps stay reachable from Settings.

    Refused while unclaimed, even though it is trivial. ``require_auth`` is a
    no-op on an instance with no password, so without this the one route the
    gate deliberately lets through would let a stranger mark the wizard
    finished and quietly remove the prompt to finish it.
    """
    state = get_state(request)
    if setup_required(state.settings):
        raise HTTPException(
            status_code=HTTP_409_CONFLICT, detail="claim this instance before finishing its setup"
        )
    await state.settings_repo.set(SETUP_COMPLETE_KEY, "1")
    return await _state(request)
