"""Shared pytest fixtures."""

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
def settings(tmp_path: Path) -> Settings:
    return Settings(data_dir=tmp_path / "data", auth_password="", jwt_secret="test-secret")


@pytest_asyncio.fixture
async def client(settings: Settings) -> AsyncIterator[AsyncClient]:
    app = create_app(settings)
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac


@pytest.fixture
def auth_settings(tmp_path: Path) -> Settings:
    """Settings with a password set, so every auth check is actually live."""
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
