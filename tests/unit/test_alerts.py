"""Tests for the push-alert manager (per-channel ntfy / Telegram / webhook)."""

from __future__ import annotations

import json
from pathlib import Path

import httpx

from loreline.monitoring.alerts import (
    AlertChannel,
    AlertConfig,
    AlertLevel,
    AlertManager,
    channel_token_secret,
)
from loreline.secrets import SecretStore


class FakeSettings:
    """In-memory key/value store satisfying the SettingsStore protocol."""

    def __init__(self) -> None:
        self._data: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self._data.get(key)

    async def set(self, key: str, value: str) -> None:
        self._data[key] = value


def _manager(
    handler: httpx.MockTransport, secrets: SecretStore
) -> tuple[AlertManager, FakeSettings]:
    settings = FakeSettings()

    def factory() -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=handler)

    return AlertManager(settings=settings, secrets=secrets, client_factory=factory), settings


async def test_disabled_channel_sends_nothing(tmp_path: Path) -> None:
    calls: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200)

    manager, _ = _manager(httpx.MockTransport(handle), SecretStore(tmp_path / "s.json"))
    await manager.set_config(
        AlertConfig(channels=[AlertChannel(id="n1", type="ntfy", topic="t", enabled=False)])
    )
    assert await manager.send("t", "m", level=AlertLevel.ERROR) == {}
    assert calls == []


async def test_per_channel_min_level_gate(tmp_path: Path) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200)

    manager, _ = _manager(httpx.MockTransport(handle), SecretStore(tmp_path / "s.json"))
    await manager.set_config(
        AlertConfig(
            channels=[
                AlertChannel(id="n1", type="ntfy", topic="t", min_level=AlertLevel.INFO),
                AlertChannel(id="w1", type="webhook", url="http://h", min_level=AlertLevel.ERROR),
            ]
        )
    )
    # A warning clears ntfy's INFO gate but not the webhook's ERROR gate.
    assert await manager.send("t", "m", level=AlertLevel.WARNING) == {"n1": True}
    assert await manager.send("t", "m", level=AlertLevel.ERROR) == {"n1": True, "w1": True}


async def test_ntfy_and_webhook_dispatch(tmp_path: Path) -> None:
    urls: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        urls.append(str(request.url))
        return httpx.Response(200)

    manager, _ = _manager(httpx.MockTransport(handle), SecretStore(tmp_path / "s.json"))
    await manager.set_config(
        AlertConfig(
            channels=[
                AlertChannel(
                    id="n1",
                    type="ntfy",
                    server="https://ntfy.example",
                    topic="loreline",
                    min_level=AlertLevel.INFO,
                ),
                AlertChannel(
                    id="w1", type="webhook", url="https://hook.example/x", min_level=AlertLevel.INFO
                ),
            ]
        )
    )
    assert await manager.send("Title", "Body", level=AlertLevel.WARNING) == {"n1": True, "w1": True}
    assert "https://ntfy.example/loreline" in urls
    assert "https://hook.example/x" in urls


async def test_telegram_requires_token(tmp_path: Path) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200)

    secrets = SecretStore(tmp_path / "s.json")
    manager, _ = _manager(httpx.MockTransport(handle), secrets)
    await manager.set_config(
        AlertConfig(
            channels=[
                AlertChannel(id="g1", type="telegram", chat_id="42", min_level=AlertLevel.INFO)
            ]
        )
    )
    assert await manager.send("t", "m", level=AlertLevel.WARNING) == {"g1": False}
    secrets.set(channel_token_secret("g1"), "bot-token")
    assert await manager.send("t", "m", level=AlertLevel.WARNING) == {"g1": True}


async def test_test_channel_ignores_gates(tmp_path: Path) -> None:
    calls: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200)

    manager, _ = _manager(httpx.MockTransport(handle), SecretStore(tmp_path / "s.json"))
    await manager.set_config(
        AlertConfig(channels=[AlertChannel(id="w1", type="webhook", url="http://h", enabled=False)])
    )
    assert await manager.test_channel("w1") is True
    assert len(calls) == 1
    assert await manager.test_channel("missing") is False


async def test_channel_failure_reported(tmp_path: Path) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    manager, _ = _manager(httpx.MockTransport(handle), SecretStore(tmp_path / "s.json"))
    await manager.set_config(
        AlertConfig(channels=[AlertChannel(id="w1", type="webhook", url="http://h")])
    )
    assert await manager.send("t", "m", level=AlertLevel.WARNING) == {"w1": False}


async def test_config_roundtrip(tmp_path: Path) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200)

    manager, _ = _manager(httpx.MockTransport(handle), SecretStore(tmp_path / "s.json"))
    assert (await manager.get_config()).channels == []
    await manager.set_config(
        AlertConfig(channels=[AlertChannel(id="n1", type="ntfy", topic="abc")])
    )
    loaded = await manager.get_config()
    assert len(loaded.channels) == 1
    assert loaded.channels[0].topic == "abc"


async def test_legacy_config_migration(tmp_path: Path) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200)

    manager, settings = _manager(httpx.MockTransport(handle), SecretStore(tmp_path / "s.json"))
    await settings.set(
        "alerts",
        json.dumps(
            {
                "enabled": True,
                "min_level": "error",
                "ntfy": {"server": "https://ntfy.example", "topic": "loreline"},
                "telegram": {"chat_id": "5"},
                "webhook": {"url": "https://h/x"},
            }
        ),
    )
    config = await manager.get_config()
    assert sorted(c.type for c in config.channels) == ["ntfy", "telegram", "webhook"]
    assert all(c.enabled for c in config.channels)
    assert all(c.min_level == AlertLevel.ERROR for c in config.channels)
