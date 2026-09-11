"""Campaign routes, and the generated texts that hang off a campaign."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from asgi_lifespan import LifespanManager
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from test_web_session import FakeBackend, capture_factory, fake_diarizers

import loreline.web.routes.campaigns as campaigns_route
import loreline.web.routes.sessions as sessions_route
from loreline.llm import LLMError
from loreline.settings import Settings
from loreline.web.app import create_app

_MODEL = "fake-model"


@pytest.fixture
def app(tmp_path: Path) -> FastAPI:
    settings = Settings(data_dir=tmp_path / "data", auth_password="", jwt_secret="x")
    return create_app(
        settings,
        capture_factory=capture_factory,  # type: ignore[arg-type]
        backend_factory=FakeBackend,  # type: ignore[arg-type]
        diarizer_factory=fake_diarizers,
    )


@pytest_asyncio.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac


async def _campaign(client: AsyncClient, name: str = "Curse of Strahd") -> str:
    resp = await client.post("/api/campaigns", json={"name": name})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _session_with_transcript(client: AsyncClient, campaign_id: str | None = None) -> str:
    """Run a short capture so the session has a persisted transcript."""
    stt = (
        await client.post("/api/providers", json={"name": "STT", "kind": "openai_compat"})
    ).json()["id"]
    body: dict[str, object] = {"primary_provider": stt, "model": _MODEL}
    if campaign_id:
        body["campaign_id"] = campaign_id
    sid = (await client.post("/api/session/start", json=body)).json()["id"]
    await client.post("/api/session/stop")
    return sid


async def _llm_provider(client: AsyncClient) -> str:
    return (
        await client.post(
            "/api/providers",
            json={"name": "LLM", "kind": "openai_compat", "base_url": "http://llm:1234/v1"},
        )
    ).json()["id"]


# --- the campaigns themselves -------------------------------------------


async def test_campaign_crud(client: AsyncClient) -> None:
    created = await client.post("/api/campaigns", json={"name": "  Barovia  "})
    assert created.status_code == 201
    campaign_id = created.json()["id"]
    assert created.json()["name"] == "Barovia"  # trimmed on the way in

    listed = (await client.get("/api/campaigns")).json()
    assert [row["campaign"]["name"] for row in listed] == ["Barovia"]
    assert listed[0]["sessions"] == 0

    updated = await client.put(
        f"/api/campaigns/{campaign_id}",
        json={"name": "Barovia", "notes": "gloomy", "recap_prompt": "Auf Deutsch."},
    )
    assert updated.status_code == 200
    assert updated.json()["recap_prompt"] == "Auf Deutsch."

    assert (await client.get(f"/api/campaigns/{campaign_id}")).json()["notes"] == "gloomy"
    assert (await client.get("/api/campaigns/nope")).status_code == 404


async def test_a_blank_or_duplicate_name_is_refused(client: AsyncClient) -> None:
    assert (await client.post("/api/campaigns", json={"name": "   "})).status_code == 422
    await _campaign(client, "Barovia")
    assert (await client.post("/api/campaigns", json={"name": "Barovia"})).status_code == 409

    other = await _campaign(client, "Sigil")
    clash = await client.put(f"/api/campaigns/{other}", json={"name": "Barovia"})
    assert clash.status_code == 409
    # Renaming a campaign to the name it already has is not a clash with itself.
    assert (await client.put(f"/api/campaigns/{other}", json={"name": "Sigil"})).status_code == 200


async def test_deleting_a_campaign_keeps_its_sessions(client: AsyncClient) -> None:
    campaign_id = await _campaign(client)
    sid = await _session_with_transcript(client, campaign_id)

    assert (await client.delete(f"/api/campaigns/{campaign_id}")).status_code == 200
    assert (await client.get(f"/api/campaigns/{campaign_id}")).status_code == 404

    session = (await client.get(f"/api/session/{sid}")).json()["session"]
    assert session["campaign_id"] is None
    assert session["status"] == "completed"


async def test_a_session_starts_in_a_campaign_and_can_be_moved(client: AsyncClient) -> None:
    first = await _campaign(client, "Barovia")
    second = await _campaign(client, "Sigil")
    sid = await _session_with_transcript(client, first)

    assert (await client.get(f"/api/campaigns/{first}/sessions")).json()[0]["id"] == sid

    moved = await client.put(f"/api/session/{sid}/campaign", json={"campaign_id": second})
    assert moved.status_code == 200
    assert moved.json()["campaign_id"] == second
    assert (await client.get(f"/api/campaigns/{first}/sessions")).json() == []

    cleared = await client.put(f"/api/session/{sid}/campaign", json={"campaign_id": None})
    assert cleared.json()["campaign_id"] is None

    # A campaign nothing answers to is refused rather than stored, which is the
    # unresolvable id this whole feature exists to end.
    assert (
        await client.put(f"/api/session/{sid}/campaign", json={"campaign_id": "ghost"})
    ).status_code == 404


async def test_the_campaign_glossary_reaches_the_provider(client: AsyncClient) -> None:
    """A session in a campaign is transcribed with that campaign's terms.

    The merge rule is the glossary repository's (default list first, the
    campaign's appended); what this covers is that the campaign a session was
    started in is the one whose terms are asked for.
    """
    campaign_id = await _campaign(client)
    await client.put("/api/glossary", json={"terms": ["Aurora"]})
    await client.post(f"/api/campaigns/{campaign_id}/glossary/add", json={"terms": ["Strahd"]})

    effective = (await client.get(f"/api/glossary/{campaign_id}")).json()
    assert effective["terms"] == ["Strahd"]

    sid = await _session_with_transcript(client, campaign_id)
    assert (await client.get(f"/api/session/{sid}")).json()["session"]["campaign_id"] == campaign_id


async def test_glossary_add_appends_and_deduplicates(client: AsyncClient) -> None:
    campaign_id = await _campaign(client)
    await client.put(f"/api/glossary/{campaign_id}", json={"terms": ["Strahd"]})

    resp = await client.post(
        f"/api/campaigns/{campaign_id}/glossary/add",
        json={"terms": ["  Ireena  ", "strahd", "", "Vallaki"]},
    )
    assert resp.status_code == 200
    # Appended in order, trimmed, and a case-insensitive duplicate is dropped -
    # two spellings of one name only spend the provider's glossary ceiling.
    assert resp.json()["terms"] == ["Strahd", "Ireena", "Vallaki"]


# --- the cast at the table ------------------------------------------------


async def test_the_cast_rides_along_with_the_campaign(client: AsyncClient) -> None:
    """Created, read back, reordered and cleared through the same PUT."""
    campaign_id = await _campaign(client, "Barovia")

    updated = await client.put(
        f"/api/campaigns/{campaign_id}",
        json={
            "name": "Barovia",
            "players": [
                {"player": " Sara ", "character": "Ireena"},
                {"character": "Ismark"},
                {"player": "Ben"},
            ],
        },
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["players"] == [
        {"player": "Sara", "character": "Ireena"},  # trimmed on the way in
        {"player": "", "character": "Ismark"},
        {"player": "Ben", "character": ""},
    ]

    # The list page carries it too, so a picker never has to fetch the row.
    listed = (await client.get("/api/campaigns")).json()
    assert listed[0]["campaign"]["players"][0]["character"] == "Ireena"

    cleared = await client.put(
        f"/api/campaigns/{campaign_id}", json={"name": "Barovia", "players": []}
    )
    assert cleared.json()["players"] == []


async def test_a_player_row_with_no_name_at_all_is_refused(client: AsyncClient) -> None:
    """A blank row is a line the list would carry forever without saying anything."""
    campaign_id = await _campaign(client)
    resp = await client.put(
        f"/api/campaigns/{campaign_id}",
        json={"name": "Curse of Strahd", "players": [{"player": "  ", "character": ""}]},
    )
    assert resp.status_code == 422


async def test_the_cast_reaches_every_generation(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Summary, recap, extraction and "previously on" all carry the same line.

    They are four routes with four prompts, and the one fact none of them can
    read out of a transcript is which of the names in it belong to the people
    in the room.
    """
    seen: list[str] = []

    async def fake_summarize(**kwargs: object) -> str:
        seen.append(str(kwargs.get("cast_line", "")))
        return "text"

    campaign_id = await _campaign(client)
    await client.put(
        f"/api/campaigns/{campaign_id}",
        json={
            "name": "Curse of Strahd",
            "players": [{"player": "Sara", "character": "Ireena"}],
        },
    )
    sid = await _session_with_transcript(client, campaign_id)
    llm = await _llm_provider(client)
    body = {"provider_id": llm, "model": "gpt-5.6-luna"}

    monkeypatch.setattr(sessions_route, "summarize_transcript", fake_summarize)
    await client.post(f"/api/session/{sid}/summarize", json=body)
    await client.post(f"/api/session/{sid}/recap", json=body)
    assert len(seen) == 2
    assert all("Ireena (played by Sara)" in line for line in seen)

    # The extraction goes through loreline.llm.extract_entities, which passes
    # the line to both of its attempts. It is the generation that gains most:
    # it has to sort every character into pc or npc.
    async def fake_extract(**kwargs: object) -> str:
        seen.append(str(kwargs.get("cast_line", "")))
        return json.dumps(_EXTRACTION)

    monkeypatch.setattr("loreline.llm.summarize_transcript", fake_extract)
    await client.post(f"/api/session/{sid}/extract", json=body)
    assert "Ireena (played by Sara)" in seen[-1]

    monkeypatch.setattr(campaigns_route, "summarize_transcript", fake_summarize)
    await client.post(f"/api/campaigns/{campaign_id}/previously-on", json=body)
    assert "Ireena (played by Sara)" in seen[-1]


async def test_no_campaign_and_no_cast_send_no_line(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing changes for a GM who never opens this setting."""
    seen: list[str] = []

    async def fake_summarize(**kwargs: object) -> str:
        seen.append(str(kwargs.get("cast_line", "missing")))
        return "text"

    monkeypatch.setattr(sessions_route, "summarize_transcript", fake_summarize)
    llm = await _llm_provider(client)
    body = {"provider_id": llm, "model": "gpt-5.6-luna"}

    loose = await _session_with_transcript(client)
    await client.post(f"/api/session/{loose}/recap", json=body)

    campaign_id = await _campaign(client)
    in_campaign = await _session_with_transcript(client, campaign_id)
    await client.post(f"/api/session/{in_campaign}/recap", json=body)

    assert seen == ["", ""]


# --- search --------------------------------------------------------------


async def test_search_finds_a_line_and_scopes_to_a_campaign(client: AsyncClient) -> None:
    campaign_id = await _campaign(client)
    inside = await _session_with_transcript(client, campaign_id)
    await _session_with_transcript(client)  # same fake text, no campaign

    everywhere = (await client.get("/api/search", params={"q": "hello"})).json()
    assert everywhere["indexed"] is True
    assert len(everywhere["hits"]) >= 2
    assert "[hello]" in everywhere["hits"][0]["snippet"]

    scoped = (
        await client.get("/api/search", params={"q": "hello", "campaign_id": campaign_id})
    ).json()
    assert {hit["session_id"] for hit in scoped["hits"]} == {inside}

    assert (await client.get("/api/search", params={"q": ""})).json()["hits"] == []


# --- recap ---------------------------------------------------------------


async def test_recap_is_stored_as_a_document(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, object] = {}

    async def fake_summarize(**kwargs: object) -> str:
        seen.update(kwargs)
        return "The party rode into Barovia."

    monkeypatch.setattr(sessions_route, "summarize_transcript", fake_summarize)
    campaign_id = await _campaign(client)
    sid = await _session_with_transcript(client, campaign_id)
    llm = await _llm_provider(client)

    resp = await client.post(
        f"/api/session/{sid}/recap", json={"provider_id": llm, "model": "gpt-5.6-luna"}
    )
    assert resp.status_code == 200
    assert resp.json()["kind"] == "recap"
    assert resp.json()["body"] == "The party rode into Barovia."
    assert resp.json()["version"] == "original"

    # The built-in recap prompt, which is not the summary prompt.
    assert "recap" in str(seen["system_prompt"]).lower()

    detail = (await client.get(f"/api/session/{sid}")).json()
    assert [d["kind"] for d in detail["documents"]] == ["recap"]
    # The summary column is untouched: a recap is a different text for a
    # different reader, not a replacement for it.
    assert detail["session"]["summary"] is None

    listed = (
        await client.get(f"/api/campaigns/{campaign_id}/documents", params={"kind": "recap"})
    ).json()
    assert [d["session_id"] for d in listed] == [sid]


async def test_the_campaign_recap_prompt_wins_over_the_default(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Campaign, else the stored default, else the built-in text."""
    seen: list[object] = []

    async def fake_summarize(**kwargs: object) -> str:
        seen.append(kwargs["system_prompt"])
        return "ok"

    monkeypatch.setattr(sessions_route, "summarize_transcript", fake_summarize)
    campaign_id = await _campaign(client)
    sid = await _session_with_transcript(client, campaign_id)
    llm = await _llm_provider(client)
    body = {"provider_id": llm, "model": "gpt-5.6-luna"}

    await client.put("/api/system/defaults", json={"recap_prompt": "Nur drei Sätze."})
    await client.post(f"/api/session/{sid}/recap", json=body)
    assert seen[-1] == "Nur drei Sätze."

    await client.put(
        f"/api/campaigns/{campaign_id}",
        json={"name": "Curse of Strahd", "recap_prompt": "Im Stil eines Barden."},
    )
    await client.post(f"/api/session/{sid}/recap", json=body)
    assert seen[-1] == "Im Stil eines Barden."


async def test_recap_surfaces_an_upstream_failure_as_502(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_summarize(**_kwargs: object) -> str:
        raise LLMError("The model `gpt-5.6-terra` does not exist.")

    monkeypatch.setattr(sessions_route, "summarize_transcript", fake_summarize)
    sid = await _session_with_transcript(client)
    llm = await _llm_provider(client)

    resp = await client.post(
        f"/api/session/{sid}/recap", json={"provider_id": llm, "model": "gpt-5.6-terra"}
    )
    assert resp.status_code == 502
    assert (await client.get(f"/api/session/{sid}")).json()["documents"] == []


async def test_recap_rejects_a_provider_that_cannot_chat(client: AsyncClient) -> None:
    sid = await _session_with_transcript(client)
    stt = (await client.post("/api/providers", json={"name": "STT2", "kind": "deepgram"})).json()[
        "id"
    ]
    resp = await client.post(
        f"/api/session/{sid}/recap", json={"provider_id": stt, "model": "nova-3"}
    )
    assert resp.status_code == 400
    assert (
        await client.post("/api/session/nope/recap", json={"provider_id": stt, "model": "nova-3"})
    ).status_code == 404


# --- extraction ----------------------------------------------------------

_EXTRACTION = {
    "characters": [{"name": "Ireena", "kind": "PC", "notes": "the burgomaster's daughter"}],
    "places": [{"name": "Vallaki", "notes": "walled town"}],
    "items": [],
    "factions": [{"name": "The Keepers", "notes": ""}],
    "quests": [{"title": "Find the Tome", "status": "open", "notes": ""}],
    "decisions": ["Left Ismark behind"],
}


async def test_extraction_parses_a_fenced_answer(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A code fence is the normal answer to "JSON only", not a failure."""

    async def fake_summarize(**_kwargs: object) -> str:
        return f"```json\n{json.dumps(_EXTRACTION)}\n```"

    monkeypatch.setattr("loreline.llm.summarize_transcript", fake_summarize)
    sid = await _session_with_transcript(client)
    llm = await _llm_provider(client)

    resp = await client.post(
        f"/api/session/{sid}/extract", json={"provider_id": llm, "model": "gpt-5.6-luna"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["characters"] == [
        {"name": "Ireena", "kind": "pc", "notes": "the burgomaster's daughter"}
    ]
    assert body["decisions"] == ["Left Ismark behind"]

    stored = (await client.get(f"/api/session/{sid}")).json()["documents"]
    assert [d["kind"] for d in stored] == ["extraction"]
    assert json.loads(stored[0]["body"])["places"][0]["name"] == "Vallaki"


async def test_extraction_retries_once_on_unusable_json(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The first answer's mistake is quoted back, and the second one lands."""
    calls: list[str] = []

    async def fake_summarize(**kwargs: object) -> str:
        calls.append(str(kwargs["instruction"]))
        if len(calls) == 1:
            return "Sure! Here are the names I found: Ireena, Vallaki."
        return json.dumps(_EXTRACTION)

    monkeypatch.setattr("loreline.llm.summarize_transcript", fake_summarize)
    sid = await _session_with_transcript(client)
    llm = await _llm_provider(client)

    resp = await client.post(
        f"/api/session/{sid}/extract", json={"provider_id": llm, "model": "gpt-5.6-luna"}
    )
    assert resp.status_code == 200
    assert len(calls) == 2
    assert "no JSON object" in calls[1]


async def test_extraction_gives_up_after_the_retry(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_summarize(**_kwargs: object) -> str:
        return "no json here either"

    monkeypatch.setattr("loreline.llm.summarize_transcript", fake_summarize)
    sid = await _session_with_transcript(client)
    llm = await _llm_provider(client)

    resp = await client.post(
        f"/api/session/{sid}/extract", json={"provider_id": llm, "model": "gpt-5.6-luna"}
    )
    assert resp.status_code == 502
    assert (await client.get(f"/api/session/{sid}")).json()["documents"] == []


async def test_campaign_entities_merge_by_name(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One NPC across two sessions is one entry that names both."""
    second = {
        "characters": [{"name": "ireena", "kind": "pc", "notes": "now travelling with the party"}],
        "places": [{"name": "Krezk", "notes": ""}],
        "items": [],
        "factions": [],
        "quests": [{"title": "Find the Tome", "status": "done", "notes": ""}],
        "decisions": [],
    }
    answers = [json.dumps(_EXTRACTION), json.dumps(second)]

    async def fake_summarize(**_kwargs: object) -> str:
        return answers.pop(0)

    monkeypatch.setattr("loreline.llm.summarize_transcript", fake_summarize)
    campaign_id = await _campaign(client)
    first_session = await _session_with_transcript(client, campaign_id)
    second_session = await _session_with_transcript(client, campaign_id)
    llm = await _llm_provider(client)
    for sid in (first_session, second_session):
        assert (
            await client.post(
                f"/api/session/{sid}/extract", json={"provider_id": llm, "model": "m"}
            )
        ).status_code == 200

    entities = (await client.get(f"/api/campaigns/{campaign_id}/entities")).json()
    assert len(entities["characters"]) == 1
    ireena = entities["characters"][0]
    assert ireena["name"] == "Ireena"  # the first spelling seen
    assert ireena["notes"] == "now travelling with the party"  # the latest session's
    assert set(ireena["session_ids"]) == {first_session, second_session}
    assert [p["name"] for p in entities["places"]] == ["Vallaki", "Krezk"]
    # A quest closed in a later session reads as closed.
    assert entities["quests"][0]["kind"] == "done"


# --- previously on -------------------------------------------------------


async def test_previously_on_reads_recaps_then_summaries(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, object] = {}

    async def fake_recap(**_kwargs: object) -> str:
        return "The party rode into Barovia."

    async def fake_previously(**kwargs: object) -> str:
        seen.update(kwargs)
        return "Last time in Barovia..."

    campaign_id = await _campaign(client)
    with_recap = await _session_with_transcript(client, campaign_id)
    with_summary = await _session_with_transcript(client, campaign_id)
    await _session_with_transcript(client, campaign_id)  # neither: skipped
    llm = await _llm_provider(client)

    monkeypatch.setattr(sessions_route, "summarize_transcript", fake_recap)
    await client.post(f"/api/session/{with_recap}/recap", json={"provider_id": llm, "model": "m"})

    async def fake_summary(**_kwargs: object) -> str:
        return "A summary of the second evening."

    monkeypatch.setattr(sessions_route, "summarize_transcript", fake_summary)
    await client.post(
        f"/api/session/{with_summary}/summarize", json={"provider_id": llm, "model": "m"}
    )

    monkeypatch.setattr(campaigns_route, "summarize_transcript", fake_previously)
    resp = await client.post(
        f"/api/campaigns/{campaign_id}/previously-on", json={"provider_id": llm, "model": "m"}
    )
    assert resp.status_code == 200
    assert resp.json()["body"] == "Last time in Barovia..."

    fed = str(seen["transcript"])
    assert "The party rode into Barovia." in fed
    assert "A summary of the second evening." in fed

    stored = (await client.get(f"/api/campaigns/{campaign_id}/previously-on")).json()
    assert stored["body"] == "Last time in Barovia..."


async def test_previously_on_needs_something_to_read(client: AsyncClient) -> None:
    campaign_id = await _campaign(client)
    await _session_with_transcript(client, campaign_id)
    llm = await _llm_provider(client)

    resp = await client.post(
        f"/api/campaigns/{campaign_id}/previously-on", json={"provider_id": llm, "model": "m"}
    )
    assert resp.status_code == 409
    assert "recap" in resp.json()["detail"]
    assert (await client.get(f"/api/campaigns/{campaign_id}/previously-on")).json() is None
