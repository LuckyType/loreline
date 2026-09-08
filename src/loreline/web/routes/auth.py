"""Auth routes: password login + logout."""

from __future__ import annotations

from fastapi import APIRouter, Request, Response
from fastapi.exceptions import HTTPException
from starlette.status import HTTP_401_UNAUTHORIZED, HTTP_429_TOO_MANY_REQUESTS

from loreline.web.auth import (
    COOKIE_NAME,
    client_address,
    client_uses_https,
    issue_token,
    verify_password,
)
from loreline.web.deps import get_state
from loreline.web.schemas import LoginRequest, OkResponse

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/login")
async def login(request: Request, body: LoginRequest, response: Response) -> OkResponse:
    """Validate the shared password and set an auth cookie.

    Rate-limited per client (see ``LoginRateLimiter``): a shared password with
    no backoff is a free brute-force target on a device that's LAN- (or worse,
    internet-) reachable. The key comes from ``client_address`` rather than the
    socket's peer, because behind the bundled Caddy every browser at the table
    arrives from the same container address and a limiter keyed on that is a
    global one: anyone able to reach the box could hold the login shut for
    everybody, five requests at a time.
    """
    state = get_state(request)
    settings = state.settings
    key = client_address(request, settings)
    if not state.login_limiter.allowed(key):
        raise HTTPException(
            status_code=HTTP_429_TOO_MANY_REQUESTS, detail="too many attempts, try again shortly"
        )
    if not verify_password(body.password, settings):
        state.login_limiter.record_failure(key)
        raise HTTPException(status_code=HTTP_401_UNAUTHORIZED, detail="invalid password")
    state.login_limiter.record_success(key)
    token = issue_token(settings)
    response.set_cookie(
        COOKIE_NAME,
        token,
        httponly=True,
        samesite="lax",
        # Set only for a browser that reached us over TLS, which on this
        # deployment means through the bundled Caddy: marking it unconditionally
        # would stop the cookie being sent at all on the plain-HTTP LAN path
        # this app primarily supports. See client_uses_https for why the
        # forwarded scheme is only believed from a configured proxy.
        secure=client_uses_https(request, settings),
        max_age=settings.jwt_ttl_seconds,
    )
    return OkResponse()


@router.post("/logout")
async def logout(request: Request, response: Response) -> OkResponse:
    """Clear the auth cookie."""
    # Same attributes the cookie was set with, so the expiry lands on that
    # cookie rather than alongside it.
    response.delete_cookie(
        COOKIE_NAME,
        httponly=True,
        samesite="lax",
        secure=client_uses_https(request, get_state(request).settings),
    )
    return OkResponse()
