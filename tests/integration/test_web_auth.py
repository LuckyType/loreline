"""Tests for web auth: login, cookie, and protected routes."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

from asgi_lifespan import LifespanManager
from fastapi.routing import APIRoute
from httpx import ASGITransport, AsyncClient

from loreline.settings import Settings
from loreline.web.app import create_app
from loreline.web.auth import require_auth


async def test_protected_route_requires_auth(auth_client: AsyncClient) -> None:
    resp = await auth_client.get("/api/providers")
    assert resp.status_code == 401
    # Pinned because the cookie now arrives via a security scheme: that must stay
    # descriptive (``auto_error=False``) rather than raising its own 403 first.
    assert resp.json() == {"detail": "authentication required"}


async def test_login_bad_password(auth_client: AsyncClient) -> None:
    resp = await auth_client.post("/api/auth/login", json={"password": "wrong"})
    assert resp.status_code == 401


async def test_login_then_access(auth_client: AsyncClient) -> None:
    login = await auth_client.post("/api/auth/login", json={"password": "hunter2"})
    assert login.status_code == 200
    assert "loreline_token" in login.cookies

    resp = await auth_client.get("/api/providers")
    assert resp.status_code == 200
    assert resp.json() == []


async def test_logout_clears_cookie(auth_client: AsyncClient) -> None:
    await auth_client.post("/api/auth/login", json={"password": "hunter2"})
    await auth_client.post("/api/auth/logout")
    resp = await auth_client.get("/api/providers")
    assert resp.status_code == 401


# The cookie's Secure flag. This deployment serves the app two ways at once:
# plain HTTP straight onto its published port (the primary path), and HTTPS via
# the bundled Caddy, which then speaks plain HTTP to the app. Both look like
# "http" to the app, so the flag comes from X-Forwarded-Proto - and because that
# published port is reachable without going through Caddy at all, the header is
# only believed from a peer the operator listed as a proxy.


@asynccontextmanager
async def _login_client(
    settings: Settings, *, base_url: str = "http://test", peer: str = "127.0.0.1"
) -> AsyncGenerator[AsyncClient, None]:
    """A client whose scheme and apparent source address the test chooses."""
    app = create_app(settings)
    async with LifespanManager(app):
        transport = ASGITransport(app=app, client=(peer, 45678))
        async with AsyncClient(transport=transport, base_url=base_url) as ac:
            yield ac


async def _login_cookie(client: AsyncClient, forwarded_proto: str | None = None) -> str:
    """Log in, optionally claiming a forwarded scheme, and return the Set-Cookie."""
    headers = {} if forwarded_proto is None else {"X-Forwarded-Proto": forwarded_proto}
    resp = await client.post("/api/auth/login", json={"password": "hunter2"}, headers=headers)
    assert resp.status_code == 200
    return resp.headers["set-cookie"].lower()


async def test_the_cookie_is_not_secure_over_plain_http(auth_client: AsyncClient) -> None:
    """The LAN path is plain HTTP; a Secure cookie there is never sent back."""
    assert "secure" not in await _login_cookie(auth_client)


async def test_the_cookie_is_secure_over_a_direct_https_connection(
    auth_settings: Settings,
) -> None:
    async with _login_client(auth_settings, base_url="https://test") as client:
        assert "secure" in await _login_cookie(client)


async def test_a_forwarded_https_claim_alone_does_not_mark_the_cookie_secure(
    auth_settings: Settings,
) -> None:
    """Nothing is trusted by default, and the header is anyone's to send."""
    async with _login_client(auth_settings) as client:
        assert "secure" not in await _login_cookie(client, "https")


async def test_a_forwarded_https_claim_from_an_untrusted_peer_is_ignored(
    tmp_path: Path,
) -> None:
    """A LAN client on the published port is not the proxy, whatever it says."""
    settings = Settings(
        data_dir=tmp_path / "data",
        auth_password="hunter2",
        jwt_secret="test-secret",
        trusted_proxies="172.16.0.0/12",
    )
    async with _login_client(settings, peer="192.168.1.50") as client:
        assert "secure" not in await _login_cookie(client, "https")


async def test_a_forwarded_https_claim_from_the_proxy_marks_the_cookie_secure(
    tmp_path: Path,
) -> None:
    """Behind the TLS front end the browser's hop was HTTPS, so the flag belongs."""
    settings = Settings(
        data_dir=tmp_path / "data",
        auth_password="hunter2",
        jwt_secret="test-secret",
        trusted_proxies="10.0.0.0/8, 127.0.0.0/8",
    )
    async with _login_client(settings) as client:  # peer 127.0.0.1, a listed proxy
        assert "secure" in await _login_cookie(client, "https")


async def test_the_proxy_reporting_plain_http_leaves_the_cookie_unsecured(
    tmp_path: Path,
) -> None:
    """Caddy's :80 listener forwards http, and that cookie has to keep working."""
    settings = Settings(
        data_dir=tmp_path / "data",
        auth_password="hunter2",
        jwt_secret="test-secret",
        trusted_proxies="127.0.0.0/8",
    )
    async with _login_client(settings) as client:
        assert "secure" not in await _login_cookie(client, "http")


async def test_auth_disabled_allows_access(client: AsyncClient) -> None:
    # Default fixture uses an empty password -> auth disabled.
    resp = await client.get("/api/providers")
    assert resp.status_code == 200


async def test_login_rate_limited_after_repeated_failures(auth_client: AsyncClient) -> None:
    for _ in range(5):
        resp = await auth_client.post("/api/auth/login", json={"password": "wrong"})
        assert resp.status_code == 401
    # 6th attempt is blocked outright, even with the correct password.
    resp = await auth_client.post("/api/auth/login", json={"password": "hunter2"})
    assert resp.status_code == 429


# Who the backoff is counted against. Behind the bundled Caddy every browser on
# the LAN opens its socket from the proxy container's address, so a limiter
# keyed on the peer is a global one and five wrong passwords from any machine
# shut the login for the whole table. X-Forwarded-For says who really called,
# and is believed on the same terms as X-Forwarded-Proto above: only from a
# listed proxy, and only its own rightmost entry.


def _proxied_settings(tmp_path: Path) -> Settings:
    """Auth on, and the loopback peer the test client uses listed as the proxy."""
    return Settings(
        data_dir=tmp_path / "data",
        auth_password="hunter2",
        jwt_secret="test-secret",
        trusted_proxies="127.0.0.0/8",
    )


async def _attempt(client: AsyncClient, password: str, forwarded_for: str | None = None) -> int:
    """One login attempt, optionally claiming a forwarded client. Returns the status."""
    headers = {} if forwarded_for is None else {"X-Forwarded-For": forwarded_for}
    resp = await client.post("/api/auth/login", json={"password": password}, headers=headers)
    return resp.status_code


async def test_one_forwarded_client_locking_out_leaves_the_others_alone(
    tmp_path: Path,
) -> None:
    """The finding itself: a locked-out browser must not lock the table's out."""
    async with _login_client(_proxied_settings(tmp_path)) as client:
        for _ in range(5):
            assert await _attempt(client, "wrong", "192.168.1.10") == 401
        assert await _attempt(client, "hunter2", "192.168.1.10") == 429
        # A different machine at the same table, arriving through the same
        # proxy, still gets its five and logs in on the first try.
        assert await _attempt(client, "hunter2", "192.168.1.11") == 200


async def test_only_the_proxys_own_entry_counts(tmp_path: Path) -> None:
    """Prefixing the header cannot buy a fresh bucket: the last entry is the proxy's."""
    async with _login_client(_proxied_settings(tmp_path)) as client:
        for hop in range(5):
            assert await _attempt(client, "wrong", f"203.0.113.{hop}, 192.168.1.10") == 401
        assert await _attempt(client, "hunter2", "198.51.100.7, 192.168.1.10") == 429


async def test_the_forwarded_client_is_ignored_when_the_peer_is_not_a_proxy(
    tmp_path: Path,
) -> None:
    """Nothing is trusted by default, so the header is just an unverified claim."""
    settings = Settings(
        data_dir=tmp_path / "data",
        auth_password="hunter2",
        jwt_secret="test-secret",
    )
    async with _login_client(settings, peer="192.168.1.50") as client:
        for attempt in range(5):
            assert await _attempt(client, "wrong", f"10.0.0.{attempt}") == 401
        # All five landed on the peer's own bucket, whatever they claimed.
        assert await _attempt(client, "hunter2", "10.0.0.99") == 429


async def test_an_unparseable_forwarded_client_falls_back_to_the_peer(
    tmp_path: Path,
) -> None:
    """Junk is free to generate; a key per request would defeat the limiter."""
    async with _login_client(_proxied_settings(tmp_path)) as client:
        for attempt in range(5):
            assert await _attempt(client, "wrong", f"not-an-address-{attempt}") == 401
        assert await _attempt(client, "hunter2", "still-not-an-address") == 429


# The routers gate routes with a plain ``Depends(require_auth)``, which FastAPI
# cannot recognise as a security scheme on its own. ``require_auth`` therefore
# pulls the cookie through ``Security(session_cookie)``, and these tests pin the
# result: a new route added without auth metadata fails here rather than
# quietly shipping a schema that says the API is open.

# Deliberately unauthenticated: the liveness probe (which says only that the
# process is up - /healthz and its snapshot are behind auth), the static
# capability config the login screen needs to render, the login/logout pair,
# and the two first-run routes. The last two cannot be behind auth by
# definition: an unclaimed instance has no password, so there is nobody to
# authenticate, and these are what the browser reads and posts to give it one.
# What keeps them safe is the setup code, not a session - see
# loreline.web.setup, and tests/integration/test_web_first_run.py for what they
# refuse and what they never reveal.
PUBLIC_OPERATIONS = {
    ("/api/system/livez", "get"),
    ("/api/capabilities", "get"),
    ("/api/auth/login", "post"),
    ("/api/auth/logout", "post"),
    ("/api/setup/state", "get"),
    ("/api/setup/claim", "post"),
}


async def test_openapi_declares_the_cookie_security_scheme(client: AsyncClient) -> None:
    schema = (await client.get("/openapi.json")).json()
    schemes = schema["components"]["securitySchemes"]
    assert schemes["sessionCookie"]["type"] == "apiKey"
    assert schemes["sessionCookie"]["in"] == "cookie"
    assert schemes["sessionCookie"]["name"] == "loreline_token"


def test_openapi_security_matches_the_routes(settings: Settings) -> None:
    app = create_app(settings)
    schema = app.openapi()
    declared = {
        (path, method)
        for path, item in schema["paths"].items()
        for method, op in item.items()
        if op.get("security") == [{"sessionCookie": []}]
    }
    documented = {
        (path, method)
        for path, item in schema["paths"].items()
        for method, op in item.items()
        if isinstance(op, dict)
    }
    assert documented - declared == PUBLIC_OPERATIONS

    # And the schema tracks the code rather than a list maintained by hand:
    # every route whose dependencies include ``require_auth`` is in there.
    guarded = {
        (route.path, method.lower())
        for route in app.routes
        if isinstance(route, APIRoute)
        for method in route.methods
        if any(d.call is require_auth for d in route.dependant.dependencies)
    }
    assert guarded == declared
