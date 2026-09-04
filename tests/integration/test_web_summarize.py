"""Tests for the session summarize route (LLM provider + on-demand model)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient
from test_web_session import FakeBackend, capture_factory, fake_diarizers

import loreline.web.routes.sessions as sessions_route
from loreline.llm import LLMError
from loreline.settings import Settings
from loreline.web.app import create_app

# Any model id: the fake backend never looks at it, but the API requires one -
# a provider row carries no model, so the request is where it is decided.
_MODEL = "fake-model"


@pytest_asyncio.fixture
async def client(tmp_path: Path) -> AsyncIterator[AsyncClient]:
    settings = Settings(data_dir=tmp_path / "data", auth_password="", jwt_secret="x")
    app = create_app(
        settings,
        capture_factory=capture_factory,  # type: ignore[arg-type]
        backend_factory=FakeBackend,  # type: ignore[arg-type]
        diarizer_factory=fake_diarizers,
    )
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac


async def _session_with_transcript(client: AsyncClient) -> str:
    """Run a short capture so the session has a persisted transcript."""
    stt = (
        await client.post(
            "/api/providers",
            json={"name": "STT", "kind": "openai_compat"},
        )
    ).json()["id"]
    sid = (
        await client.post("/api/session/start", json={"primary_provider": stt, "model": _MODEL})
    ).json()["id"]
    await client.post("/api/session/stop")
    return sid


async def _llm_provider(client: AsyncClient) -> str:
    return (
        await client.post(
            "/api/providers",
            json={
                "name": "LLM",
                "kind": "openai_compat",
                "base_url": "http://llm:1234/v1",
            },
        )
    ).json()["id"]


async def test_summarize_uses_configured_system_prompt(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The summary system prompt saved in settings is handed to the LLM call;
    with nothing saved, none is passed (the built-in default applies)."""
    seen: dict[str, object] = {}

    async def fake_summarize(**kwargs: object) -> str:
        seen.clear()
        seen.update(kwargs)
        return "ok"

    monkeypatch.setattr(sessions_route, "summarize_transcript", fake_summarize)
    sid = await _session_with_transcript(client)
    llm = await _llm_provider(client)

    body = {"provider_id": llm, "model": "gpt-5.6-luna"}
    resp = await client.post(f"/api/session/{sid}/summarize", json=body)
    assert resp.status_code == 200
    assert seen["system_prompt"] is None  # nothing configured -> built-in default

    await client.put("/api/system/defaults", json={"summarize_prompt": "Nur Stichpunkte."})
    resp = await client.post(f"/api/session/{sid}/summarize", json=body)
    assert resp.status_code == 200
    assert seen["system_prompt"] == "Nur Stichpunkte."


async def test_summarize_session(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_summarize(**_kwargs: object) -> str:
        return "The party fought a dragon."

    monkeypatch.setattr(sessions_route, "summarize_transcript", fake_summarize)

    sid = await _session_with_transcript(client)
    llm = await _llm_provider(client)

    resp = await client.post(
        f"/api/session/{sid}/summarize", json={"provider_id": llm, "model": "gpt-4o-mini"}
    )
    assert resp.status_code == 200
    assert resp.json()["summary"] == "The party fought a dragon."

    # Persisted on the session.
    detail = await client.get(f"/api/session/{sid}")
    assert detail.json()["session"]["summary"] == "The party fought a dragon."


async def test_summarize_surfaces_upstream_error_as_502(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_summarize(**_kwargs: object) -> str:
        raise LLMError("The model `gpt-5.6-terra` does not exist.")

    monkeypatch.setattr(sessions_route, "summarize_transcript", fake_summarize)

    sid = await _session_with_transcript(client)
    llm = await _llm_provider(client)

    resp = await client.post(
        f"/api/session/{sid}/summarize", json={"provider_id": llm, "model": "gpt-5.6-terra"}
    )
    assert resp.status_code == 502
    assert "gpt-5.6-terra" in resp.json()["detail"]

    # Not persisted - the session still has no summary.
    detail = await client.get(f"/api/session/{sid}")
    assert detail.json()["session"]["summary"] is None


async def test_summarize_rejects_non_llm_provider(client: AsyncClient) -> None:
    sid = await _session_with_transcript(client)
    stt = (
        await client.post(
            "/api/providers",
            # Deepgram transcribes only; openai_compat now summarizes too.
            json={"name": "STT2", "kind": "deepgram"},
        )
    ).json()["id"]
    resp = await client.post(
        f"/api/session/{sid}/summarize", json={"provider_id": stt, "model": "nova-3"}
    )
    assert resp.status_code == 400


async def test_summarize_unknown_session(client: AsyncClient) -> None:
    llm = await _llm_provider(client)
    resp = await client.post(
        "/api/session/nope/summarize", json={"provider_id": llm, "model": "gpt-5.6-luna"}
    )
    assert resp.status_code == 404


async def test_summarize_with_openrouter_provider(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """OpenRouter is an LLM kind too - the route accepts it and records the
    ``vendor/model`` id the request named as the summary's provenance.

    Recording what was asked for is now the same thing as recording what ran:
    the route hands the summarizer that model and nothing re-derives it, so the
    two cannot disagree the way a duplicated fallback chain let them."""

    async def fake_summarize(**_kwargs: object) -> str:
        return "The party bargained with a dragon."

    monkeypatch.setattr(sessions_route, "summarize_transcript", fake_summarize)

    sid = await _session_with_transcript(client)
    llm = (
        await client.post(
            "/api/providers",
            json={"name": "OpenRouter", "kind": "openrouter"},
        )
    ).json()["id"]

    resp = await client.post(
        f"/api/session/{sid}/summarize",
        json={"provider_id": llm, "model": "openai/gpt-5.6-luna"},
    )
    assert resp.status_code == 200

    detail = (await client.get(f"/api/session/{sid}")).json()["session"]
    assert detail["summary"] == "The party bargained with a dragon."
    assert detail["summary_model"] == "openai/gpt-5.6-luna"
