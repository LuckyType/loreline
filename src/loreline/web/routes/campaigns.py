"""Campaign routes: the campaigns themselves, and what accumulates in them.

A campaign is a row here rather than the free string ``sessions.campaign_id``
has always held (see docs/adr/0009). What hangs off it is the point of it: the
sessions in the order they were played, the glossary they share, the names the
extractions found across all of them, and the "previously on" that opens the
next one.
"""

from __future__ import annotations

import time
import uuid
from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, Request
from fastapi.exceptions import HTTPException
from starlette.status import (
    HTTP_404_NOT_FOUND,
    HTTP_409_CONFLICT,
    HTTP_502_BAD_GATEWAY,
)

from loreline.llm import DEFAULT_PREVIOUSLY_ON_PROMPT, LLMError, summarize_transcript
from loreline.models import (
    DOCUMENT_EXTRACTION,
    DOCUMENT_PREVIOUSLY_ON,
    DOCUMENT_RECAP,
    Campaign,
    CampaignDocument,
    CampaignEntities,
    CampaignSummary,
    Glossary,
    MergedEntity,
    Session,
    SessionDocument,
    SessionExtraction,
)
from loreline.web.auth import require_auth
from loreline.web.deps import get_state
from loreline.web.generation import llm_target
from loreline.web.schemas import (
    CampaignWrite,
    GlossaryAdd,
    OkResponse,
    PreviouslyOnRequest,
)

if TYPE_CHECKING:
    from loreline.web.app import AppState

router = APIRouter(
    prefix="/api/campaigns", tags=["campaigns"], dependencies=[Depends(require_auth)]
)


async def _require_campaign(state: AppState, campaign_id: str) -> Campaign:
    campaign = await state.campaigns.get(campaign_id)
    if campaign is None:
        raise HTTPException(status_code=HTTP_404_NOT_FOUND, detail="campaign not found")
    return campaign


@router.get("")
async def list_campaigns(request: Request) -> list[CampaignSummary]:
    """Every campaign, by name, with its session count and last session."""
    return await get_state(request).campaigns.list()


@router.post("", status_code=201)
async def create_campaign(request: Request, body: CampaignWrite) -> Campaign:
    """Create a campaign.

    A name already in use is a 409 rather than a second row: the picker shows
    names, so two campaigns called "Curse of Strahd" are indistinguishable
    everywhere a GM would have to choose between them, and the mistake that
    produces them is pressing New twice.
    """
    state = get_state(request)
    if await state.campaigns.by_name(body.name) is not None:
        raise HTTPException(
            status_code=HTTP_409_CONFLICT, detail=f"a campaign named {body.name!r} already exists"
        )
    campaign = Campaign(
        id=uuid.uuid4().hex,
        name=body.name,
        created_at=time.time(),
        notes=body.notes,
        recap_prompt=body.recap_prompt,
    )
    await state.campaigns.create(campaign)
    return campaign


@router.get("/{campaign_id}")
async def get_campaign(request: Request, campaign_id: str) -> Campaign:
    """One campaign."""
    return await _require_campaign(get_state(request), campaign_id)


@router.put("/{campaign_id}")
async def update_campaign(request: Request, campaign_id: str, body: CampaignWrite) -> Campaign:
    """Rename a campaign, or change its notes or its recap prompt."""
    state = get_state(request)
    campaign = await _require_campaign(state, campaign_id)
    clash = await state.campaigns.by_name(body.name)
    if clash is not None and clash.id != campaign_id:
        raise HTTPException(
            status_code=HTTP_409_CONFLICT, detail=f"a campaign named {body.name!r} already exists"
        )
    updated = campaign.model_copy(
        update={"name": body.name, "notes": body.notes, "recap_prompt": body.recap_prompt}
    )
    await state.campaigns.update(updated)
    return updated


@router.delete("/{campaign_id}")
async def delete_campaign(request: Request, campaign_id: str) -> OkResponse:
    """Delete a campaign. Its sessions are kept, unassigned.

    Said in the confirm dialog too, because it is the one thing about this
    button anybody could be afraid of: a campaign is a label on recordings that
    already exist, and deleting the label must not be able to delete four hours
    of audio nobody can capture again.
    """
    state = get_state(request)
    await _require_campaign(state, campaign_id)
    await state.campaigns.delete(campaign_id)
    return OkResponse()


@router.get("/{campaign_id}/sessions")
async def campaign_sessions(request: Request, campaign_id: str) -> list[Session]:
    """The campaign's sessions, oldest first - the order they were played in.

    The opposite of the History page's order, deliberately: that page answers
    "what did I record last", and a campaign answers "what happened, and then
    what happened", which is a story and reads forwards.
    """
    state = get_state(request)
    await _require_campaign(state, campaign_id)
    sessions = [s for s in await state.sessions.list() if s.campaign_id == campaign_id]
    sessions.sort(key=lambda s: s.started_at)
    return sessions


@router.get("/{campaign_id}/documents")
async def campaign_session_documents(
    request: Request, campaign_id: str, kind: str | None = None
) -> list[SessionDocument]:
    """Every generated text of the campaign's sessions, oldest session first."""
    state = get_state(request)
    await _require_campaign(state, campaign_id)
    return await state.documents.for_campaign(campaign_id, kind)


@router.get("/{campaign_id}/entities")
async def campaign_entities(request: Request, campaign_id: str) -> CampaignEntities:
    """Every session's extraction, merged by name.

    Merged on a normalised name (trimmed and case-folded) rather than on the
    exact string, because a transcript spells a name three ways across nine
    sessions and three entries for one NPC is exactly the mess this page exists
    to replace. The spelling kept is the first one seen, the notes are the
    latest session's, and the session list says where the name came up - which
    is what turns "who was Vallaki's burgomaster again" into a link.
    """
    state = get_state(request)
    await _require_campaign(state, campaign_id)
    documents = await state.documents.for_campaign(campaign_id, DOCUMENT_EXTRACTION)
    return _merge_extractions(documents)


@router.post("/{campaign_id}/glossary/add")
async def add_to_campaign_glossary(
    request: Request, campaign_id: str, body: GlossaryAdd
) -> Glossary:
    """Append names to the campaign's glossary, trimmed and deduplicated.

    Append rather than replace: this is reached from the extracted-names list,
    where the gesture is "and these too", and a request that could silently
    drop the terms a GM typed by hand would make that list dangerous to use.
    Case-insensitive on the duplicate check, because "Strahd" and "strahd" bias
    a recognizer identically and two of them only spend the glossary ceiling.
    """
    state = get_state(request)
    await _require_campaign(state, campaign_id)
    glossary = await state.glossaries.get(campaign_id)
    terms = list(glossary.terms)
    known = {term.casefold() for term in terms}
    for raw in body.terms:
        term = raw.strip()
        if term and term.casefold() not in known:
            terms.append(term)
            known.add(term.casefold())
    updated = Glossary(campaign_id=campaign_id, terms=terms)
    await state.glossaries.put(updated)
    return updated


@router.get("/{campaign_id}/previously-on")
async def get_previously_on(request: Request, campaign_id: str) -> CampaignDocument | None:
    """The stored "previously on", or null when none has been written."""
    state = get_state(request)
    await _require_campaign(state, campaign_id)
    return await state.documents.get_campaign_document(campaign_id, DOCUMENT_PREVIOUSLY_ON)


@router.post("/{campaign_id}/previously-on")
async def write_previously_on(
    request: Request, campaign_id: str, body: PreviouslyOnRequest
) -> CampaignDocument:
    """Write the campaign's "previously on" from its last few sessions.

    Reads recaps, falling back per session to the stored summary, because a
    campaign is rarely all one or all the other: the last three sessions have
    recaps and the one before them was summarized months ago, and refusing to
    read that one would silently drop a session out of the story. A session
    with neither is skipped rather than represented by its raw transcript -
    four hours of dialogue would swamp the two paragraphs either side of it.
    """
    state = get_state(request)
    campaign = await _require_campaign(state, campaign_id)
    provider, api_key = await llm_target(state, body.provider_id)

    sessions = await campaign_sessions(request, campaign_id)
    recaps = {
        document.session_id: document.body
        for document in await state.documents.for_campaign(campaign_id, DOCUMENT_RECAP)
    }
    parts: list[str] = []
    for session in sessions[-body.sessions :]:
        text = recaps.get(session.id) or session.summary
        if text:
            when = time.strftime("%Y-%m-%d", time.localtime(session.started_at))
            parts.append(f"Session of {when}:\n{text}")
    if not parts:
        raise HTTPException(
            status_code=HTTP_409_CONFLICT,
            detail="no session in this campaign has a recap or a summary yet",
        )

    try:
        text = await summarize_transcript(
            config=provider,
            api_key=api_key,
            model=body.model,
            transcript="\n\n".join(parts),
            system_prompt=DEFAULT_PREVIOUSLY_ON_PROMPT,
            instruction=(
                f'Write the "previously on" for the next session of '
                f"{campaign.name}, from these recaps:"
            ),
            reasoning_effort=body.reasoning_effort or None,
        )
    except LLMError as exc:
        raise HTTPException(status_code=HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc

    document = CampaignDocument(
        campaign_id=campaign_id,
        kind=DOCUMENT_PREVIOUSLY_ON,
        body=text,
        provider_id=provider.id,
        model=body.model,
        created_at=time.time(),
    )
    await state.documents.put_campaign_document(document)
    return document


def _merge_extractions(documents: list[SessionDocument]) -> CampaignEntities:
    """Fold each session's extraction into one list per group.

    A body that no longer parses is skipped rather than failing the page: it is
    one session's worth of names, it was written by a model on a day the schema
    may have been different, and the other eight sessions are still worth
    reading.
    """
    groups: dict[str, dict[str, MergedEntity]] = {
        "characters": {},
        "places": {},
        "items": {},
        "factions": {},
        "quests": {},
        "decisions": {},
    }

    def add(group: str, session_id: str, name: str, kind: str, notes: str) -> None:
        cleaned = name.strip()
        if not cleaned:
            return
        key = cleaned.casefold()
        existing = groups[group].get(key)
        if existing is None:
            groups[group][key] = MergedEntity(
                name=cleaned, kind=kind, notes=notes, session_ids=[session_id]
            )
            return
        if session_id not in existing.session_ids:
            existing.session_ids.append(session_id)
        # The newest session wins the note and the kind: a quest that was
        # "open" three sessions ago and "done" last week is done, and the
        # documents arrive oldest first.
        if notes:
            existing.notes = notes
        if kind:
            existing.kind = kind

    for document in documents:
        try:
            extraction = SessionExtraction.model_validate_json(document.body)
        except ValueError:
            continue
        session_id = document.session_id
        for character in extraction.characters:
            add("characters", session_id, character.name, character.kind, character.notes)
        for group, entries in (
            ("places", extraction.places),
            ("items", extraction.items),
            ("factions", extraction.factions),
        ):
            for entry in entries:
                add(group, session_id, entry.name, "", entry.notes)
        for quest in extraction.quests:
            add("quests", session_id, quest.title, quest.status, quest.notes)
        for decision in extraction.decisions:
            add("decisions", session_id, decision, "", "")

    return CampaignEntities(
        characters=list(groups["characters"].values()),
        places=list(groups["places"].values()),
        items=list(groups["items"].values()),
        factions=list(groups["factions"].values()),
        quests=list(groups["quests"].values()),
        decisions=list(groups["decisions"].values()),
    )
