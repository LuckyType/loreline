"""List the models a provider offers, for the on-demand model pickers.

A picker row is a projection of the one vendor catalogue reader,
:mod:`loreline.catalog`: probe the kind's ``catalog`` surface, then narrow and
annotate what came back. Best-effort by contract: an unusable probe (no
catalogue, no key, vendor down, shape moved) falls back to the curated list
in capabilities.yaml, or to an empty list for a kind that curates nothing.

Not every catalogue the reader parses is offered live. A ``catalog`` surface
marked ``picker: false`` in the yaml is read to check the curated list, never
to replace it in a picker: Deepgram's lists a task-tuned variant for every
occasion where the yaml deliberately offers four, and Gemini's is one list for
every interaction with nothing in it saying which model transcribes and which
chats. That gate is data, beside the surface it gates, not a kind set here.

This module decides *where* a list comes from, never *which models are in it*.
That second question has exactly one answer, capabilities.yaml, read here through
:func:`loreline.capabilities.curated_models`. It used to have two: a ``_CURATED``
table lived here as well, and the pair had already drifted - it offered six
task-tuned Deepgram variants the yaml deliberately does not list, and it kept
gemini-3.5-transcribe-live out of every picker for a while after the yaml unhid
it, because nobody remembered the second gate.
"""

from __future__ import annotations

from collections.abc import Iterable

from loreline.capabilities import (
    catalog_for,
    curated_models,
    filter_models,
    is_realtime_model,
    supports_inline_diarization,
)
from loreline.catalog import ClientFactory, VendorModel, VendorPrice, probe
from loreline.models import Interaction, ModelInfo, ModelPrice, ProviderKind

# A picker is waiting on this: shorter than the CI check's budget, longer than
# the boot-time courtesy's.
PICKER_TIMEOUT_S = 15.0


async def list_models(
    *,
    kind: ProviderKind,
    base_url: str | None,
    api_key: str | None,
    interaction: Interaction = Interaction.TRANSCRIBE,
    strict_filtering: bool = True,
    client_factory: ClientFactory | None = None,
) -> list[ModelInfo]:
    """Available models for a provider connection (live where possible).

    Scoped to ``interaction``: a transcription picker must never offer a chat
    or image model, which is exactly what an unscoped OpenAI ``/models`` dump
    used to allow. See :mod:`loreline.capabilities` for how that narrowing is
    decided per kind. ``strict_filtering=False`` turns off the guessed part of
    that narrowing (the name markers) while keeping the parts sourced from the
    provider's own metadata.

    Entries carry price and context length when the provider publishes them
    (OpenRouter does; plain OpenAI ``/models`` and the curated lists do not),
    so everything past ``id`` is optional and a caller can always just read ids.
    """
    # Where the vendor publishes its list for this interaction: OpenRouter
    # splits its catalogue three ways, OpenAI serves one list for everything,
    # the self-hosted server's list sits beside whatever base the row names.
    # None means nothing to fetch; a surface the pickers may not read live is
    # the curated list by declaration.
    catalogue = catalog_for(kind, interaction, base_url=base_url)
    if catalogue is not None and catalogue.surface.picker:
        answer = await probe(
            kind,
            interaction,
            api_key=api_key,
            base_url=base_url,
            client_factory=client_factory,
            request_timeout=PICKER_TIMEOUT_S,
        )
        if answer.usable:
            narrowed = filter_models(
                _rows(answer.models), kind=kind, interaction=interaction, strict=strict_filtering
            )
            return _annotate(narrowed, kind=kind, interaction=interaction)
    # The curated fallback, scoped to this interaction: a Gemini summarize
    # picker must not offer a transcription model to summarize with.
    curated = [ModelInfo(id=model_id) for model_id in curated_models(kind, interaction)]
    return _annotate(curated, kind=kind, interaction=interaction)


def _rows(models: Iterable[VendorModel]) -> list[ModelInfo]:
    """Picker rows from vendor rows: sorted by id, first of any duplicate kept."""
    by_id: dict[str, ModelInfo] = {}
    for model in models:
        by_id.setdefault(model.id, _row(model))
    return sorted(by_id.values(), key=lambda m: m.id)


def _row(model: VendorModel) -> ModelInfo:
    return ModelInfo(
        id=model.id,
        context_length=model.context_length,
        supports_reasoning=bool(model.accepts_reasoning_effort),
        pricing=_price(model.pricing) if model.pricing else None,
        price_tiers=[_price(tier) for tier in model.price_tiers],
    )


def _price(price: VendorPrice) -> ModelPrice:
    return ModelPrice(
        prompt=price.prompt,
        completion=price.completion,
        min_prompt_tokens=price.min_prompt_tokens,
    )


def _annotate(
    models: list[ModelInfo], *, kind: ProviderKind, interaction: Interaction
) -> list[ModelInfo]:
    """Stamp the capability flags the pickers read onto transcription entries.

    A live ``/models`` row says nothing about transport or diarization, so both
    come from the capability tables, per model rather than per kind: OpenAI's
    list mixes realtime and batch models, and OpenRouter's carries one model
    (grok-stt-1.0) whose inline diarization would otherwise go unadvertised.
    Other interactions have no such flags to stamp.
    """
    if interaction is not Interaction.TRANSCRIBE:
        return models
    return [
        model.model_copy(
            update={
                "realtime": is_realtime_model(kind, model.id),
                "inline_diarization": supports_inline_diarization(kind, model.id),
            }
        )
        for model in models
    ]
