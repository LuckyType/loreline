"""Tests that the FastAPI app serves the built SPA with client-side fallback."""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from loreline.web.spa import spa_directory

_HAS_SPA = spa_directory() is not None
pytestmark = pytest.mark.skipif(_HAS_SPA is False, reason="frontend build not present")


async def test_index_served(client: AsyncClient) -> None:
    resp = await client.get("/")
    assert resp.status_code == 200
    assert "Loreline" in resp.text


async def test_deep_link_falls_back_to_index(client: AsyncClient) -> None:
    resp = await client.get("/sessions/does-not-exist")
    assert resp.status_code == 200
    assert "<html" in resp.text.lower()


async def test_api_still_takes_precedence(client: AsyncClient) -> None:
    resp = await client.get("/api/system/healthz")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
