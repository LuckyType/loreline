"""Shared plumbing for the routes that ask an LLM for a text.

Five routes now hand a text to a chat model: the summary, the recap, the
extraction, the campaign's "previously on" and the video scene. They
differ in the prompt and in where the answer is stored, and they agreed on
everything else by copying it - which provider row is usable, where its key
comes from, which prompt wins when a campaign, the stored defaults and the
built-in text all have one, and who is actually at this table. Those answers
live here, once.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, NamedTuple

from fastapi.exceptions import HTTPException
from starlette.status import HTTP_400_BAD_REQUEST, HTTP_404_NOT_FOUND

from loreline.capabilities import supports
from loreline.llm import DEFAULT_RECAP_PROMPT, DEFAULT_SCENE_PROMPT
from loreline.models import CampaignPlayer, Interaction, ProviderConfig
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


async def scene_prompt(state: AppState, style: str) -> str:
    """The scene instructions: the stored default, else the built-in text, plus a style.

    Two levels rather than the recap's three, deliberately: there is no
    per-campaign override yet, and when there is one it slots in above the
    stored default exactly as :func:`recap_prompt` does it.

    ``style`` is the look the scene should be written in, and it arrives from
    the client rather than being read here. That is what keeps the promise
    VideoGenerateRequest makes: the video model is sent whatever the GM left in
    the prompt box and nothing else, so a style has to shape the text while it
    is still in the box, where it can be read and edited, instead of being
    appended to the request behind them.
    """
    defaults = await load_action_defaults(state)
    instructions = defaults.scene_prompt.strip() or DEFAULT_SCENE_PROMPT
    look = style.strip()
    if not look:
        return instructions
    return (
        f"{instructions}\n\nRender the scene in this visual style, and describe "
        f"the style as part of the scene: {look}"
    )


def describe_cast(players: Sequence[CampaignPlayer]) -> str:
    """One line of instruction naming a table's player characters, or nothing.

    The four generations all read a transcript where nothing says which of the
    twenty names in it belong to the five people in the room. The extraction
    is where that costs the most: it has to sort every character into ``pc`` or
    ``npc``, and with nothing to go on it guesses from how often a name comes
    up, which is how a talkative innkeeper becomes a player character. A summary
    and a recap want the same fact for a softer reason, that the party should
    read as the party and be spelled the way the GM spells them.

    One line, not a paragraph: it is prepended to instructions that already say
    what to write, and a second set of instructions competing with the first is
    how a recap turns into a cast list.

    A row with only a player name is printed as that name alone. That is a
    person at the table whose character has not been written down, and the name
    still gets said out loud, so it is still worth spelling right.
    """
    entries: list[str] = []
    for player in players:
        if player.character and player.player:
            entries.append(f"{player.character} (played by {player.player})")
        else:
            entries.append(player.character or player.player)
    if not entries:
        return ""
    return (
        "The player characters in this campaign, and the players behind them: "
        + ", ".join(entries)
        + ". Treat these as the player characters and anybody else named as an NPC, "
        "and use exactly these spellings for their names."
    )


async def cast_note(state: AppState, campaign_id: str | None) -> str:
    """The cast line for this session's campaign, or "" for no line at all.

    The same shape as :func:`recap_prompt` and for the same reason: four routes
    want one fact about the campaign a session is in, and the one that has to
    be right is the empty case. A session in no campaign, a campaign nobody has
    filled a cast in for, and a ``campaign_id`` left over from before campaigns
    were rows all answer "", and an empty note adds nothing to any prompt - so
    a GM who never opens this setting gets exactly the generations they got
    before it existed.
    """
    if not campaign_id:
        return ""
    campaign = await state.campaigns.get(campaign_id)
    if campaign is None:
        return ""
    return describe_cast(campaign.players)
