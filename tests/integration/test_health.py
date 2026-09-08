"""Tests for the system liveness and health endpoints."""

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


# The two are split by what they hand an anonymous caller. /livez says the
# process answered; /healthz carries the version, free disk, capture state, the
# operator's diarizer endpoint and the STT vendor's own error text, which is a
# reconnaissance report and belongs behind the cookie like the rest of the API.


async def test_livez_needs_no_cookie(auth_client: AsyncClient) -> None:
    resp = await auth_client.get("/api/system/livez")
    assert resp.status_code == 200
    # Nothing about this deployment in it, deliberately: only that it is up.
    assert resp.json() == {"status": "ok"}


async def test_healthz_requires_a_cookie(auth_client: AsyncClient) -> None:
    resp = await auth_client.get("/api/system/healthz")
    assert resp.status_code == 401
    assert resp.json() == {"detail": "authentication required"}


async def test_healthz_serves_the_snapshot_once_logged_in(auth_client: AsyncClient) -> None:
    login = await auth_client.post("/api/auth/login", json={"password": "hunter2"})
    assert login.status_code == 200
    resp = await auth_client.get("/api/system/healthz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["version"] == __version__
    assert body["capture_status"] == "idle"
    assert body["disk_total_bytes"] > 0
