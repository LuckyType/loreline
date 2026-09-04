"""What each vendor's catalogue lists right now, read once and fail soft.

The one vendor catalogue reader. It answers one question, "what does this
vendor list for this interaction right now", and three readers project the
answer: the model pickers (:mod:`loreline.stt.catalog`), the video generate
dialog (:mod:`loreline.video.client`) and the staleness feature
(:mod:`loreline.staleness`), which compares it against the curated file. Each
used to fetch and parse for itself; a vendor payload change now breaks one
parser here and one test file, not three.

FAIL SOFT IS THE POINT. Nothing in here raises. A vendor that is down, rate
limiting, missing a key, or answering with a shape nobody has seen yields a
:class:`CatalogProbe` whose status says so. The pickers read that as "fall
back to the curated list"; the staleness check reads it as "could not check",
never as "your models are gone". Two rules encode that:

* An empty catalogue is UNREADABLE, never OK-with-zero-models. No real vendor
  serves an empty list, so parsing one means the response shape moved under us,
  and treating it as truth would report every curated model as retired.
* A paginated answer we did not follow to the end is marked ``partial``:
  present models are still trustworthy, absent ones prove nothing.

Where each catalogue lives, how the key is spelled for it, whether it answers
without one, and which environment variable CI would find a key in, are all
read from capabilities.yaml (the kind's ``catalog`` surface and ``key_env``),
never restated here. What stays in this module is how to read the body,
because the bodies differ enough that one generic parser would be a lie:
OpenRouter's chat catalogue publishes prices, reasoning and parameter
metadata, its video catalogue is a different schema entirely, OpenAI's carries
nothing but an id and a shutdown date (and a self-hosted server claiming
compatibility may serve a bare list of id strings), Deepgram splits stt from
tts, and Gemini prefixes every id with ``models/``.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import cast

import httpx

from loreline import capabilities
from loreline.capability_config import ProviderSpec, Surface
from loreline.logging import get_logger
from loreline.models import Interaction, ProviderKind

log = get_logger(__name__)

# A catalogue is a small JSON document; the only reason this is not shorter is
# that OpenRouter's chat list is ~700 kB of model metadata.
DEFAULT_TIMEOUT_S = 20.0

ClientFactory = Callable[[], httpx.AsyncClient]


class CatalogStatus(StrEnum):
    """Why a probe did or did not produce a usable answer.

    Only ``OK`` licenses a comparison. Every other value is a "not checked",
    which is a very different statement from "nothing found" and is rendered as
    such in the report.
    """

    OK = "ok"
    # The vendor publishes no machine-readable catalogue (AssemblyAI), or the
    # endpoint is on a server only the operator can name (the self-hosted kind).
    NO_CATALOGUE = "no_catalogue"
    # The endpoint exists but needs a key nobody supplied. CI runs without
    # secrets by design, so this is the normal case for most vendors.
    NO_CREDENTIALS = "no_credentials"
    # Down, slow, rate limiting, refusing the key: anything that stopped a body
    # from arriving.
    UNREACHABLE = "unreachable"
    # A body arrived and made no sense. Kept apart from UNREACHABLE because it
    # is the one status that means "our parser is stale", not "the vendor is".
    UNREADABLE = "unreadable"


@dataclass(frozen=True, slots=True)
class VendorReasoning:
    """The vendor's own reasoning metadata for one model.

    Absent (the enclosing field is None) means the vendor says the model does
    no reasoning at all. Present with an empty ``efforts`` is a different and
    equally deliberate statement: it reasons, but exposes no discrete effort
    levels, which is exactly what ``reasoning: {supported: true, efforts: []}``
    records in the yaml.
    """

    efforts: tuple[str, ...]
    mandatory: bool


@dataclass(frozen=True, slots=True)
class VendorPrice:
    """What a model costs, in USD per million tokens, as the vendor quotes it.

    OpenRouter quotes USD per *single* token as a decimal string ("0.000003"),
    which is unreadable in a picker and lossy as a float, so the parser scales
    it through ``Decimal`` to the per-million figure every price list uses.
    ``min_prompt_tokens`` is None on the base price and set on a tier that only
    applies above a prompt-length threshold.
    """

    prompt: float | None = None
    completion: float | None = None
    min_prompt_tokens: int | None = None


@dataclass(frozen=True, slots=True)
class VendorVideo:
    """The parameter surface a video model publishes.

    None on a list means the vendor published no value for it, which is not the
    same as an empty list and must never be compared against the yaml.
    """

    durations: tuple[int, ...] | None = None
    resolutions: tuple[str, ...] | None = None
    aspect_ratios: tuple[str, ...] | None = None
    # Explicit "WxH" sizes, where a model offers those instead of resolutions.
    sizes: tuple[str, ...] | None = None
    audio: bool | None = None
    image_input: bool | None = None
    seed: bool | None = None


@dataclass(frozen=True, slots=True)
class VendorModel:
    """One catalogue row, reduced to the facts a vendor actually publishes.

    Every optional field is None when this vendor does not publish it, and the
    comparison skips None rather than reading it as a contradiction. That
    distinction is what keeps the check from inventing drift out of OpenAI's
    ``/models``, which carries an id and a shutdown date and nothing else.
    """

    id: str
    # The vendor's display name and blurb, where it publishes them (OpenRouter
    # does); the video dialog shows the name, the pickers show the id.
    name: str | None = None
    description: str | None = None
    context_length: int | None = None
    max_output_tokens: int | None = None
    temperature: bool | None = None
    reasoning: VendorReasoning | None = None
    # Whether the model takes a reasoning-effort setting, read from the
    # vendor's parameter list rather than from the reasoning block above: the
    # two are published separately and the picker's control follows the
    # parameter. None where the vendor publishes no parameter list.
    accepts_reasoning_effort: bool | None = None
    # Whether this row's catalogue speaks about reasoning at all. Needed
    # because a missing block means two opposite things: on OpenRouter's chat
    # catalogue it is the vendor saying "this model does not reason", and on
    # OpenAI's bare /models it is the vendor saying nothing whatsoever. Only
    # the parser knows which catalogue it read, so it records the answer here.
    publishes_reasoning: bool = False
    # Vendor-announced sunset: OpenRouter's ``expiration_date``, OpenAI's
    # ``shutdown_date``. The yaml's ``deprecated:`` is the same fact by hand.
    retires_on: date | None = None
    # The base price and the ladder of prices that take over above a
    # prompt-length threshold, cheapest threshold first. Empty for the vast
    # majority of models, which price one way at any length. None where the
    # vendor publishes no price, or one this app cannot read honestly.
    pricing: VendorPrice | None = None
    price_tiers: tuple[VendorPrice, ...] = ()
    video: VendorVideo | None = None


@dataclass(frozen=True, slots=True)
class CatalogProbe:
    """One vendor question and its answer, successful or not."""

    kind: ProviderKind
    interaction: Interaction
    endpoint: str | None
    status: CatalogStatus
    detail: str
    models: tuple[VendorModel, ...] = ()
    # True when the vendor paged and we did not walk every page. Present models
    # are still real; a missing one proves nothing, so the caller must not
    # report it as retired.
    partial: bool = False

    @property
    def usable(self) -> bool:
        """Whether this answer may be compared against the curated file."""
        return self.status is CatalogStatus.OK

    def find(self, model_id: str) -> VendorModel | None:
        return next((m for m in self.models if m.id == model_id), None)

    def lists(self, model_id: str) -> bool:
        return self.find(model_id) is not None


class _UnreadableError(Exception):
    """The body arrived and could not be understood. Never leaves this module."""


def credential_from_env(spec: ProviderSpec) -> str | None:
    """A key for this vendor from the environment, if CI was given one."""
    for name in spec.key_env:
        value = os.environ.get(name)
        if value:
            return value
    return None


def needs_credentials(spec: ProviderSpec, interaction: Interaction) -> bool:
    """Whether this vendor refuses its catalogue without a key.

    False for a catalogue declared ``public`` (OpenRouter's, verified
    unauthenticated on 2026-09-02: HTTP 200, 425 models, which is what lets
    the CI check run with no secret at all) and for a kind whose server may
    not check one (``auth: optional``, the self-hosted kind). A key is still
    sent when one is around: an authenticated call sees the same list plus
    the account's own limits.
    """
    catalog = spec.catalog(interaction)
    if catalog is None:
        return True
    return not catalog.public and spec.auth == "api_key"


def _catalog(spec: ProviderSpec, interaction: Interaction) -> Surface | None:
    return spec.catalog(interaction)


def endpoint_for(
    spec: ProviderSpec, interaction: Interaction, *, base_url: str | None = None
) -> str | None:
    """The catalogue URL for one provider and interaction, or None.

    Read from the capability config rather than restated here, because the
    endpoints are already written down once and a second copy is exactly the
    drift this feature exists to catch. None means there is nothing to call:
    the vendor publishes no catalogue, or the address belongs to a server only
    the operator can name and ``base_url`` did not supply it. A stored
    provider's base already carries the version segment the connectors post
    to ("http://speaches:8000/v1"), so the self-hosted catalogue is spliced
    beside it as "{base_url}/models".
    """
    catalog = _catalog(spec, interaction)
    return catalog.resolve(base_url) if catalog else None


def _needs_base_url(spec: ProviderSpec, interaction: Interaction) -> bool:
    """Whether this endpoint is one only an operator's base URL can locate."""
    catalog = _catalog(spec, interaction)
    return catalog is not None and catalog.resolve(None) is None


async def probe(
    kind: ProviderKind,
    interaction: Interaction,
    *,
    spec: ProviderSpec | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
    client_factory: ClientFactory | None = None,
    request_timeout: float = DEFAULT_TIMEOUT_S,
) -> CatalogProbe:
    """Ask one vendor what it offers. Never raises, never blocks forever.

    ``spec`` is the provider's entry in the capability config, defaulting to
    the shipped file's; the staleness check passes its own so a test can run
    it against a synthetic file. ``base_url`` is a provider row's override,
    which is the only way to locate a self-hosted server's catalogue.
    """
    if spec is None:
        spec = capabilities.config().provider(kind)
    url = endpoint_for(spec, interaction, base_url=base_url) if spec else None
    if spec is None or url is None:
        detail = _nothing_to_call(spec, interaction)
        return CatalogProbe(kind, interaction, None, CatalogStatus.NO_CATALOGUE, detail)
    if api_key is None and needs_credentials(spec, interaction):
        env = ", ".join(spec.key_env) or "an API key"
        return CatalogProbe(
            kind,
            interaction,
            url,
            CatalogStatus.NO_CREDENTIALS,
            f"not checked, no credentials (set {env})",
        )
    catalog = _catalog(spec, interaction)
    headers = catalog.request_headers(api_key) if catalog else {}
    factory = client_factory or (lambda: httpx.AsyncClient(timeout=request_timeout))
    try:
        async with factory() as client:
            response = await client.get(url, headers=headers)
            response.raise_for_status()
            body = response.content
    except httpx.HTTPError as exc:
        # Connection refused, DNS failure, timeout, 4xx, 5xx, a proxy in the
        # way: everything that stopped a body from arriving is one status.
        return _unreachable(kind, interaction, url, exc)
    except Exception as exc:  # pragma: no cover - the "never crash" backstop
        # A catalogue read may not be the thing that takes an app down, so an
        # exception nobody anticipated (a transport raising OSError, an SSL
        # library disagreeing with a proxy) still becomes a status.
        return _unreachable(kind, interaction, url, exc)
    try:
        # A body that is not JSON at all (an HTML error page from a captive
        # portal, a truncated response) is a shape problem, not a reachability
        # one, and lands in the same bucket as a schema that moved.
        payload = cast("object", json.loads(body))
        models, partial = _parse(kind, interaction, payload)
    except (ValueError, TypeError, _UnreadableError) as exc:
        log.warning(
            "catalog.unreadable",
            kind=kind.value,
            interaction=interaction.value,
            error=str(exc),
        )
        return CatalogProbe(
            kind, interaction, url, CatalogStatus.UNREADABLE, f"unrecognised response: {exc}"
        )
    return CatalogProbe(
        kind,
        interaction,
        url,
        CatalogStatus.OK,
        f"{len(models)} models" + (" (paginated, partial)" if partial else ""),
        tuple(models),
        partial=partial,
    )


def _nothing_to_call(spec: ProviderSpec | None, interaction: Interaction) -> str:
    """Why there is no address: the report has to tell the nothings apart.

    A kind the file does not know, a vendor that publishes no list at all, or
    a list that exists on a server whose address only the operator knows.
    """
    if spec is None:
        return "unknown provider kind"
    if _needs_base_url(spec, interaction):
        return "catalogue lives on the operator's own server, no address to call"
    return "vendor publishes no catalogue for this interaction"


def _unreachable(
    kind: ProviderKind, interaction: Interaction, url: str, exc: Exception
) -> CatalogProbe:
    detail = f"{type(exc).__name__}: {exc}".strip().rstrip(":")
    log.warning(
        "catalog.unreachable",
        kind=kind.value,
        interaction=interaction.value,
        error=detail,
    )
    return CatalogProbe(
        kind, interaction, url, CatalogStatus.UNREACHABLE, f"could not check: {detail}"
    )


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------


def _rows(payload: object, keys: Sequence[str]) -> list[dict[str, object]]:
    """The list of model objects in a body, whichever envelope it uses.

    Raises rather than returning [] for anything unexpected, including an empty
    list: no vendor here serves an empty catalogue, so an empty one means the
    shape moved and every curated model would otherwise look retired.
    """
    data: object = payload
    if isinstance(payload, dict):
        mapping = cast("dict[str, object]", payload)
        for key in keys:
            candidate = mapping.get(key)
            if isinstance(candidate, list):
                data = cast("list[object]", candidate)
                break
        else:
            raise _UnreadableError(f"no {' / '.join(keys)} list in the response body")
    if not isinstance(data, list):
        raise _UnreadableError(f"expected a list of models, got {type(data).__name__}")
    entries = cast("list[object]", data)
    # A bare string is a row with nothing but an id: some self-hosted servers
    # claiming OpenAI compatibility answer ``/models`` with a list of names,
    # and no parser here reads anything else from such a row.
    rows: list[dict[str, object]] = [
        cast("dict[str, object]", r) if isinstance(r, dict) else {"id": r}
        for r in entries
        if isinstance(r, dict | str)
    ]
    if not rows:
        raise _UnreadableError("catalogue is empty")
    return rows


def _str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _int(value: object) -> int | None:
    # bool is an int in Python and would silently become 0/1 here.
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _bool(value: object) -> bool | None:
    return value if isinstance(value, bool) else None


def _str_tuple(value: object) -> tuple[str, ...] | None:
    if not isinstance(value, list):
        return None
    return tuple(v for v in cast("list[object]", value) if isinstance(v, str))


def _int_tuple(value: object) -> tuple[int, ...] | None:
    if not isinstance(value, list):
        return None
    return tuple(v for v in cast("list[object]", value) if isinstance(v, int))


def _date(value: object) -> date | None:
    """An ISO date from a vendor field, or None if it is anything else.

    Vendors put sentinels in here (OpenRouter carries 2098-12-31 for models
    with no real sunset), so parsing is deliberately permissive and the
    comparison decides what a far-future date means.
    """
    text = _str(value)
    if text is None:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _dict(value: object) -> dict[str, object] | None:
    return cast("dict[str, object]", value) if isinstance(value, dict) else None


# A price quoted per single token, scaled to the per-million figure people read.
_PER_MILLION = 1_000_000


def _usd_per_million(raw: object) -> float | None:
    """ "0.000003" (USD per token) -> 3.0 (USD per million tokens).

    Parsed as ``Decimal`` rather than ``float`` so the scaling is exact: the
    source values run to nine decimal places, where binary floating point
    starts printing 2.9999999999999996 at people. Anything unparseable (a
    missing key, an empty string, a future non-numeric marker) yields None:
    a price we cannot read must render as "unknown", never as free.
    """
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        return float(Decimal(raw) * _PER_MILLION)
    except (InvalidOperation, ValueError):
        return None


def _price(source: dict[str, object], min_prompt_tokens: object = None) -> VendorPrice | None:
    """A price from a ``pricing`` (or ``pricing.overrides[]``) object, or None
    when it names neither an input nor an output price."""
    prompt = _usd_per_million(source.get("prompt"))
    completion = _usd_per_million(source.get("completion"))
    if prompt is None and completion is None:
        return None
    return VendorPrice(
        prompt=prompt, completion=completion, min_prompt_tokens=_int(min_prompt_tokens)
    )


def _price_tiers(pricing: dict[str, object]) -> tuple[VendorPrice, ...]:
    """The ``overrides`` ladder, prices that take over above a prompt-length
    threshold, cheapest threshold first."""
    raw = pricing.get("overrides")
    if not isinstance(raw, list):
        return ()
    tiers: list[VendorPrice] = []
    for override in cast("list[object]", raw):
        entry = _dict(override)
        if entry is None:
            continue
        tier = _price(entry, entry.get("min_prompt_tokens"))
        if tier is not None:
            tiers.append(tier)
    return tuple(sorted(tiers, key=lambda t: t.min_prompt_tokens or 0))


def _is_transcription_model(row: dict[str, object]) -> bool:
    architecture = _dict(row.get("architecture"))
    outputs = _str_tuple(architecture.get("output_modalities")) if architecture else None
    return outputs is not None and "transcription" in outputs


# Parameter names that mean "this model takes a reasoning-effort setting".
# OpenRouter publishes these per model in ``supported_parameters``; both
# spellings appear across its catalogue, and either is enough to offer the
# control.
_REASONING_PARAMS = frozenset({"reasoning", "reasoning_effort"})


def _reasoning(row: dict[str, object]) -> VendorReasoning | None:
    block = _dict(row.get("reasoning"))
    if block is None:
        return None
    return VendorReasoning(
        efforts=_str_tuple(block.get("supported_efforts")) or (),
        mandatory=_bool(block.get("mandatory")) or False,
    )


def _openrouter_chat(rows: list[dict[str, object]]) -> list[VendorModel]:
    """OpenRouter ``GET /api/v1/models``.

    The same schema serves the transcription list (``?output_modalities=
    transcription``), so this parser reads both. The fields read here are the
    ones the gateway genuinely publishes and that the yaml mirrors, plus the
    ones the pickers show: context_length, top_provider.max_completion_tokens,
    whether ``temperature`` and a reasoning effort are in supported_parameters,
    the reasoning block, and the price. Nothing else in this payload is treated
    as a fact about a model.

    Transcription models are priced per unit of *audio*, not per token, and
    the catalogue does not say which unit: measured against the live API,
    deepgram/nova-3's "0.0043" bills per minute while nvidia/nemotron-3.5-asr's
    "0.00000333" bills per second. Nothing in the payload distinguishes the
    two, so any figure shown would be wrong by a factor of 60 for some models.
    Reporting no price is the honest option; OpenRouter's own model page is the
    place to check an audio rate.
    """
    models: list[VendorModel] = []
    for row in rows:
        model_id = _str(row.get("id"))
        if model_id is None:
            continue
        top = _dict(row.get("top_provider")) or {}
        params = _str_tuple(row.get("supported_parameters"))
        pricing = None if _is_transcription_model(row) else _dict(row.get("pricing"))
        models.append(
            VendorModel(
                id=model_id,
                name=_str(row.get("name")),
                description=_str(row.get("description")),
                context_length=_int(row.get("context_length")),
                max_output_tokens=_int(top.get("max_completion_tokens")),
                temperature=("temperature" in params) if params is not None else None,
                reasoning=_reasoning(row),
                accepts_reasoning_effort=(
                    any(p in _REASONING_PARAMS for p in params) if params is not None else None
                ),
                publishes_reasoning=True,
                retires_on=_date(row.get("expiration_date")),
                pricing=_price(pricing) if pricing is not None else None,
                price_tiers=_price_tiers(pricing) if pricing is not None else (),
            )
        )
    return models


def _openrouter_video(rows: list[dict[str, object]]) -> list[VendorModel]:
    """OpenRouter ``GET /api/v1/videos/models``.

    A separate schema from the chat catalogue, and the whole ``video:`` block
    in the yaml except the prompt limits comes from it. ``generate_audio`` and
    ``seed`` are read as tri-states: null means the vendor says nothing, which
    the staleness check renders as silence rather than as "no audio", so it
    must not be flattened to False here; the generate dialog flattens it for
    its own form, where a knob the vendor did not vouch for is not offered.
    """
    models: list[VendorModel] = []
    for row in rows:
        model_id = _str(row.get("id"))
        if model_id is None:
            continue
        # Present-and-null is the vendor saying "this model takes no frame
        # image", which is where the yaml's image_input: false came from.
        # Absent entirely would mean the field has gone away, and that must
        # read as "unknown" rather than turn every model into a finding.
        frames = "supported_frame_images" in row
        models.append(
            VendorModel(
                id=model_id,
                name=_str(row.get("name")),
                description=_str(row.get("description")),
                retires_on=_date(row.get("expiration_date")),
                video=VendorVideo(
                    durations=_int_tuple(row.get("supported_durations")),
                    resolutions=_str_tuple(row.get("supported_resolutions")),
                    aspect_ratios=_str_tuple(row.get("supported_aspect_ratios")),
                    sizes=_str_tuple(row.get("supported_sizes")),
                    audio=_bool(row.get("generate_audio")),
                    image_input=(
                        bool(_str_tuple(row.get("supported_frame_images"))) if frames else None
                    ),
                    seed=_bool(row.get("seed")),
                ),
            )
        )
    return models


def _openai(rows: list[dict[str, object]]) -> list[VendorModel]:
    """OpenAI ``GET /v1/models``.

    Carries id, object, created, owned_by and a nullable shutdown_date, and
    nothing that distinguishes a transcription model from an image one. So it
    is usable for exactly two things: does this id still exist, and has OpenAI
    announced a date for it. Every capability in the yaml for this vendor stays
    hand-annotated from the model cards.

    Also the parser for anything claiming OpenAI compatibility (Speaches,
    Ollama, LM Studio, vLLM), whose rows may carry nothing but an id, or be
    bare id strings: see :func:`_rows`.
    """
    models: list[VendorModel] = []
    for row in rows:
        model_id = _str(row.get("id"))
        if model_id is None:
            continue
        models.append(VendorModel(id=model_id, retires_on=_date(row.get("shutdown_date"))))
    return models


def _deepgram(rows: list[dict[str, object]]) -> list[VendorModel]:
    """Deepgram ``GET /v1/models``.

    Rows carry both ``name`` (the value you send as ``model=``) and
    ``canonical_name`` (a versioned alias). Both are recorded as separate
    entries so a curated id matches whichever spelling it used.
    """
    models: list[VendorModel] = []
    for row in rows:
        for key in ("name", "canonical_name"):
            model_id = _str(row.get(key))
            if model_id is not None:
                models.append(VendorModel(id=model_id))
    return models


def _gemini(rows: list[dict[str, object]]) -> list[VendorModel]:
    """Gemini ``GET /v1beta/models``.

    Ids arrive as ``models/gemini-3.5-transcribe``; the yaml stores the bare
    name because that is what the request body carries.
    """
    models: list[VendorModel] = []
    for row in rows:
        name = _str(row.get("name"))
        if name is None:
            continue
        models.append(VendorModel(id=name.removeprefix("models/")))
    return models


# Envelope keys per vendor, most specific first.
_ENVELOPES: dict[ProviderKind, tuple[str, ...]] = {
    ProviderKind.OPENROUTER: ("data",),
    ProviderKind.OPENAI: ("data",),
    ProviderKind.OPENAI_COMPAT: ("data",),
    # Deepgram splits its catalogue by modality; only the STT half is ours.
    ProviderKind.DEEPGRAM: ("stt", "data", "models"),
    ProviderKind.GEMINI: ("models", "data"),
    ProviderKind.ASSEMBLYAI: ("data", "models"),
}


def _parse(
    kind: ProviderKind, interaction: Interaction, payload: object
) -> tuple[list[VendorModel], bool]:
    """Body to models, plus whether the answer was only one page of several."""
    rows = _rows(payload, _ENVELOPES.get(kind, ("data",)))
    if kind is ProviderKind.OPENROUTER:
        models = (
            _openrouter_video(rows) if interaction is Interaction.VIDEO else _openrouter_chat(rows)
        )
    elif kind is ProviderKind.DEEPGRAM:
        models = _deepgram(rows)
    elif kind is ProviderKind.GEMINI:
        models = _gemini(rows)
    else:
        # OpenAI and anything claiming OpenAI compatibility.
        models = _openai(rows)
    if not models:
        raise _UnreadableError(f"{len(rows)} rows carried no model id")
    # Gemini pages its catalogue. An unwalked page is not evidence of absence,
    # so the flag travels with the answer and the comparison honours it.
    envelope = _dict(payload)
    partial = bool(_str(envelope.get("nextPageToken"))) if envelope else False
    return models, partial


__all__ = [
    "DEFAULT_TIMEOUT_S",
    "CatalogProbe",
    "CatalogStatus",
    "ClientFactory",
    "VendorModel",
    "VendorPrice",
    "VendorReasoning",
    "VendorVideo",
    "credential_from_env",
    "endpoint_for",
    "needs_credentials",
    "probe",
]
