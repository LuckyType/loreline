"""What each vendor's catalogue says today, fetched fail soft.

This is the read half of the staleness feature, and it deliberately knows
nothing about capabilities.yaml. It answers one question - "what does the
vendor publish for this provider and interaction right now" - so that the write
half planned for later (a sync script that regenerates the derivable fields
instead of hand editing them) can reuse it unchanged. Everything that compares
an answer against the curated file lives in :mod:`loreline.staleness.compare`.

FAIL SOFT IS THE POINT. Nothing in here raises. A vendor that is down, rate
limiting, missing a key, or answering with a shape nobody has seen yields a
:class:`CatalogProbe` whose status says so, and the caller reports "could not
check" instead of "your models are gone". Two rules encode that:

* An empty catalogue is UNREADABLE, never OK-with-zero-models. No real vendor
  serves an empty list, so parsing one means the response shape moved under us,
  and treating it as truth would report every curated model as retired.
* A paginated answer we did not follow to the end is marked ``partial``:
  present models are still trustworthy, absent ones prove nothing.

Where each catalogue lives, how the key is spelled for it, whether it answers
without one, and which environment variable CI would find a key in, are all
read from the capability config (the kind's ``catalog`` surface and
``key_env``), never restated here. What stays in this module is how to read
the body, because the bodies differ enough that one generic parser would be a
lie: OpenRouter's chat catalogue publishes reasoning and parameter metadata,
its video catalogue is a different schema entirely, OpenAI's carries nothing
but an id and a shutdown date, Deepgram splits stt from tts, and Gemini
prefixes every id with ``models/``.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from typing import cast

import httpx

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
class VendorVideo:
    """The parameter surface a video model publishes.

    None on a list means the vendor published no value for it, which is not the
    same as an empty list and must never be compared against the yaml.
    """

    durations: tuple[int, ...] | None = None
    resolutions: tuple[str, ...] | None = None
    aspect_ratios: tuple[str, ...] | None = None
    audio: bool | None = None
    image_input: bool | None = None


@dataclass(frozen=True, slots=True)
class VendorModel:
    """One catalogue row, reduced to the facts a vendor actually publishes.

    Every optional field is None when this vendor does not publish it, and the
    comparison skips None rather than reading it as a contradiction. That
    distinction is what keeps the check from inventing drift out of OpenAI's
    ``/models``, which carries an id and a shutdown date and nothing else.
    """

    id: str
    context_length: int | None = None
    max_output_tokens: int | None = None
    temperature: bool | None = None
    reasoning: VendorReasoning | None = None
    # Whether this row's catalogue speaks about reasoning at all. Needed
    # because a missing block means two opposite things: on OpenRouter's chat
    # catalogue it is the vendor saying "this model does not reason", and on
    # OpenAI's bare /models it is the vendor saying nothing whatsoever. Only
    # the parser knows which catalogue it read, so it records the answer here.
    publishes_reasoning: bool = False
    # Vendor-announced sunset: OpenRouter's ``expiration_date``, OpenAI's
    # ``shutdown_date``. The yaml's ``deprecated:`` is the same fact by hand.
    retires_on: date | None = None
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
    spec: ProviderSpec,
    api_key: str | None = None,
    base_url: str | None = None,
    endpoint: str | None = None,
    client_factory: ClientFactory | None = None,
    request_timeout: float = DEFAULT_TIMEOUT_S,
) -> CatalogProbe:
    """Ask one vendor what it offers. Never raises, never blocks forever.

    ``endpoint`` overrides the address from the config, which the startup check
    uses for a self-hosted server whose URL only its provider row knows.
    """
    url = endpoint or endpoint_for(spec, interaction, base_url=base_url)
    if url is None:
        # Two different nothings, and the report has to tell them apart: a
        # vendor that publishes no list at all, versus a list that exists on a
        # server whose address only the operator knows.
        detail = (
            "catalogue lives on the operator's own server, no address to call"
            if _needs_base_url(spec, interaction)
            else "vendor publishes no catalogue for this interaction"
        )
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
        # A staleness check may not be the thing that takes an app down, so an
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
            "staleness.catalog.unreadable",
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


def _unreachable(
    kind: ProviderKind, interaction: Interaction, url: str, exc: Exception
) -> CatalogProbe:
    detail = f"{type(exc).__name__}: {exc}".strip().rstrip(":")
    log.warning(
        "staleness.catalog.unreachable",
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
    rows: list[dict[str, object]] = [
        cast("dict[str, object]", r) for r in entries if isinstance(r, dict)
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

    The fields read here are the ones the gateway genuinely publishes and that
    the yaml mirrors: context_length, top_provider.max_completion_tokens,
    whether ``temperature`` is in supported_parameters, and the reasoning
    block. Nothing else in this payload is treated as a fact about a model.
    """
    models: list[VendorModel] = []
    for row in rows:
        model_id = _str(row.get("id"))
        if model_id is None:
            continue
        top = _dict(row.get("top_provider")) or {}
        params = _str_tuple(row.get("supported_parameters"))
        models.append(
            VendorModel(
                id=model_id,
                context_length=_int(row.get("context_length")),
                max_output_tokens=_int(top.get("max_completion_tokens")),
                temperature=("temperature" in params) if params is not None else None,
                reasoning=_reasoning(row),
                publishes_reasoning=True,
                retires_on=_date(row.get("expiration_date")),
            )
        )
    return models


def _openrouter_video(rows: list[dict[str, object]]) -> list[VendorModel]:
    """OpenRouter ``GET /api/v1/videos/models``.

    A separate schema from the chat catalogue, and the whole ``video:`` block
    in the yaml except the prompt limits comes from it. ``generate_audio`` is
    read as a tri-state: null means the vendor says nothing, which the modal
    renders as silence rather than as "no audio", so it must not be flattened
    to False the way the runtime parser does.
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
                retires_on=_date(row.get("expiration_date")),
                video=VendorVideo(
                    durations=_int_tuple(row.get("supported_durations")),
                    resolutions=_str_tuple(row.get("supported_resolutions")),
                    aspect_ratios=_str_tuple(row.get("supported_aspect_ratios")),
                    audio=_bool(row.get("generate_audio")),
                    image_input=(
                        bool(_str_tuple(row.get("supported_frame_images"))) if frames else None
                    ),
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
