"""Tests for provider CRUD, secret storage, and glossary routes."""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from loreline.capabilities import filter_models
from loreline.health import HealthReport, HealthStatus
from loreline.models import Interaction, ModelInfo, ProviderConfig, ProviderKind


def _provider_body() -> dict[str, object]:
    return {
        "name": "Local Whisper",
        "kind": "openai_compat",
        "protocol": "http_batch",
        "base_url": "http://localhost:9000",
        "model": "whisper-1",
        "sample_rate": 16000,
    }


async def test_provider_crud(client: AsyncClient) -> None:
    created = await client.post("/api/providers", json=_provider_body())
    assert created.status_code == 201
    provider = created.json()
    pid = provider["id"]
    assert provider["name"] == "Local Whisper"
    assert provider["auth_ref"] == f"provider:{pid}"

    listed = await client.get("/api/providers")
    assert listed.status_code == 200
    assert [p["id"] for p in listed.json()] == [pid]

    updated = await client.put(
        f"/api/providers/{pid}", json={**_provider_body(), "name": "Renamed"}
    )
    assert updated.status_code == 200
    assert updated.json()["name"] == "Renamed"

    deleted = await client.delete(f"/api/providers/{pid}")
    assert deleted.status_code == 200
    assert (await client.get("/api/providers")).json() == []


async def test_update_missing_provider(client: AsyncClient) -> None:
    resp = await client.put("/api/providers/nope", json=_provider_body())
    assert resp.status_code == 404


async def test_set_secret(client: AsyncClient) -> None:
    pid = (await client.post("/api/providers", json=_provider_body())).json()["id"]
    resp = await client.post(f"/api/providers/{pid}/secret", json={"value": "sk-123"})
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


async def test_provider_models_curated(client: AsyncClient) -> None:
    # Deepgram has no /v1/models endpoint -> the route returns the curated catalog
    # (no network involved).
    resp = await client.post("/api/providers/models", json={"kind": "deepgram"})
    assert resp.status_code == 200
    models = resp.json()
    assert "nova-3" in [m["id"] for m in models]
    # A curated entry publishes no price or context length - the pickers must
    # get nulls, never a zero that would render as "free".
    assert all(m["pricing"] is None and m["context_length"] is None for m in models)


async def test_favorite_models_persist(client: AsyncClient) -> None:
    body = {**_provider_body(), "favorite_models": ["nova-3", "nova-2"]}
    pid = (await client.post("/api/providers", json=body)).json()["id"]
    listed = (await client.get("/api/providers")).json()
    saved = next(p for p in listed if p["id"] == pid)
    assert saved["favorite_models"] == ["nova-3", "nova-2"]


async def test_glossary_roundtrip(client: AsyncClient) -> None:
    empty = await client.get("/api/glossary/camp-1")
    assert empty.status_code == 200
    assert empty.json()["terms"] == []

    put = await client.put("/api/glossary/camp-1", json={"terms": ["Drizzt", "Faerûn"]})
    assert put.status_code == 200

    fetched = await client.get("/api/glossary/camp-1")
    assert fetched.json()["terms"] == ["Drizzt", "Faerûn"]


async def test_default_glossary_roundtrip(client: AsyncClient) -> None:
    assert (await client.get("/api/glossary")).json()["terms"] == []
    put = await client.put("/api/glossary", json={"terms": ["Aurora", "Mistwood"]})
    assert put.status_code == 200
    assert put.json()["campaign_id"] == "_default"
    assert (await client.get("/api/glossary")).json()["terms"] == ["Aurora", "Mistwood"]


async def test_model_filtering_setting_controls_what_the_picker_offers(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The "only show compatible models" setting has to reach the models route,
    not just exist in the settings payload - off means an operator sees every
    model their endpoint offers, including ones too new to be recognised."""
    listed = [
        ModelInfo(id="gpt-4o"),
        ModelInfo(id="dall-e-3"),
        ModelInfo(id="whisper-1"),
    ]

    async def fake_list_models(**kwargs: object) -> list[ModelInfo]:
        return filter_models(
            listed,
            kind=ProviderKind.OPENAI,
            interaction=Interaction.TRANSCRIBE,
            strict=bool(kwargs["strict_filtering"]),
        )

    monkeypatch.setattr("loreline.web.routes.providers.list_models", fake_list_models)
    body = {"kind": "openai", "interaction": "transcribe"}

    # Default (strict): the image and chat models are hidden.
    resp = await client.post("/api/providers/models", json=body)
    assert [m["id"] for m in resp.json()] == ["whisper-1"]

    # Turned off: everything the endpoint reports comes through.
    defaults = (await client.get("/api/system/defaults")).json()
    defaults["strict_model_filtering"] = False
    assert (await client.put("/api/system/defaults", json=defaults)).status_code == 200

    resp = await client.post("/api/providers/models", json=body)
    assert [m["id"] for m in resp.json()] == ["gpt-4o", "dall-e-3", "whisper-1"]


# --- POST /providers/{id}/test ---------------------------------------------
#
# The Test button. It used to answer a single boolean and answer it wrong: a
# provider with a completely invalid key reported healthy, because the probe
# graded ``status_code < 500`` and every vendor rejects a key well below that.
# These pin the states that replaced it and, more importantly, pin that the
# route never turns a probe failure into an HTTP error - the page can render a
# state, it cannot render a 400.


async def test_test_route_reports_a_missing_key_without_calling_out(
    client: AsyncClient,
) -> None:
    """No key stored, no network call, and it says which of the two it is.

    A cloud kind cannot succeed without a credential, so there is nothing to
    ask; and asking anyway would be actively misleading for Gemini, whose
    OpenAI-compatible /models answers a keyless request with 404 - a wrong-URL
    status for what is really a missing key.
    """
    body = {"name": "Gemini", "kind": "gemini", "protocol": "http_batch"}
    pid = (await client.post("/api/providers", json=body)).json()["id"]

    resp = await client.post(f"/api/providers/{pid}/test")

    assert resp.status_code == 200
    assert resp.json() == {
        "status": "unauthorized",
        "detail": "no API key stored for this provider",
    }


async def test_test_route_hands_the_row_and_its_key_to_the_probe_and_renders_the_report(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The route builds nothing and decides nothing: it looks the row up, hands
    the probe the stored key, and renders whatever came back. "API key not
    valid" is worth vastly more to a GM than "down", so the detail travels
    untouched."""
    seen: list[tuple[ProviderKind, str | None]] = []

    async def fake_probe(config: ProviderConfig, api_key: str | None) -> HealthReport:
        seen.append((config.kind, api_key))
        return HealthReport(HealthStatus.UNAUTHORIZED, "API key not valid.")

    monkeypatch.setattr("loreline.web.routes.providers.probe_provider", fake_probe)
    body = {"name": "Gemini", "kind": "gemini", "protocol": "http_batch", "api_key": "bad"}
    pid = (await client.post("/api/providers", json=body)).json()["id"]

    resp = await client.post(f"/api/providers/{pid}/test")

    assert resp.status_code == 200
    assert resp.json() == {"status": "unauthorized", "detail": "API key not valid."}
    assert seen == [(ProviderKind.GEMINI, "bad")]


async def test_test_route_404s_only_for_a_missing_provider(client: AsyncClient) -> None:
    assert (await client.post("/api/providers/nope/test")).status_code == 404
