"""Integration tests for the M11 system routes (health/update/autostart/alerts).

A fake command runner stands in for git/systemctl, and a MockTransport stands in
for outbound alert HTTP, so nothing touches the real host.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

import loreline.web.routes.system as system_route
from loreline.settings import Settings
from loreline.updater.process import CommandResult
from loreline.web.app import create_app


class FakeRunner:
    """Stand-in for git/systemctl with a togglable autostart state."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.enabled = False

    async def __call__(self, argv: list[str], *, cwd: str | None = None) -> CommandResult:
        self.calls.append(argv)
        if argv[:2] == ["git", "rev-parse"]:
            return CommandResult(0, "commit-sha\n", "")
        if argv[0] == "bash":
            return CommandResult(0, "Update complete.", "")
        if argv[:2] == ["systemctl", "is-enabled"]:
            text = "enabled\n" if self.enabled else "disabled\n"
            return CommandResult(0 if self.enabled else 1, text, "")
        if argv[:3] == ["sudo", "systemctl", "enable"]:
            self.enabled = True
            return CommandResult(0, "", "")
        if argv[:3] == ["sudo", "systemctl", "disable"]:
            self.enabled = False
            return CommandResult(0, "", "")
        return CommandResult(0, "", "")


@pytest.fixture
def alert_requests() -> list[httpx.Request]:
    return []


@pytest_asyncio.fixture
async def client(
    settings: Settings, alert_requests: list[httpx.Request]
) -> AsyncIterator[AsyncClient]:
    def handle(request: httpx.Request) -> httpx.Response:
        alert_requests.append(request)
        return httpx.Response(200)

    def alert_client_factory() -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(handle))

    app = create_app(
        settings, command_runner=FakeRunner(), alert_client_factory=alert_client_factory
    )
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac


async def test_healthz_extended(client: AsyncClient) -> None:
    body = (await client.get("/api/system/healthz")).json()
    assert body["status"] in {"ok", "degraded"}
    assert body["capture_status"] == "idle"
    assert body["disk_total_bytes"] > 0
    assert body["alerts_enabled"] is False
    # No diarization endpoint configured -> nothing probed, nothing claimed.
    assert body["diarizer_endpoint"] is None
    assert body["diarizer_reachable"] is None


async def test_healthz_reports_diarizer_reachability(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Once an endpoint is configured, health says whether it actually answers."""
    probed: list[str] = []

    async def fake_probe(endpoint: str, **_kwargs: object) -> bool:
        probed.append(endpoint)
        return True

    monkeypatch.setattr(system_route, "probe_health", fake_probe)
    monkeypatch.setattr(system_route, "_diarizer_probe", None)  # drop any cached verdict

    await client.put(
        "/api/system/defaults",
        json={
            "stt_model": "",
            "diar_mode": "remote",
            "diar_endpoint": "http://diarization:8001",
            "summarize_model": "",
        },
    )

    body = (await client.get("/api/system/healthz")).json()
    assert body["diarizer_endpoint"] == "http://diarization:8001"
    assert body["diarizer_reachable"] is True
    assert probed == ["http://diarization:8001"]

    # Polled again straight away: served from cache, not re-probed - /healthz is
    # hit every few seconds by the UI.
    await client.get("/api/system/healthz")
    assert probed == ["http://diarization:8001"]


async def test_revision(client: AsyncClient) -> None:
    body = (await client.get("/api/system/revision")).json()
    assert body["commit"] == "commit-sha"


async def test_update(client: AsyncClient) -> None:
    body = (await client.post("/api/system/update")).json()
    assert body["ok"] is True
    assert body["new_commit"] == "commit-sha"
    assert "Update complete." in body["output"]


async def test_rollback(client: AsyncClient) -> None:
    body = (await client.post("/api/system/rollback", json={"commit": "deadbeef"})).json()
    assert body["ok"] is True


async def test_rollback_rejects_non_commit_looking_input(client: AsyncClient) -> None:
    # Guards against argument injection into `git reset --hard <commit>`: a
    # value starting with "-" (or containing non-hex chars) must never reach
    # the subprocess call as a bare argv element.
    resp = await client.post("/api/system/rollback", json={"commit": "--upload-pack=x"})
    assert resp.status_code == 422


async def test_autostart_toggle(client: AsyncClient) -> None:
    assert (await client.get("/api/system/autostart")).json()["enabled"] is False
    put = await client.put("/api/system/autostart", json={"enabled": True})
    assert put.json()["enabled"] is True
    assert (await client.get("/api/system/autostart")).json()["enabled"] is True


async def test_action_defaults_roundtrip(client: AsyncClient) -> None:
    empty = (await client.get("/api/system/defaults")).json()
    assert empty == {
        "stt_model": "",
        "diar_mode": "",
        "diar_endpoint": "",
        "summarize_model": "",
    }

    put = await client.put(
        "/api/system/defaults",
        json={
            "stt_model": "nova-3",
            "diar_mode": "remote",
            "diar_endpoint": "http://diarizer:8001",
            "summarize_model": "gpt-4o-mini",
        },
    )
    assert put.status_code == 200
    fetched = (await client.get("/api/system/defaults")).json()
    assert fetched["stt_model"] == "nova-3"
    assert fetched["diar_mode"] == "remote"
    assert fetched["diar_endpoint"] == "http://diarizer:8001"
    assert fetched["summarize_model"] == "gpt-4o-mini"


async def test_alert_channels_crud_and_test(
    client: AsyncClient, alert_requests: list[httpx.Request]
) -> None:
    assert (await client.get("/api/system/alerts/channels")).json() == []

    created = await client.post(
        "/api/system/alerts/channels",
        json={
            "type": "telegram",
            "enabled": True,
            "min_level": "warning",
            "chat_id": "5",
            "token": "bot-token",
        },
    )
    assert created.status_code == 201
    channel = created.json()
    assert channel["type"] == "telegram"
    assert channel["token_set"] is True
    assert "token" not in channel  # secret never returned
    channel_id = channel["id"]

    listed = (await client.get("/api/system/alerts/channels")).json()
    assert len(listed) == 1
    assert listed[0]["chat_id"] == "5"
    # health reflects that at least one channel is enabled
    assert (await client.get("/api/system/healthz")).json()["alerts_enabled"] is True

    # update: disable + raise the gate; omitting the token keeps the stored one
    updated = await client.put(
        f"/api/system/alerts/channels/{channel_id}",
        json={"type": "telegram", "enabled": False, "min_level": "error", "chat_id": "5"},
    )
    assert updated.json()["enabled"] is False
    assert updated.json()["token_set"] is True

    result = (await client.post(f"/api/system/alerts/channels/{channel_id}/test")).json()
    assert result["ok"] is True
    assert len(alert_requests) >= 1

    assert (await client.delete(f"/api/system/alerts/channels/{channel_id}")).status_code == 200
    assert (await client.get("/api/system/alerts/channels")).json() == []
    assert (await client.get("/api/system/healthz")).json()["alerts_enabled"] is False
    assert (await client.delete(f"/api/system/alerts/channels/{channel_id}")).status_code == 404


async def test_ops_endpoints_require_auth(tmp_path: Path) -> None:
    secured = Settings(data_dir=tmp_path / "data", auth_password="secret", jwt_secret="k")
    app = create_app(secured, command_runner=FakeRunner())
    async with (
        LifespanManager(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac,
    ):
        assert (await ac.post("/api/system/update")).status_code == 401
        assert (await ac.get("/api/system/autostart")).status_code == 401
        # health stays open for external pollers
        assert (await ac.get("/api/system/healthz")).status_code == 200
