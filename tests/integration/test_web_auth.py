"""Tests for web auth: login, cookie, and protected routes."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

from loreline.settings import Settings
from loreline.web.app import create_app


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
