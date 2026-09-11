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
