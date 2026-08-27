"""Tests for the compose-project service manager (Settings > Services)."""

from __future__ import annotations

import hashlib
import json

import httpx
import pytest

from loreline.services import (
    DockerUnavailableError,
    ServiceManager,
    _demux,  # pyright: ignore[reportPrivateUsage]
)

_PROJECT = "loreline"


def _fake_id(service: str) -> str:
    """Deterministic 64-hex id, as the Docker API returns."""
    return hashlib.sha256(service.encode()).hexdigest()


def _container(
    service: str, *, state: str = "running", project: str = _PROJECT
) -> dict[str, object]:
    return {
        # Realistic 64-hex container id; the manager truncates to 12 like docker does.
        "Id": _fake_id(service),
        "Image": f"{service}:latest",
        "State": state,
        "Status": "Up 2 hours" if state == "running" else "Exited (0) 1 hour ago",
        "Labels": {
            "com.docker.compose.project": project,
            "com.docker.compose.service": service,
        },
    }


def _manager(handler: httpx.MockTransport) -> ServiceManager:
    client = httpx.AsyncClient(transport=handler, base_url="http://docker-proxy:2375")
    return ServiceManager("http://docker-proxy:2375", project=_PROJECT, client=client)


async def test_lists_only_this_compose_project() -> None:
    """A shared Docker host may run anything; the UI must only ever see its own stack."""
    body = [
        _container("app"),
        _container("diarization", state="exited"),
        _container("someone-elses-db", project="unrelated-project"),
    ]
    manager = _manager(httpx.MockTransport(lambda _r: httpx.Response(200, json=body)))

    services = await manager.list_services()

    assert [s.name for s in services] == ["app", "diarization"]
    assert "someone-elses-db" not in {s.name for s in services}


async def test_marks_only_optional_services_controllable() -> None:
    """The app must not be able to stop itself, nor the proxy the feature runs on."""
    body = [_container("app"), _container("docker-proxy"), _container("diarization")]
    manager = _manager(httpx.MockTransport(lambda _r: httpx.Response(200, json=body)))

    controllable = {s.name: s.controllable for s in await manager.list_services()}

    assert controllable == {"app": False, "docker-proxy": False, "diarization": True}


async def test_start_refuses_uncontrollable_service() -> None:
    manager = _manager(
        httpx.MockTransport(lambda _r: httpx.Response(200, json=[_container("app")]))
    )
    with pytest.raises(DockerUnavailableError, match="cannot be controlled"):
        await manager.set_running("app", running=False)


async def test_start_posts_to_docker_and_reports_new_state() -> None:
    calls: list[str] = []
    state = {"value": "exited"}

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/start"):
            calls.append(request.url.path)
            state["value"] = "running"
            return httpx.Response(204)
        return httpx.Response(200, json=[_container("diarization", state=state["value"])])

    manager = _manager(httpx.MockTransport(handle))
    result = await manager.set_running("diarization", running=True)

    assert result.state == "running"
    assert calls == [f"/containers/{_fake_id('diarization')[:12]}/start"]


async def test_already_running_is_not_an_error() -> None:
    """Docker answers 304 when there's nothing to do - that's success, not failure."""

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/start"):
            return httpx.Response(304)
        return httpx.Response(200, json=[_container("diarization")])

    manager = _manager(httpx.MockTransport(handle))
    assert (await manager.set_running("diarization", running=True)).state == "running"


async def test_disabled_without_a_docker_api() -> None:
    manager = ServiceManager("", project=_PROJECT)
    assert manager.enabled is False
    with pytest.raises(DockerUnavailableError, match="not configured"):
        await manager.list_services()


async def test_unreachable_docker_api_raises_cleanly() -> None:
    def boom(_r: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    manager = _manager(httpx.MockTransport(boom))
    with pytest.raises(DockerUnavailableError, match="could not reach"):
        await manager.list_services()


async def test_logs_strip_docker_stream_framing() -> None:
    """Non-TTY logs are length-prefixed; the header bytes must not reach the UI."""
    payload = b""
    for line in (b"first line\n", b"second line\n"):
        payload += bytes([1, 0, 0, 0]) + len(line).to_bytes(4, "big") + line

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/logs"):
            return httpx.Response(200, content=payload)
        return httpx.Response(200, json=[_container("diarization")])

    manager = _manager(httpx.MockTransport(handle))
    assert await manager.logs("diarization") == "first line\nsecond line\n"


def test_demux_passes_through_unframed_tty_output() -> None:
    """TTY containers emit plain text with no header - don't mangle it."""
    assert _demux(b"plain tty output\n") == "plain tty output\n"


def test_demux_handles_empty_output() -> None:
    assert _demux(b"") == ""


async def test_list_survives_unexpected_payload_shapes() -> None:
    """The Docker API is external input; malformed entries shouldn't 500 the page."""
    body = json.loads('[{"Id":"x"}, "not-a-dict", {"Labels": null, "Id": "y"}]')
    manager = _manager(httpx.MockTransport(lambda _r: httpx.Response(200, json=body)))

    services = await manager.list_services()

    # No compose labels -> falls back to the container id, and nothing raises.
    assert all(s.controllable is False for s in services)
