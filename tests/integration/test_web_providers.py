"""Tests for provider CRUD, secret storage, and glossary routes."""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from loreline.capabilities import filter_models
from loreline.catalog import CatalogProbe, CatalogStatus
from loreline.health import HealthReport, HealthStatus
from loreline.models import Interaction, ModelInfo, ProviderConfig, ProviderKind
from loreline.stt.catalog import ModelListing


def _provider_body() -> dict[str, object]:
    return {
        "name": "Local Whisper",
        "kind": "openai_compat",
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


async def test_a_whitespace_api_key_is_not_a_credential(client: AsyncClient) -> None:
    """Reproduced through the wizard: three spaces in the key field, "Save
    anyway" on the "No API key" dialog, and the row came back looking exactly
    like a credentialed one - "•••" in the API key column, no warning on the way
    back in, "blank = keep current" under the field, Load models enabled - while
    every request built from it died with ``Illegal header value b'Bearer    '``.

    Whitespace must therefore land where an empty field already landed: no
    secret written, and a row that honestly reports having none.
    """
    body = {**_provider_body(), "kind": "openai", "api_key": "   "}
    created = await client.post("/api/providers", json=body)

    assert created.status_code == 201
    assert created.json()["secret_set"] is False
    assert created.json()["secret_hint"] is None

    # And the same on the way through update, which shares the payload model.
    pid = created.json()["id"]
    updated = await client.put(f"/api/providers/{pid}", json={**body, "api_key": " \n\t "})
    assert updated.status_code == 200
    assert updated.json()["secret_set"] is False


async def test_a_real_key_is_stored_with_its_pasted_whitespace_trimmed(
    client: AsyncClient,
) -> None:
    """The other half of the same rule. A key arrives pasted, and a browser
    field or a password manager brings a trailing newline with it; the header it
    would build is illegal, so the key is stored as it will actually be sent."""
    body = {**_provider_body(), "kind": "openai", "api_key": "  sk-123\n"}
    pid = (await client.post("/api/providers", json=body)).json()["id"]

    listed = (await client.get("/api/providers")).json()
    saved = next(p for p in listed if p["id"] == pid)
    assert saved["secret_set"] is True
    # The hint masks all but the ends of what is stored, so it is also how a
    # test sees that the padding did not come along: an untrimmed value would
    # show its spaces and its newline here.
    assert saved["secret_hint"] == "sk…23"


async def test_the_secret_route_refuses_a_blank_value(client: AsyncClient) -> None:
    """The API has to be safe whichever client is calling, and this route means
    "store this key": there is nothing to normalise a blank one into, and
    writing nothing while answering ``ok`` would be the same lie by another
    route."""
    pid = (await client.post("/api/providers", json=_provider_body())).json()["id"]
    refused = await client.post(f"/api/providers/{pid}/secret", json={"value": "   "})
    assert refused.status_code == 422
    assert (await client.get("/api/providers")).json()[0]["secret_set"] is False


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


# --- POST /providers/models, and why an empty list is not always an answer --


def _dead_catalogue(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every catalogue read fails the way an unlistening port does."""

    async def unreachable(
        kind: ProviderKind, interaction: Interaction, **_kwargs: object
    ) -> CatalogProbe:
        return CatalogProbe(
            kind,
            interaction,
            "http://10.10.50.55:9911/v1/models",
            CatalogStatus.UNREACHABLE,
            "could not check: ConnectError: All connection attempts failed",
        )

    monkeypatch.setattr("loreline.stt.catalog.probe", unreachable)


async def test_an_unreachable_catalogue_reports_the_reason_rather_than_nothing(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A self-hosted base URL pointing at nothing used to answer ``200 []``.

    That is the same answer a vendor with no models gives, so the wizard's
    "Load models" button showed "Loading…", went back to "Load models" and left
    the operator to guess between a wrong host, a wrong port, a stopped service
    and a provider with nothing to offer. The probe knew which it was all along
    and only ever wrote it to the log.
    """
    _dead_catalogue(monkeypatch)

    resp = await client.post(
        "/api/providers/models",
        json={"kind": "openai_compat", "base_url": "http://10.10.50.55:9911/v1"},
    )

    assert resp.status_code == 502
    assert "All connection attempts failed" in resp.json()["detail"]


async def test_a_kind_with_a_curated_list_still_answers_when_the_vendor_is_down(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fail soft is unchanged where it can be: the curated fallback is the
    answer, not a consolation prize, so a picker with something to show hears
    nothing about the failed read. Only an empty list has to say why."""
    _dead_catalogue(monkeypatch)

    resp = await client.post("/api/providers/models", json={"kind": "openai"})

    assert resp.status_code == 200
    assert "gpt-transcribe" in [m["id"] for m in resp.json()]


async def test_a_provider_with_nowhere_to_look_is_not_an_error(client: AsyncClient) -> None:
    """The other empty list: a self-hosted row with no base URL yet has no
    catalogue address at all, so nothing was asked and nothing failed. It says
    "no models", which is exactly what it means."""
    resp = await client.post("/api/providers/models", json={"kind": "openai_compat"})
    assert resp.status_code == 200
    assert resp.json() == []


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

    async def fake_list_catalog(**kwargs: object) -> ModelListing:
        return ModelListing(
            filter_models(
                listed,
                kind=ProviderKind.OPENAI,
                interaction=Interaction.TRANSCRIBE,
                strict=bool(kwargs["strict_filtering"]),
            )
        )

    monkeypatch.setattr("loreline.web.routes.providers.list_catalog", fake_list_catalog)
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
    body = {"name": "Gemini", "kind": "gemini"}
    pid = (await client.post("/api/providers", json=body)).json()["id"]

    resp = await client.post(f"/api/providers/{pid}/test")

    assert resp.status_code == 200
    assert resp.json() == {
        "status": "unauthorized",
        "detail": "no API key stored for this provider",
        "interaction": None,
        "transport": None,
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
    body = {"name": "Gemini", "kind": "gemini", "api_key": "bad"}
    pid = (await client.post("/api/providers", json=body)).json()["id"]

    resp = await client.post(f"/api/providers/{pid}/test")

    assert resp.status_code == 200
    assert resp.json() == {
        "status": "unauthorized",
        "detail": "API key not valid.",
        "interaction": None,
        "transport": None,
    }
    assert seen == [(ProviderKind.GEMINI, "bad")]


async def test_test_route_says_which_surface_the_verdict_is_about(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One probe per row (ADR 0004) grades a summarizing kind on its chat
    surface, so a "healthy" Gemini row says nothing about transcription. The
    surface rides along untouched, which is what lets the page print "healthy,
    summarize surface" rather than let the word stand for the whole row."""

    async def fake_probe(config: ProviderConfig, api_key: str | None) -> HealthReport:
        return HealthReport(HealthStatus.HEALTHY, interaction=Interaction.SUMMARIZE)

    monkeypatch.setattr("loreline.web.routes.providers.probe_provider", fake_probe)
    body = {"name": "Gemini", "kind": "gemini", "api_key": "good"}
    pid = (await client.post("/api/providers", json=body)).json()["id"]

    resp = await client.post(f"/api/providers/{pid}/test")

    assert resp.status_code == 200
    assert resp.json() == {
        "status": "healthy",
        "detail": None,
        "interaction": "summarize",
        "transport": None,
    }


async def test_test_route_404s_only_for_a_missing_provider(client: AsyncClient) -> None:
    assert (await client.post("/api/providers/nope/test")).status_code == 404
