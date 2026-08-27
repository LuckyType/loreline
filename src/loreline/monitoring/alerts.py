"""Push alert notifications: per-channel ntfy / Telegram / generic webhook.

Alerts are a **list of channels** persisted as ``kv_settings`` JSON; each channel
carries its own ``enabled`` flag and ``min_level`` gate. Channel secrets (a
Telegram bot token, or an optional ntfy auth token) live in the ``SecretStore``
keyed by channel id and are never returned by the API. Delivery is best-effort: a
channel failure is logged and reported but never raised, so alerting can never
break the caller (a capture session must not die because ntfy is down).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Awaitable, Callable
from enum import StrEnum
from typing import TYPE_CHECKING, Literal, Protocol, cast

import httpx
from pydantic import BaseModel, Field

from loreline.logging import get_logger

if TYPE_CHECKING:
    from loreline.secrets import SecretStore


class SettingsStore(Protocol):
    """Minimal persisted key/value store (satisfied by ``SettingsRepository``)."""

    async def get(self, key: str) -> str | None: ...
    async def set(self, key: str, value: str) -> None: ...


log = get_logger(__name__)

SETTINGS_KEY = "alerts"

AlertChannelType = Literal["ntfy", "telegram", "webhook"]


def channel_token_secret(channel_id: str) -> str:
    """Secret-store key holding a channel's token (Telegram bot / ntfy auth)."""
    return f"alert:{channel_id}:token"


class AlertLevel(StrEnum):
    """Severity of an alert; gated against each channel's ``min_level``."""

    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


_LEVEL_ORDER: dict[AlertLevel, int] = {
    AlertLevel.INFO: 0,
    AlertLevel.WARNING: 1,
    AlertLevel.ERROR: 2,
}
_NTFY_PRIORITY: dict[AlertLevel, str] = {
    AlertLevel.INFO: "default",
    AlertLevel.WARNING: "high",
    AlertLevel.ERROR: "urgent",
}


class AlertChannel(BaseModel):
    """One notification channel with its own enable flag and severity gate."""

    id: str
    type: AlertChannelType
    enabled: bool = True
    min_level: AlertLevel = AlertLevel.WARNING
    server: str = "https://ntfy.sh"  # ntfy
    topic: str | None = None  # ntfy
    chat_id: str | None = None  # telegram
    url: str | None = None  # webhook


class AlertConfig(BaseModel):
    """Persisted alert configuration: a list of channels (no secrets)."""

    channels: list[AlertChannel] = Field(default_factory=list[AlertChannel])


ClientFactory = Callable[[], httpx.AsyncClient]


def _default_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=10.0)


def _as_dict(value: object) -> dict[str, object]:
    return cast("dict[str, object]", value) if isinstance(value, dict) else {}


def _migrate_legacy(data: dict[str, object]) -> dict[str, object]:
    """Convert the pre-list shape ({enabled, min_level, ntfy, telegram, webhook})."""
    enabled = bool(data.get("enabled", False))
    min_level = data.get("min_level", AlertLevel.WARNING.value)

    def base(channel_type: str) -> dict[str, object]:
        return {
            "id": uuid.uuid4().hex,
            "type": channel_type,
            "enabled": enabled,
            "min_level": min_level,
        }

    channels: list[dict[str, object]] = []
    ntfy = _as_dict(data.get("ntfy"))
    if ntfy.get("topic"):
        channels.append(
            {
                **base("ntfy"),
                "server": ntfy.get("server", "https://ntfy.sh"),
                "topic": ntfy["topic"],
            }
        )
    telegram = _as_dict(data.get("telegram"))
    if telegram.get("chat_id"):
        channels.append({**base("telegram"), "chat_id": telegram["chat_id"]})
    webhook = _as_dict(data.get("webhook"))
    if webhook.get("url"):
        channels.append({**base("webhook"), "url": webhook["url"]})
    return {"channels": channels}


class AlertManager:
    """Load alert channels and fan a notification out to the enabled ones."""

    def __init__(
        self,
        *,
        settings: SettingsStore,
        secrets: SecretStore,
        client_factory: ClientFactory | None = None,
    ) -> None:
        self._settings = settings
        self._secrets = secrets
        self._client_factory = client_factory or _default_client

    async def get_config(self) -> AlertConfig:
        raw = await self._settings.get(SETTINGS_KEY)
        if raw is None:
            return AlertConfig()
        data: object = json.loads(raw)
        if isinstance(data, dict) and "channels" not in data:
            data = _migrate_legacy(cast("dict[str, object]", data))
        return AlertConfig.model_validate(data)

    async def set_config(self, config: AlertConfig) -> None:
        await self._settings.set(SETTINGS_KEY, config.model_dump_json())

    async def send(
        self, title: str, message: str, *, level: AlertLevel = AlertLevel.WARNING
    ) -> dict[str, bool]:
        """Deliver an alert to every enabled channel at/above its ``min_level``."""
        config = await self.get_config()
        results: dict[str, bool] = {}
        async with self._client_factory() as client:
            for channel in config.channels:
                if not channel.enabled or _LEVEL_ORDER[level] < _LEVEL_ORDER[channel.min_level]:
                    continue
                results[channel.id] = await self._send_channel(
                    client, channel, title, message, level
                )
        if results:
            log.info("alert.sent", title=title, level=level.value, results=results)
        return results

    async def test_channel(self, channel_id: str) -> bool:
        """Send a test notification to one channel (ignores the enable/level gates)."""
        config = await self.get_config()
        channel = next((c for c in config.channels if c.id == channel_id), None)
        if channel is None:
            return False
        async with self._client_factory() as client:
            return await self._send_channel(
                client,
                channel,
                "Loreline test alert",
                "Test notification from Loreline.",
                AlertLevel.INFO,
            )

    async def _send_channel(
        self,
        client: httpx.AsyncClient,
        channel: AlertChannel,
        title: str,
        message: str,
        level: AlertLevel,
    ) -> bool:
        if channel.type == "ntfy" and channel.topic:
            return await self._send_ntfy(client, channel, title, message, level)
        if channel.type == "telegram" and channel.chat_id:
            return await self._send_telegram(client, channel, title, message)
        if channel.type == "webhook" and channel.url:
            return await self._send_webhook(client, channel, title, message, level)
        return False

    async def _send_ntfy(
        self,
        client: httpx.AsyncClient,
        channel: AlertChannel,
        title: str,
        message: str,
        level: AlertLevel,
    ) -> bool:
        headers = {"Title": title, "Priority": _NTFY_PRIORITY[level]}
        token = self._secrets.get(channel_token_secret(channel.id))
        if token:
            headers["Authorization"] = f"Bearer {token}"
        url = f"{channel.server.rstrip('/')}/{channel.topic}"
        return await self._safe("ntfy", client.post(url, content=message.encode(), headers=headers))

    async def _send_telegram(
        self, client: httpx.AsyncClient, channel: AlertChannel, title: str, message: str
    ) -> bool:
        token = self._secrets.get(channel_token_secret(channel.id))
        if not token:
            log.warning("alert.telegram.no_token", channel=channel.id)
            return False
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = {"chat_id": channel.chat_id, "text": f"{title}\n\n{message}"}
        return await self._safe("telegram", client.post(url, json=payload))

    async def _send_webhook(
        self,
        client: httpx.AsyncClient,
        channel: AlertChannel,
        title: str,
        message: str,
        level: AlertLevel,
    ) -> bool:
        payload = {"title": title, "message": message, "level": level.value}
        return await self._safe("webhook", client.post(channel.url or "", json=payload))

    async def _safe(self, channel: str, request: Awaitable[httpx.Response]) -> bool:
        try:
            response = await request
        except httpx.HTTPError as exc:
            log.warning("alert.failed", channel=channel, error=str(exc))
            return False
        if response.status_code >= httpx.codes.BAD_REQUEST:
            log.warning("alert.rejected", channel=channel, status=response.status_code)
            return False
        return True
