"""The first run over the wire: the gate, the claim, and what leaks from neither."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient, Response

from loreline.secrets import SecretStore
from loreline.settings import Settings
from loreline.web.app import create_app
from loreline.web.setup import SETUP_CODE_NAME


@pytest.fixture
def unclaimed_settings(tmp_path: Path) -> Settings:
    """A brand new instance: the gate on, and no password from either source."""
    return Settings(
        data_dir=tmp_path / "data",
        auth_password="",
        jwt_secret="test-secret",
        first_run_setup=True,
    )


@pytest_asyncio.fixture
async def unclaimed(unclaimed_settings: Settings) -> AsyncIterator[AsyncClient]:
    app = create_app(unclaimed_settings)
    async with LifespanManager(app):
        transport = ASGITransport(app=app, client=("10.0.0.9", 45678))
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac


def _code(settings: Settings) -> str:
    """The code this instance minted at startup, read the way an operator would
    read it off the log - except from the store, which is where the log got it."""
    code = SecretStore(settings.secrets_path).get(SETUP_CODE_NAME)
    assert code is not None
    return code


async def _claim(
    client: AsyncClient, settings: Settings, password: str = "a-good-password"
) -> Response:
    return await client.post(
        "/api/setup/claim",
        json={
            "setup_code": _code(settings),
            "password": password,
            "password_confirm": password,
        },
    )


# --- the gate ---------------------------------------------------------------


async def test_an_unclaimed_instance_refuses_everything_but_its_setup(
    unclaimed: AsyncClient,
) -> None:
    """Without this the box would serve transcripts and keys to the LAN: an
    unclaimed instance has no password, so require_auth is a no-op on it."""
    for path in ("/api/providers", "/api/sessions", "/api/system/healthz", "/api/capabilities"):
        resp = await unclaimed.get(path)
        assert resp.status_code == 403, path
        assert "setup code" in resp.json()["detail"]


async def test_an_unclaimed_instance_still_answers_livez(unclaimed: AsyncClient) -> None:
    """An orchestrator polls it before anybody has opened a browser."""
    assert (await unclaimed.get("/api/system/livez")).status_code == 200


async def test_an_unclaimed_instance_refuses_its_sockets(unclaimed: AsyncClient) -> None:
    """A dependency on a router would not have covered these, and they carry a
    session's audio and its live transcript."""
    resp = await unclaimed.get("/ws/transcript", headers={"upgrade": "websocket"})
    assert resp.status_code == 403


async def test_an_open_instance_is_unchanged(client: AsyncClient) -> None:
    """The shared fixture's state, and the one ~1200 tests run under."""
    assert (await client.get("/api/providers")).status_code == 200
    assert (await client.get("/api/setup/state")).json()["state"] == "open"


async def test_a_claimed_instance_is_not_gated(auth_client: AsyncClient) -> None:
    """It answers 401 because auth is live, not 403 because setup is pending."""
    assert (await auth_client.get("/api/providers")).status_code == 401
    assert (await auth_client.get("/api/setup/state")).json()["state"] == "claimed"


# --- what the state route says, and what it does not ------------------------


async def test_the_state_route_answers_without_a_session(unclaimed: AsyncClient) -> None:
    body = (await unclaimed.get("/api/setup/state")).json()
    assert body == {"state": "unclaimed", "provider_configured": False, "wizard_complete": False}


async def test_the_setup_code_is_never_in_the_state(
    unclaimed: AsyncClient, unclaimed_settings: Settings
) -> None:
    """The code is the whole security argument; a route that hands it out for
    free would turn the claim back into "whoever gets here first"."""
    resp = await unclaimed.get("/api/setup/state")
    assert _code(unclaimed_settings) not in resp.text


async def test_the_setup_code_is_never_in_a_refusal(
    unclaimed: AsyncClient, unclaimed_settings: Settings
) -> None:
    code = _code(unclaimed_settings)
    blocked = await unclaimed.get("/api/providers")
    wrong = await unclaimed.post(
        "/api/setup/claim",
        json={
            "setup_code": "WRNG-CODE",
            "password": "a-good-password",
            "password_confirm": "a-good-password",
        },
    )
    assert wrong.status_code == 401
    for resp in (blocked, wrong):
        assert code not in resp.text
        assert code.replace("-", "") not in resp.text


# --- claiming ---------------------------------------------------------------


async def test_claiming_signs_the_browser_in_and_opens_the_gate(
    unclaimed: AsyncClient, unclaimed_settings: Settings
) -> None:
    resp = await _claim(unclaimed, unclaimed_settings)
    assert resp.status_code == 200
    assert resp.json()["state"] == "claimed"
    # Signed in, rather than bounced to a form to retype what was just chosen.
    assert "loreline_token" in resp.cookies
    listed = await unclaimed.get("/api/providers")
    assert listed.status_code == 200
    assert listed.json() == []


async def test_the_claimed_password_is_what_a_later_login_accepts(
    unclaimed: AsyncClient, unclaimed_settings: Settings
) -> None:
    await _claim(unclaimed, unclaimed_settings, "chosen-in-the-browser")
    await unclaimed.post("/api/auth/logout")
    assert (await unclaimed.get("/api/providers")).status_code == 401
    bad = await unclaimed.post("/api/auth/login", json={"password": "something-else"})
    assert bad.status_code == 401
    good = await unclaimed.post("/api/auth/login", json={"password": "chosen-in-the-browser"})
    assert good.status_code == 200
    assert (await unclaimed.get("/api/providers")).status_code == 200


async def test_the_password_survives_a_restart(unclaimed_settings: Settings) -> None:
    """A claim writes to the secret store, so the next process finds it there."""
    app = create_app(unclaimed_settings)
    async with LifespanManager(app):
        transport = ASGITransport(app=app, client=("10.0.0.9", 45678))
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            await _claim(ac, unclaimed_settings, "chosen-in-the-browser")

    restarted = Settings(
        data_dir=unclaimed_settings.data_dir,
        auth_password="",
        jwt_secret="test-secret",
        first_run_setup=True,
    )
    app = create_app(restarted)
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            assert (await ac.get("/api/setup/state")).json()["state"] == "claimed"
            login = await ac.post("/api/auth/login", json={"password": "chosen-in-the-browser"})
            assert login.status_code == 200
    # And the code that let anyone claim it is gone for good.
    assert SecretStore(restarted.secrets_path).get(SETUP_CODE_NAME) is None


async def test_the_environment_password_wins_over_the_stored_one(
    unclaimed_settings: Settings,
) -> None:
    """The one way back into an instance whose password was mistyped."""
    app = create_app(unclaimed_settings)
    async with LifespanManager(app):
        transport = ASGITransport(app=app, client=("10.0.0.9", 45678))
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            await _claim(ac, unclaimed_settings, "the-typo-nobody-can-repeat")

    rescued = Settings(
        data_dir=unclaimed_settings.data_dir,
        auth_password="set-on-the-host",
        jwt_secret="test-secret",
        first_run_setup=True,
    )
    app = create_app(rescued)
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            assert (
                await ac.post("/api/auth/login", json={"password": "the-typo-nobody-can-repeat"})
            ).status_code == 401
            assert (
                await ac.post("/api/auth/login", json={"password": "set-on-the-host"})
            ).status_code == 200


async def test_a_second_claim_is_refused(
    unclaimed: AsyncClient, unclaimed_settings: Settings
) -> None:
    await _claim(unclaimed, unclaimed_settings)
    again = await unclaimed.post(
        "/api/setup/claim",
        json={
            "setup_code": "ANY-CODE",
            "password": "another-password",
            "password_confirm": "another-password",
        },
    )
    assert again.status_code == 409


async def test_an_open_instance_cannot_be_claimed(client: AsyncClient) -> None:
    """Nothing to claim: the operator turned the gate off deliberately."""
    resp = await client.post(
        "/api/setup/claim",
        json={
            "setup_code": "ANY-CODE",
            "password": "a-good-password",
            "password_confirm": "a-good-password",
        },
    )
    assert resp.status_code == 409


# --- a wrong code, and the backoff it shares with the login route -----------


async def test_a_wrong_code_is_refused_and_then_rate_limited(unclaimed: AsyncClient) -> None:
    body = {
        "setup_code": "WRNG-CODE",
        "password": "a-good-password",
        "password_confirm": "a-good-password",
    }
    for _ in range(5):
        assert (await unclaimed.post("/api/setup/claim", json=body)).status_code == 401
    locked = await unclaimed.post("/api/setup/claim", json=body)
    assert locked.status_code == 429


async def test_the_backoff_is_shared_with_the_login_route(
    unclaimed: AsyncClient, unclaimed_settings: Settings
) -> None:
    """One limiter, so guessing cannot get a fresh budget by switching routes."""
    body = {
        "setup_code": "WRNG-CODE",
        "password": "a-good-password",
        "password_confirm": "a-good-password",
    }
    for _ in range(5):
        await unclaimed.post("/api/setup/claim", json=body)
    # Even the right code is now refused, rather than counted.
    assert (await _claim(unclaimed, unclaimed_settings)).status_code == 429


async def test_a_password_problem_does_not_spend_an_attempt(
    unclaimed: AsyncClient, unclaimed_settings: Settings
) -> None:
    """The budget exists to slow down guessing the code, not to punish a typo
    in a field the form already checks."""
    code = _code(unclaimed_settings)
    for _ in range(8):
        resp = await unclaimed.post(
            "/api/setup/claim",
            json={"setup_code": code, "password": "short", "password_confirm": "short"},
        )
        assert resp.status_code == 400
    assert (await _claim(unclaimed, unclaimed_settings)).status_code == 200


async def test_a_mismatched_password_is_refused_with_a_sentence(
    unclaimed: AsyncClient, unclaimed_settings: Settings
) -> None:
    resp = await unclaimed.post(
        "/api/setup/claim",
        json={
            "setup_code": _code(unclaimed_settings),
            "password": "a-good-password",
            "password_confirm": "a-good-passwrod",
        },
    )
    assert resp.status_code == 400
    assert resp.json()["detail"] == "the two passwords do not match"
    # Nothing was stored: the instance is still claimable.
    assert (await unclaimed.get("/api/setup/state")).json()["state"] == "unclaimed"


# --- the steps after the claim ----------------------------------------------


async def test_the_remaining_steps_are_reported_and_resumable(client: AsyncClient) -> None:
    before = (await client.get("/api/setup/state")).json()
    assert before["provider_configured"] is False
    assert before["wizard_complete"] is False

    await client.post(
        "/api/providers",
        json={"name": "dg", "kind": "deepgram", "api_key": "k", "favorite_models": []},
    )
    assert (await client.get("/api/setup/state")).json()["provider_configured"] is True

    done = await client.post("/api/setup/complete")
    assert done.status_code == 200
    assert done.json()["wizard_complete"] is True
    # And it stays done across a fresh read, which is what makes it resumable.
    assert (await client.get("/api/setup/state")).json()["wizard_complete"] is True


async def test_completing_needs_a_session_once_there_is_one(auth_client: AsyncClient) -> None:
    assert (await auth_client.post("/api/setup/complete")).status_code == 401
    await auth_client.post("/api/auth/login", json={"password": "hunter2"})
    assert (await auth_client.post("/api/setup/complete")).status_code == 200
