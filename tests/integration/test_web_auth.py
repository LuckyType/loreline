"""Tests for web auth: login, cookie, and protected routes."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from asgi_lifespan import LifespanManager
from fastapi.routing import APIRoute
from httpx import ASGITransport, AsyncClient

from loreline.settings import Settings
from loreline.web.app import create_app
from loreline.web.auth import require_auth


@pytest.fixture
def auth_settings(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=tmp_path / "data",
        auth_password="hunter2",
        jwt_secret="test-secret",
    )


@pytest_asyncio.fixture
async def auth_client(auth_settings: Settings) -> AsyncIterator[AsyncClient]:
    app = create_app(auth_settings)
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac


async def test_protected_route_requires_auth(auth_client: AsyncClient) -> None:
    resp = await auth_client.get("/api/providers")
    assert resp.status_code == 401
    # Pinned because the cookie now arrives via a security scheme: that must stay
    # descriptive (``auto_error=False``) rather than raising its own 403 first.
    assert resp.json() == {"detail": "authentication required"}


async def test_login_bad_password(auth_client: AsyncClient) -> None:
    resp = await auth_client.post("/api/auth/login", json={"password": "wrong"})
    assert resp.status_code == 401


async def test_login_then_access(auth_client: AsyncClient) -> None:
    login = await auth_client.post("/api/auth/login", json={"password": "hunter2"})
    assert login.status_code == 200
    assert "loreline_token" in login.cookies

    resp = await auth_client.get("/api/providers")
    assert resp.status_code == 200
    assert resp.json() == []


async def test_logout_clears_cookie(auth_client: AsyncClient) -> None:
    await auth_client.post("/api/auth/login", json={"password": "hunter2"})
    await auth_client.post("/api/auth/logout")
    resp = await auth_client.get("/api/providers")
    assert resp.status_code == 401


async def test_auth_disabled_allows_access(client: AsyncClient) -> None:
    # Default fixture uses an empty password -> auth disabled.
    resp = await client.get("/api/providers")
    assert resp.status_code == 200


async def test_login_rate_limited_after_repeated_failures(auth_client: AsyncClient) -> None:
    for _ in range(5):
        resp = await auth_client.post("/api/auth/login", json={"password": "wrong"})
        assert resp.status_code == 401
    # 6th attempt is blocked outright, even with the correct password.
    resp = await auth_client.post("/api/auth/login", json={"password": "hunter2"})
    assert resp.status_code == 429


# The routers gate routes with a plain ``Depends(require_auth)``, which FastAPI
# cannot recognise as a security scheme on its own. ``require_auth`` therefore
# pulls the cookie through ``Security(session_cookie)``, and these tests pin the
# result: a new route added without auth metadata fails here rather than
# quietly shipping a schema that says the API is open.

# Deliberately unauthenticated: the health probe, the static capability config
# the login screen needs to render, and the login/logout pair itself.
PUBLIC_OPERATIONS = {
    ("/api/system/healthz", "get"),
    ("/api/capabilities", "get"),
    ("/api/auth/login", "post"),
    ("/api/auth/logout", "post"),
}


async def test_openapi_declares_the_cookie_security_scheme(client: AsyncClient) -> None:
    schema = (await client.get("/openapi.json")).json()
    schemes = schema["components"]["securitySchemes"]
    assert schemes["sessionCookie"]["type"] == "apiKey"
    assert schemes["sessionCookie"]["in"] == "cookie"
    assert schemes["sessionCookie"]["name"] == "loreline_token"


def test_openapi_security_matches_the_routes(settings: Settings) -> None:
    app = create_app(settings)
    schema = app.openapi()
    declared = {
        (path, method)
        for path, item in schema["paths"].items()
        for method, op in item.items()
        if op.get("security") == [{"sessionCookie": []}]
    }
    documented = {
        (path, method)
        for path, item in schema["paths"].items()
        for method, op in item.items()
        if isinstance(op, dict)
    }
    assert documented - declared == PUBLIC_OPERATIONS

    # And the schema tracks the code rather than a list maintained by hand:
    # every route whose dependencies include ``require_auth`` is in there.
    guarded = {
        (route.path, method.lower())
        for route in app.routes
        if isinstance(route, APIRoute)
        for method in route.methods
        if any(d.call is require_auth for d in route.dependant.dependencies)
    }
    assert guarded == declared
