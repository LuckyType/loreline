"""Read and control the stack's own containers, for Settings > Services.

Talks to a Docker API over HTTP - in the shipped stack that's the
``docker-socket-proxy`` sidecar, which only exposes the container endpoints
plus start/stop (see docker-compose.yml). Deliberately a thin httpx client
over the handful of endpoints used rather than the full Docker SDK: it keeps
the dependency out of the base install, and there's nothing here that needs
more than four calls.

Everything is scoped to *this* compose project via the
``com.docker.compose.project`` label, so the UI can never list or touch
unrelated containers on the same host.
"""

from __future__ import annotations

from http import HTTPStatus
from typing import cast

import httpx
from pydantic import BaseModel

from loreline.logging import get_logger

log = get_logger(__name__)

_TIMEOUT_S = 10.0
_PROJECT_LABEL = "com.docker.compose.project"
_SERVICE_LABEL = "com.docker.compose.service"

# Services the UI is allowed to start/stop. The app must never be able to stop
# itself (it would kill the request mid-flight and leave nothing to restart
# it), and the proxy is what enforces the whole feature - taking either down
# from the UI is a foot-gun, not a feature.
CONTROLLABLE = frozenset({"speaches", "diarization"})


class ServiceState(BaseModel):
    """One container in this compose project."""

    name: str
    """Compose service name (e.g. "app", "diarization")."""
    container_id: str
    state: str
    """Docker's lifecycle state: running, exited, created, paused, …"""
    status: str
    """Human-readable detail, e.g. "Up 2 hours"."""
    image: str
    controllable: bool
    """Whether the UI may start/stop this one."""


class DockerUnavailableError(RuntimeError):
    """The Docker API isn't configured or can't be reached."""


class ServiceManager:
    """Minimal Docker API client scoped to one compose project."""

    def __init__(
        self, base_url: str, *, project: str = "", client: httpx.AsyncClient | None = None
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._project = project
        self._client = client

    @property
    def enabled(self) -> bool:
        return bool(self._base_url)

    def _http(self) -> httpx.AsyncClient:
        if self._client is not None:
            return self._client
        return httpx.AsyncClient(base_url=self._base_url, timeout=_TIMEOUT_S)

    async def _request(self, method: str, path: str, **kwargs: object) -> httpx.Response:
        if not self.enabled:
            msg = "Docker API not configured (LORELINE_DOCKER_API is unset)"
            raise DockerUnavailableError(msg)
        client = self._http()
        owns = self._client is None
        try:
            return await client.request(method, path, **kwargs)  # pyright: ignore[reportArgumentType]
        except httpx.HTTPError as exc:
            msg = f"could not reach the Docker API: {exc}"
            raise DockerUnavailableError(msg) from exc
        finally:
            if owns:
                await client.aclose()

    async def list_services(self) -> list[ServiceState]:
        """Every container in this compose project, running or not."""
        response = await self._request("GET", "/containers/json", params={"all": "true"})
        response.raise_for_status()
        payload: object = response.json()
        items = cast("list[object]", payload) if isinstance(payload, list) else []

        services: list[ServiceState] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            container = cast("dict[str, object]", item)
            labels = container.get("Labels")
            labels = cast("dict[str, str]", labels) if isinstance(labels, dict) else {}
            if self._project and labels.get(_PROJECT_LABEL) != self._project:
                continue
            name = labels.get(_SERVICE_LABEL) or _fallback_name(container)
            services.append(
                ServiceState(
                    name=name,
                    container_id=str(container.get("Id", ""))[:12],
                    state=str(container.get("State", "unknown")),
                    status=str(container.get("Status", "")),
                    image=str(container.get("Image", "")),
                    controllable=name in CONTROLLABLE,
                )
            )
        return sorted(services, key=lambda s: s.name)

    async def _resolve(self, service: str) -> ServiceState:
        for candidate in await self.list_services():
            if candidate.name == service:
                return candidate
        msg = f"unknown service {service!r}"
        raise DockerUnavailableError(msg)

    async def set_running(self, service: str, *, running: bool) -> ServiceState:
        """Start or stop ``service``; no-op if it's already in that state."""
        if service not in CONTROLLABLE:
            msg = f"service {service!r} cannot be controlled from the UI"
            raise DockerUnavailableError(msg)
        target = await self._resolve(service)
        action = "start" if running else "stop"
        response = await self._request("POST", f"/containers/{target.container_id}/{action}")
        # 304 = already started/stopped, which is a success for our purposes.
        if response.status_code not in {HTTPStatus.NO_CONTENT, HTTPStatus.NOT_MODIFIED}:
            msg = f"docker {action} failed ({response.status_code}): {response.text[:200]}"
            raise DockerUnavailableError(msg)
        log.info("services.set_running", service=service, running=running)
        return await self._resolve(service)

    async def logs(self, service: str, *, tail: int = 200) -> str:
        """Recent stdout/stderr for ``service``."""
        target = await self._resolve(service)
        response = await self._request(
            "GET",
            f"/containers/{target.container_id}/logs",
            params={"stdout": "true", "stderr": "true", "tail": str(tail)},
        )
        response.raise_for_status()
        return _demux(response.content)


def _fallback_name(container: dict[str, object]) -> str:
    """Container name for anything not started by compose."""
    names = container.get("Names")
    if isinstance(names, list) and names:
        return str(cast("list[object]", names)[0]).lstrip("/")
    return str(container.get("Id", ""))[:12]


def _demux(raw: bytes) -> str:
    """Strip Docker's stream framing from non-TTY container logs.

    Without a TTY the engine prefixes every chunk with an 8-byte header
    (stream type + 4-byte big-endian length); printing it raw leaves control
    bytes smeared through the output. TTY containers send plain text with no
    header, so fall back to decoding as-is when the framing doesn't parse.
    """
    out: list[str] = []
    offset = 0
    while offset + 8 <= len(raw):
        if raw[offset] not in (0, 1, 2):  # not a stream-type byte -> not framed
            break
        size = int.from_bytes(raw[offset + 4 : offset + 8], "big")
        chunk = raw[offset + 8 : offset + 8 + size]
        if len(chunk) < size:
            break
        out.append(chunk.decode("utf-8", errors="replace"))
        offset += 8 + size
    if not out:
        return raw.decode("utf-8", errors="replace")
    return "".join(out)
