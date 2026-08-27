"""Auth routes: password login + logout."""

from __future__ import annotations

from fastapi import APIRouter, Request, Response
from fastapi.exceptions import HTTPException
from starlette.status import HTTP_401_UNAUTHORIZED, HTTP_429_TOO_MANY_REQUESTS

from loreline.web.auth import COOKIE_NAME, issue_token, verify_password
from loreline.web.deps import get_state
from loreline.web.schemas import LoginRequest, OkResponse

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/login")
async def login(request: Request, body: LoginRequest, response: Response) -> OkResponse:
    """Validate the shared password and set an auth cookie.

    Rate-limited per client IP (see ``LoginRateLimiter``): a shared password
    with no backoff is a free brute-force target on a device that's LAN- (or
    worse, internet-) reachable.
    """
    state = get_state(request)
    settings = state.settings
    key = request.client.host if request.client else "unknown"
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
        max_age=settings.jwt_ttl_seconds,
    )
    return OkResponse()


@router.post("/logout")
async def logout(response: Response) -> OkResponse:
    """Clear the auth cookie."""
    response.delete_cookie(COOKIE_NAME)
    return OkResponse()
