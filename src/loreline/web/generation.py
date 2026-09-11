"""Shared plumbing for the routes that ask an LLM for a text.

Four routes now hand a transcript (or a stack of recaps) to a chat model: the
summary, the recap, the extraction and the campaign's "previously on". They
differ in the prompt and in where the answer is stored, and they agreed on
everything else by copying it - which provider row is usable, where its key
comes from, which prompt wins when a campaign, the stored defaults and the
built-in text all have one. Those three answers live here, once.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, NamedTuple

from fastapi.exceptions import HTTPException
from starlette.status import HTTP_400_BAD_REQUEST, HTTP_404_NOT_FOUND

from loreline.capabilities import supports
from loreline.llm import DEFAULT_RECAP_PROMPT
from loreline.models import Interaction, ProviderConfig
from loreline.web.deps import load_action_defaults

if TYPE_CHECKING:
    from loreline.web.app import AppState


class LLMTarget(NamedTuple):
    """A provider row that can run a chat completion, and the key to run it with."""

    provider: ProviderConfig
    api_key: str | None


async def llm_target(state: AppState, provider_id: str) -> LLMTarget:
    """Resolve a provider id to a usable LLM row, or raise the HTTP error.

    Asked of the yaml per request rather than off a stored capability flag, so
    a ``capabilities.reload()`` is honoured and a row that has just gained (or
    lost) chat is graded as it is now.
    """
    provider = await state.providers.get(provider_id)
    if provider is None:
        raise HTTPException(status_code=HTTP_404_NOT_FOUND, detail="provider not found")
    if not supports(provider.kind, Interaction.SUMMARIZE):
        raise HTTPException(
            status_code=HTTP_400_BAD_REQUEST, detail="provider is not an LLM provider"
        )
    return LLMTarget(provider, state.secrets.get(provider.auth_ref) if provider.auth_ref else None)


async def recap_prompt(state: AppState, campaign_id: str | None) -> str:
    """The recap instructions for this session: campaign, else default, else built in.

    Three levels, narrowest first, and each one blank means "the level above
    decides". That is what makes a campaign whose table plays in German, or one
    that wants its recaps in character, a single field on the campaign rather
    than a prompt somebody has to paste into the dialog every time - while a
    GM who has never opened either setting keeps getting the built-in text,
    including the improvements it gets later.
    """
    if campaign_id:
        campaign = await state.campaigns.get(campaign_id)
        if campaign is not None and campaign.recap_prompt.strip():
            return campaign.recap_prompt.strip()
    defaults = await load_action_defaults(state)
    return defaults.recap_prompt.strip() or DEFAULT_RECAP_PROMPT
