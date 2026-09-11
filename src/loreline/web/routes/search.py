"""Search across stored transcripts."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field

from loreline.models import SearchHit
from loreline.web.auth import require_auth
from loreline.web.deps import get_state

router = APIRouter(prefix="/api/search", tags=["search"], dependencies=[Depends(require_auth)])

_MAX_HITS = 200


class SearchResults(BaseModel):
    """What a search found, and whether it was a real search.

    ``indexed`` is false on a SQLite build with no FTS5 in it, where the query
    ran as a ``LIKE`` scan: the hits are real, the ranking is not (they come
    back newest first, because there is nothing to rank by). The page says so
    in a line under the box rather than silently presenting an unranked list as
    a ranked one - on a library of one campaign the difference is invisible,
    and on ten it is the difference between the answer and the tenth answer.
    """

    hits: list[SearchHit] = Field(default_factory=list[SearchHit])
    indexed: bool = True


@router.get("")
async def search_transcripts(
    request: Request,
    q: str = "",
    campaign_id: str | None = None,
    limit: int = Query(default=50, ge=1, le=_MAX_HITS),
) -> SearchResults:
    """Find transcript lines matching ``q``, optionally within one campaign.

    An empty query is an empty result rather than an error: the box is emptied
    by a backspace, and a page that answered that with a red banner would blame
    the reader for clearing it.
    """
    state = get_state(request)
    hits = await state.search.search(q, campaign_id, limit)
    return SearchResults(hits=hits, indexed=state.db.fts5)
