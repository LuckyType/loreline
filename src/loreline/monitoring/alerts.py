"""Push alert notifications: per-channel ntfy / Telegram / generic webhook.

Alerts are a **list of channels** persisted as ``kv_settings`` JSON; each channel
carries its own ``enabled`` flag and ``min_level`` gate. Channel secrets (a
Telegram bot token, or an optional ntfy auth token) live in the ``SecretStore``
keyed by channel id and are never returned by the API. Delivery is best-effort: a
channel failure is logged and reported but never raised, so alerting can never
break the caller (a capture session must not die because ntfy is down).

A failure also has to say *why*. "Test failed" with no reason is the same bug
one level up: the operator learns that something is wrong and nothing about
which field to fix. See :class:`DeliveryResult` for how far that reason travels
and why the fan-out path deliberately keeps its bool.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Literal, Protocol, cast

import httpx
from pydantic import BaseModel, Field

from loreline.health import error_message
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


@dataclass(frozen=True)
class DeliveryResult:
    """Whether a channel took the notification, and the reason when it did not.

    The smallest design that makes a failed test actionable without disturbing
    the alerting path. Two callers want different things from one delivery:

    * :meth:`AlertManager.send` fans out across every enabled channel while
      something is already going wrong. It wants one bit per channel, it logs
      the rest, and nobody is reading a reason at that moment. It keeps its
      ``dict[str, bool]``, so no caller of the alerting path changes shape.
    * :meth:`AlertManager.test_channel` is a button a human just pressed, and
      the answer is the whole point of pressing it. It hands this back.

    So the reason is produced once, at the bottom, and only the test path
    carries it up. Widening ``send`` to return these as well would cost every
    caller a ``.ok`` for information none of them read.

    ``detail`` is written for whoever is looking at the settings page: the
    transport's own words ("could not connect: [Errno 111] Connection
    refused"), or the status and the vendor's sentence for an HTTP rejection.
    It is scrubbed by :func:`_scrub` and bounded, because it is rendered in the
    UI and written to the log.
    """

    ok: bool
    detail: str | None = None


# Details end up in a one-line message above the channel table, and a vendor
# that answers with a whole HTML error page must not put all of it there. Same
# bound the provider health probes use, for the same reason.
_MAX_DETAIL_CHARS = 300
_TOKEN_MASK = "***"
# Telegram puts the bot token in the *path* (`/bot<token>/sendMessage`), so any
# vendor sentence or transport error that quotes the request URL quotes the
# credential with it. Redacting the token we hold covers that; this covers the
# case where what is echoed is not the token we hold (rotated, re-encoded, or
# a stale in-flight request), which is exactly when a leak would slip through
# unnoticed.
_BOT_PATH = re.compile(r"/bot[^/\s]+")
# Why a channel could not even be attempted. A channel type with its one
# required field blank is not a transport failure, and saying "connection
# refused" for it would send the operator to the wrong place.
_MISSING_FIELD: dict[AlertChannelType, str] = {
    "ntfy": "no topic set on this ntfy channel",
    "telegram": "no chat id set on this Telegram channel",
    "webhook": "no URL set on this webhook channel",
}


def _scrub(text: str, token: str | None) -> str:
    """Bound a detail string and strip the channel's credential out of it.

    Called on every path that produces a ``detail``, before it is logged and
    before it is returned, rather than at the two places it is consumed: a
    redaction you have to remember to apply is one that eventually is not.
    Truncation comes after redaction so a token can never be half-masked by
    the cut.
    """
    if token:
        text = text.replace(token, _TOKEN_MASK)
    text = _BOT_PATH.sub(f"/bot{_TOKEN_MASK}", text)
    return text.strip()[:_MAX_DETAIL_CHARS]


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
                # `.ok` here, deliberately: see DeliveryResult on why the
                # fan-out keeps one bit per channel. The reason is already in
                # the log by the time this line runs.
                results[channel.id] = (
                    await self._send_channel(client, channel, title, message, level)
                ).ok
        if results:
            log.info("alert.sent", title=title, level=level.value, results=results)
        return results

    async def test_channel(self, channel_id: str) -> DeliveryResult:
        """Send a test notification to one channel (ignores the enable/level gates).

        Returns the reason as well as the verdict: this is the only feedback an
        alert channel ever gives, since nothing probes one periodically and the
        table shows no health. A bare False here is a page that says "Test
        failed" and leaves the operator to guess between a typo, a closed port
        and a rejected token.
        """
        config = await self.get_config()
        channel = next((c for c in config.channels if c.id == channel_id), None)
        if channel is None:
            return DeliveryResult(False, "no such alert channel")
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
    ) -> DeliveryResult:
        if channel.type == "ntfy" and channel.topic:
            return await self._send_ntfy(client, channel, title, message, level)
        if channel.type == "telegram" and channel.chat_id:
            return await self._send_telegram(client, channel, title, message)
        if channel.type == "webhook" and channel.url:
            return await self._send_webhook(client, channel, title, message, level)
        return DeliveryResult(False, _MISSING_FIELD.get(channel.type, "channel not configured"))

    async def _send_ntfy(
        self,
        client: httpx.AsyncClient,
        channel: AlertChannel,
        title: str,
        message: str,
        level: AlertLevel,
    ) -> DeliveryResult:
        headers = {"Title": title, "Priority": _NTFY_PRIORITY[level]}
        token = self._secrets.get(channel_token_secret(channel.id))
        if token:
            headers["Authorization"] = f"Bearer {token}"
        url = f"{channel.server.rstrip('/')}/{channel.topic}"
        return await self._safe(
            "ntfy",
            client.post(url, content=message.encode(), headers=headers),
            token=token,
        )

    async def _send_telegram(
        self, client: httpx.AsyncClient, channel: AlertChannel, title: str, message: str
    ) -> DeliveryResult:
        token = self._secrets.get(channel_token_secret(channel.id))
        if not token:
            log.warning("alert.telegram.no_token", channel=channel.id)
            return DeliveryResult(False, "no bot token stored for this channel")
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = {"chat_id": channel.chat_id, "text": f"{title}\n\n{message}"}
        return await self._safe("telegram", client.post(url, json=payload), token=token)

    async def _send_webhook(
        self,
        client: httpx.AsyncClient,
        channel: AlertChannel,
        title: str,
        message: str,
        level: AlertLevel,
    ) -> DeliveryResult:
        payload = {"title": title, "message": message, "level": level.value}
        # A webhook has no token of its own, but the URL may still carry a
        # shared secret in a query string, so the scrubbing path is the same.
        return await self._safe(
            "webhook",
            client.post(channel.url or "", json=payload),
            token=self._secrets.get(channel_token_secret(channel.id)),
        )

    async def _safe(
        self, channel: str, request: Awaitable[httpx.Response], *, token: str | None = None
    ) -> DeliveryResult:
        """Run one delivery and grade it. Never raises, and never leaks ``token``.

        The single place a reason is written, so the single place it has to be
        scrubbed. ``token`` is the channel's stored credential where it has
        one: a refused connection and a vendor error body both quote the URL
        they were given often enough that redaction cannot be conditional on
        the transport.
        """
        try:
            response = await request
        except httpx.HTTPError as exc:
            detail = _scrub(f"could not connect: {str(exc).strip() or type(exc).__name__}", token)
            log.warning("alert.failed", channel=channel, error=detail)
            return DeliveryResult(False, detail)
        if response.status_code >= httpx.codes.BAD_REQUEST:
            # The vendor's own sentence, unwrapped the same way a provider
            # health probe unwraps it, because "HTTP 401: Unauthorized" and
            # "HTTP 404: topic not found" send the operator to different
            # fields while a bare 4xx sends them nowhere.
            said = error_message(response.text)
            detail = _scrub(
                f"HTTP {response.status_code}: {said}" if said else f"HTTP {response.status_code}",
                token,
            )
            log.warning(
                "alert.rejected", channel=channel, status=response.status_code, error=detail
            )
            return DeliveryResult(False, detail)
        return DeliveryResult(True)
