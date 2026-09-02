"""The capability endpoint the browser filters its pickers with.

What these guard is the contract the frontend relies on. Before this endpoint
existed the same facts were mirrored by hand in TypeScript and had drifted, so
the failure mode is not a crash: it is a picker that offers a provider which
cannot serve the job, or hides one that can.
"""

from __future__ import annotations

from httpx import AsyncClient

from loreline.capabilities import config
from loreline.models import Interaction, ProviderKind


async def test_capabilities_describe_every_provider_kind(client: AsyncClient) -> None:
    response = await client.get("/api/capabilities")
    assert response.status_code == 200
    payload = response.json()
    assert set(payload["providers"]) == {k.value for k in ProviderKind}


async def test_payload_matches_the_loaded_config(client: AsyncClient) -> None:
    """The wire format is the config itself, not a hand-written projection.

    A second shape here would be a second source of truth, which is the thing
    this whole endpoint exists to remove.
    """
    response = await client.get("/api/capabilities")
    assert response.json() == config().model_dump(mode="json")


async def test_verified_models_are_offered(client: AsyncClient) -> None:
    """`hidden` is the release gate an unverified connector sits behind.

    gemini-3.5-transcribe-live spent its unverified life hidden: described in
    full, so an explicit config still routed to the Live connector, but absent
    from every picker. It is verified against the real service now, so both
    Gemini transcription models are offered.
    """
    payload = (await client.get("/api/capabilities")).json()
    gemini = payload["providers"]["gemini"]
    live = next(m for m in gemini["models"] if m["id"] == "gemini-3.5-transcribe-live")
    assert live["hidden"] is False
    offered = [m["id"] for m in gemini["models"] if not m["hidden"]]
    assert offered == ["gemini-3.5-transcribe", "gemini-3.5-transcribe-live"]


async def test_glossary_support_is_stated_per_model(client: AsyncClient) -> None:
    """The "Use glossary" checkbox needs to know when it would do nothing.

    OpenRouter accepts a `prompt` field on transcription and ignores it, so a
    checkbox there is a silent no-op. Deepgram's field name also differs per
    model, which is why this is not a per-provider flag.
    """
    payload = (await client.get("/api/capabilities")).json()

    def glossary(kind: str, model_id: str) -> dict[str, object]:
        provider = payload["providers"][kind]
        model = next(m for m in provider["models"] if m["id"] == model_id)
        return model["transcribe"]["glossary"]

    assert glossary("deepgram", "nova-3")["field"] == "keyterm"
    assert glossary("deepgram", "nova-2")["field"] == "keywords"
    assert glossary("assemblyai", "universal-3-5-pro")["max_terms_realtime"] == 100
    assert glossary("openrouter", "x-ai/grok-stt-1.0")["supported"] is False


async def test_video_parameters_are_present_for_the_modal(client: AsyncClient) -> None:
    """Durations and resolutions drive the generate-video form directly."""
    payload = (await client.get("/api/capabilities")).json()
    models = payload["providers"]["openrouter"]["models"]
    veo = next(m for m in models if m["id"] == "google/veo-3.1")
    assert veo["video"]["durations"] == [4, 6, 8]
    assert veo["video"]["resolutions"]
    assert veo["video"]["aspect_ratios"]


async def test_reasoning_efforts_are_listed_per_model(client: AsyncClient) -> None:
    """Per-model, because the accepted values genuinely differ."""
    payload = (await client.get("/api/capabilities")).json()
    models = {m["id"]: m for m in payload["providers"]["openrouter"]["models"]}
    mandatory = models["z-ai/glm-5.3-flash"]["llm"]["reasoning"]
    assert mandatory["mandatory"] is True
    assert "none" not in mandatory["efforts"]
    # Reasons, but exposes no effort levels: the dropdown must be hidden, not
    # rendered empty.
    always_on = models["minimax/minimax-m3"]["llm"]["reasoning"]
    assert always_on["supported"] is True
    assert always_on["efforts"] == []


async def test_live_capture_exclusion_is_visible_to_the_ui(client: AsyncClient) -> None:
    """OpenRouter may re-process stored audio but never drive a live session."""
    payload = (await client.get("/api/capabilities")).json()
    assert payload["providers"]["openrouter"]["live_capture"] is False
    assert payload["providers"]["deepgram"]["live_capture"] is True
    assert Interaction.TRANSCRIBE.value in payload["providers"]["openrouter"]["interactions"]
