"""Tests for the system health endpoint."""

from __future__ import annotations

from httpx import AsyncClient

from loreline import __version__


async def test_healthz_ok(client: AsyncClient) -> None:
    resp = await client.get("/api/system/healthz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["version"] == __version__
    assert body["capture_status"] == "idle"
    assert body["uptime_seconds"] >= 0
