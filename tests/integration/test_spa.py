"""Tests that the FastAPI app serves the built SPA with client-side fallback."""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from loreline.web.spa import spa_directory

_HAS_SPA = spa_directory() is not None
pytestmark = pytest.mark.skipif(_HAS_SPA is False, reason="frontend build not present")


async def test_index_served(client: AsyncClient) -> None:
    resp = await client.get("/")
    assert resp.status_code == 200
    assert "Loreline" in resp.text


async def test_deep_link_falls_back_to_index(client: AsyncClient) -> None:
    resp = await client.get("/sessions/does-not-exist")
    assert resp.status_code == 200
    assert "<html" in resp.text.lower()


async def test_api_still_takes_precedence(client: AsyncClient) -> None:
    resp = await client.get("/api/system/healthz")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


async def test_the_audio_worklet_is_served_as_javascript(client: AsyncClient) -> None:
    """The browser fetches this one by URL, so the SPA mount has to hand it over.

    An ``AudioWorklet`` processor cannot be bundled into the app's module graph
    (``addModule`` takes a URL and the browser runs the script on the audio
    thread), so the client microphone depends on a static file surviving both
    the SvelteKit build and this mount - and on it arriving with a JavaScript
    content type, because a worklet served as anything else is refused outright.
    """
    resp = await client.get("/client-mic-worklet.js")
    assert resp.status_code == 200
    assert "javascript" in resp.headers["content-type"]
    assert "registerProcessor" in resp.text


async def test_hashed_assets_are_cached_for_good_and_the_shell_is_not(client: AsyncClient) -> None:
    """A file named by its content hash never changes; the page that names it does.

    The shell has to be revalidated on every visit, or a browser would keep
    asking for the previous build's hashed files after an update.
    """
    directory = spa_directory()
    assert directory is not None
    asset = next((directory / "_app" / "immutable").rglob("*.js"))

    hashed = await client.get("/" + asset.relative_to(directory).as_posix())
    shell = await client.get("/")
    missing = await client.get("/_app/immutable/no-such-file.js")

    assert hashed.status_code == 200
    assert "immutable" in hashed.headers["cache-control"]
    assert "immutable" not in shell.headers.get("cache-control", "")
    assert "immutable" not in missing.headers.get("cache-control", "")
