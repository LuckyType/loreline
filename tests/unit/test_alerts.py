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
    DeliveryResult,
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
    assert (await manager.test_channel("w1")).ok is True
    assert len(calls) == 1
    missing = await manager.test_channel("missing")
    assert missing.ok is False
    assert missing.detail == "no such alert channel"


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


async def test_test_channel_reports_the_http_reason(tmp_path: Path) -> None:
    """A rejected delivery names the status and repeats what the vendor said.

    The whole point of the detail: "Test failed" cannot tell a wrong topic from
    a rejected token, and both are one field away from working.
    """

    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": "topic not found"})

    manager, _ = _manager(httpx.MockTransport(handle), SecretStore(tmp_path / "s.json"))
    await manager.set_config(
        AlertConfig(channels=[AlertChannel(id="n1", type="ntfy", topic="nope")])
    )
    result = await manager.test_channel("n1")
    assert result.ok is False
    assert result.detail == "HTTP 404: topic not found"


async def test_test_channel_reports_the_transport_reason(tmp_path: Path) -> None:
    """A closed port says so, rather than saying nothing."""

    def handle(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("[Errno 111] Connection refused")

    manager, _ = _manager(httpx.MockTransport(handle), SecretStore(tmp_path / "s.json"))
    await manager.set_config(
        AlertConfig(channels=[AlertChannel(id="w1", type="webhook", url="http://127.0.0.1:9/x")])
    )
    result = await manager.test_channel("w1")
    assert result.ok is False
    assert result.detail == "could not connect: [Errno 111] Connection refused"


async def test_test_channel_reports_a_blank_required_field(tmp_path: Path) -> None:
    """A channel that was never attempted says why, not "connection refused"."""

    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200)

    manager, _ = _manager(httpx.MockTransport(handle), SecretStore(tmp_path / "s.json"))
    await manager.set_config(AlertConfig(channels=[AlertChannel(id="n1", type="ntfy", topic=None)]))
    result = await manager.test_channel("n1")
    assert result.ok is False
    assert result.detail == "no topic set on this ntfy channel"


async def test_test_channel_succeeds_without_a_reason(tmp_path: Path) -> None:
    """Nothing to explain when it worked, so nothing is attached."""

    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200)

    manager, _ = _manager(httpx.MockTransport(handle), SecretStore(tmp_path / "s.json"))
    await manager.set_config(
        AlertConfig(channels=[AlertChannel(id="w1", type="webhook", url="https://hook.example/x")])
    )
    assert await manager.test_channel("w1") == DeliveryResult(True, None)


async def test_detail_never_carries_the_channel_token(tmp_path: Path) -> None:
    """Telegram puts the bot token in the URL, and vendors echo URLs back.

    Both ways it can come back are covered: the vendor quoting the request in
    its error body, and a transport error quoting the URL it failed to reach.
    """
    token = "1234567:AA-super-secret-bot-token"

    def echo_the_url(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"description": f"Unauthorized: {request.url}"})

    secrets = SecretStore(tmp_path / "s.json")
    secrets.set(channel_token_secret("g1"), token)
    manager, _ = _manager(httpx.MockTransport(echo_the_url), secrets)
    await manager.set_config(
        AlertConfig(channels=[AlertChannel(id="g1", type="telegram", chat_id="42")])
    )
    result = await manager.test_channel("g1")
    assert result.ok is False
    assert result.detail is not None
    assert token not in result.detail
    assert "/bot***" in result.detail

    def refuse_with_the_url(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"failed to connect to {request.url}")

    manager, _ = _manager(httpx.MockTransport(refuse_with_the_url), secrets)
    await manager.set_config(
        AlertConfig(channels=[AlertChannel(id="g1", type="telegram", chat_id="42")])
    )
    refused = await manager.test_channel("g1")
    assert refused.detail is not None
    assert token not in refused.detail


async def test_detail_is_bounded(tmp_path: Path) -> None:
    """A vendor answering with a whole error page must not become the message."""

    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="<html>" + "x" * 5000 + "</html>")

    manager, _ = _manager(httpx.MockTransport(handle), SecretStore(tmp_path / "s.json"))
    await manager.set_config(
        AlertConfig(channels=[AlertChannel(id="w1", type="webhook", url="https://hook.example/x")])
    )
    result = await manager.test_channel("w1")
    assert result.detail is not None
    assert len(result.detail) <= 300


async def test_send_keeps_its_bool_fan_out(tmp_path: Path) -> None:
    """The alerting path's shape is unchanged: one bit per channel, no reasons."""

    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    manager, _ = _manager(httpx.MockTransport(handle), SecretStore(tmp_path / "s.json"))
    await manager.set_config(
        AlertConfig(
            channels=[
                AlertChannel(id="w1", type="webhook", url="https://hook.example/x"),
                AlertChannel(id="n1", type="ntfy", topic="t"),
            ]
        )
    )
    assert await manager.send("t", "m", level=AlertLevel.ERROR) == {"w1": False, "n1": False}
