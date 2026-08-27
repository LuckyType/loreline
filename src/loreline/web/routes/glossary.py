"""Glossary routes: per-campaign custom vocabulary."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from loreline.models import DEFAULT_GLOSSARY_CAMPAIGN, Glossary
from loreline.web.auth import require_auth
from loreline.web.deps import get_state
from loreline.web.schemas import GlossaryWrite

router = APIRouter(prefix="/api/glossary", tags=["glossary"], dependencies=[Depends(require_auth)])


@router.get("")
async def get_default_glossary(request: Request) -> Glossary:
    """Return the default word list applied to every session."""
    return await get_state(request).glossaries.get(DEFAULT_GLOSSARY_CAMPAIGN)


@router.put("")
async def put_default_glossary(request: Request, body: GlossaryWrite) -> Glossary:
    """Replace the default word list (applied to every session)."""
    glossary = Glossary(campaign_id=DEFAULT_GLOSSARY_CAMPAIGN, terms=body.terms)
    await get_state(request).glossaries.put(glossary)
    return glossary


@router.get("/{campaign_id}")
async def get_glossary(request: Request, campaign_id: str) -> Glossary:
    """Return the glossary for a campaign (empty if none)."""
    return await get_state(request).glossaries.get(campaign_id)


@router.put("/{campaign_id}")
async def put_glossary(request: Request, campaign_id: str, body: GlossaryWrite) -> Glossary:
    """Replace a campaign's glossary terms."""
    glossary = Glossary(campaign_id=campaign_id, terms=body.terms)
    await get_state(request).glossaries.put(glossary)
    return glossary
