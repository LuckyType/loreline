"""Shared pytest fixtures."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

from loreline.settings import Settings
from loreline.web.app import create_app

# Read at construction, not at import, so it is enough to set it once here
# before any fixture builds a Settings.
#
# Every suite that builds an app builds it with no password, which since the
# first-run claim landed is two different states: an open dev box, which is
# what these tests mean, and an instance nobody has claimed, which answers
# nothing but its setup routes. The switch is a setting rather than an
# inference from the empty password precisely so this can be one line - and it
# goes in the environment rather than in the fixtures below because about a
# dozen test modules build their own ``Settings`` and would otherwise each need
# the same argument. A test that exercises the first run passes
# ``first_run_setup=True`` explicitly, which wins over this.
os.environ.setdefault("LORELINE_FIRST_RUN_SETUP", "false")


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
