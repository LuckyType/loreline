"""The app as a single container: no Docker API, no updater, no proxy in front.

One `docker run` with a published port and a data volume is a supported way to
run Loreline (the README's "The short way in"), and it is the deployment with
the fewest moving parts: no socket proxy, so Settings > Services has nothing to
manage; no updater service, so the Update button has nothing to hand the job
to; no Caddy and no `tailscale serve`, so the connection really is plain HTTP
and no forwarded scheme may be believed.

Each of those is a surface that has to say what is missing and why, rather than
fail or show a dead control, and this file is the guard on that. It starts the
whole app configured exactly as that container is and reads what those surfaces
answer, because the failure mode being guarded against is not a crash - it is a
500, or an empty panel, or a greyed button that explains nothing.

The container marker is patched rather than faked through a flag: `Updater` and
`Autostart` both read `/.dockerenv` to decide what this deployment can do, and
a test that set their `in_container` by hand would be asserting about objects
the app did not build.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

import loreline.updater.autostart as autostart_module
import loreline.updater.updater as updater_module
from loreline.settings import Settings
from loreline.updater.process import CommandResult
from loreline.web.app import create_app

_PASSWORD = "hunter2"
# A LAN address, not the loopback: this is the browser on the table reaching the
# published port directly, which is the whole traffic pattern of this
# deployment. It also makes the forged-header test below mean something.
_PEER = ("192.168.1.50", 45678)


class NeverRuns:
    """A command runner that fails the test if anything shells out.

    Nothing in this deployment may: a container has no checkout for git to
    read, no systemd unit to ask about, and no update script to run, and each
    of those paths is supposed to answer from what it already knows rather than
    spend a subprocess being told so.
    """

    async def __call__(self, argv: list[str], *, cwd: str | None = None) -> CommandResult:
        msg = f"a single container must not shell out, but ran: {argv}"
        raise AssertionError(msg)


def _settings(tmp_path: Path) -> Settings:
    """Exactly what the documented `docker run` line configures, and nothing more.

    Every optional integration is spelled out as empty rather than left to the
    default, because that is the point of the test: these are the values the
    container has, and the defaults agreeing with them today is not a promise.
    """
    return Settings(
        data_dir=tmp_path / "data",
        auth_password=_PASSWORD,
        jwt_secret="test-secret",
        docker_api="",
        updater_token="",
        trusted_proxies="",
    )


@pytest_asyncio.fixture
async def minimal_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[AsyncClient]:
    """The app as the single container runs it, signed in."""
    marker = tmp_path / "dockerenv"
    marker.touch()
    monkeypatch.setattr(updater_module, "_DOCKER_MARKER", marker)
    monkeypatch.setattr(autostart_module, "_DOCKER_MARKER", marker)
    app = create_app(_settings(tmp_path), command_runner=NeverRuns())
    async with LifespanManager(app):
        transport = ASGITransport(app=app, client=_PEER)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post("/api/auth/login", json={"password": _PASSWORD})
            assert response.status_code == 200
            yield client


async def test_the_app_starts_and_serves_with_no_optional_service(
    minimal_client: AsyncClient,
) -> None:
    """The floor: everything the app does by itself still answers."""
    assert (await minimal_client.get("/api/system/livez")).status_code == 200

    health = await minimal_client.get("/api/system/healthz")
    assert health.status_code == 200
    assert health.json()["status"] in {"ok", "degraded"}

    # The features the minimal path is sold on, each reachable and each 200.
    for path in ("/api/providers", "/api/session", "/api/campaigns", "/api/system/defaults"):
        assert (await minimal_client.get(path)).status_code == 200, path


async def test_services_answers_with_an_empty_list_rather_than_an_error(
    minimal_client: AsyncClient,
) -> None:
    """Settings > Services renders its explanation from an empty list, not a 503.

    The page has one card to show when there is nothing to manage, and it needs
    a successful answer to get there. A 503 would paint the red error line
    instead, which says "this broke" about a feature that was never configured.
    """
    response = await minimal_client.get("/api/system/services")
    assert response.status_code == 200
    assert response.json() == []


async def test_a_service_call_names_the_setting_that_is_missing(
    minimal_client: AsyncClient,
) -> None:
    """A direct call gets the reason, not a 500 from an unconfigured client."""
    logs = await minimal_client.get("/api/system/services/speaches/logs")
    assert logs.status_code == 503
    assert "LORELINE_DOCKER_API" in logs.json()["detail"]

    started = await minimal_client.post("/api/system/services/speaches", json={"running": True})
    assert started.status_code == 503
    assert "LORELINE_DOCKER_API" in started.json()["detail"]


async def test_update_explains_itself_in_this_deployment_s_own_terms(
    minimal_client: AsyncClient,
) -> None:
    """The Update button answers 200 with a refusal a stranger can act on.

    Not 500 and not a silent no-op: the button reports the one sentence the
    server writes, and here that sentence has to work for a box with no
    checkout, no .env and no compose project on it.
    """
    response = await minimal_client.post("/api/system/update")
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert "docker pull" in body["output"]
    assert "docker run" in body["output"]
    # And it still names the appliance's two answers, for a reader who has one.
    assert "deploy/update.sh" in body["output"]
    assert "--profile updater" in body["output"]


async def test_rollback_is_greyed_out_with_the_reason_beside_it(
    minimal_client: AsyncClient,
) -> None:
    """/revision carries the sentence the page shows under the greyed button."""
    body = (await minimal_client.get("/api/system/revision")).json()
    assert body["previous_commit"] is None
    reason = body["rollback_unavailable"]
    assert "image tag" in reason
    # The way round it, named for a deployment that has no compose file to edit.
    assert "docker run" in reason


async def test_autostart_says_where_the_setting_actually_lives(
    minimal_client: AsyncClient,
) -> None:
    """A container has no systemd unit, and does start at boot anyway.

    "unavailable" on its own reads as "this deployment cannot start at boot",
    which is false and unactionable; the restart policy is the setting, and the
    row shows this sentence in place of the toggle.
    """
    response = await minimal_client.get("/api/system/autostart")
    assert response.status_code == 503
    detail = response.json()["detail"]
    assert "restart policy" in detail
    assert "unless-stopped" in detail


async def test_the_login_cookie_is_not_secure_and_cannot_be_talked_into_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Plain HTTP with nothing in front, which is what this path really is.

    The flag matches the connection, so it is absent - and it stays absent when
    the browser on the LAN claims otherwise, because no peer is trusted to
    speak for the client here. That is the whole of the Secure-cookie story on
    this path: the fix is a proxy in front, not a setting.
    """
    marker = tmp_path / "dockerenv"
    marker.touch()
    monkeypatch.setattr(updater_module, "_DOCKER_MARKER", marker)
    monkeypatch.setattr(autostart_module, "_DOCKER_MARKER", marker)
    app = create_app(_settings(tmp_path), command_runner=NeverRuns())
    async with LifespanManager(app):
        transport = ASGITransport(app=app, client=_PEER)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            plain = await client.post("/api/auth/login", json={"password": _PASSWORD})
            assert "secure" not in plain.headers["set-cookie"].lower()

            forged = await client.post(
                "/api/auth/login",
                json={"password": _PASSWORD},
                headers={"X-Forwarded-Proto": "https"},
            )
            assert "secure" not in forged.headers["set-cookie"].lower()
