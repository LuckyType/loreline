"""Tests for provider CRUD, secret storage, and glossary routes."""

from __future__ import annotations

from httpx import AsyncClient


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
    assert "nova-3" in resp.json()


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
