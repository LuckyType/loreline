"""Provider CRUD + secret + connection-test routes."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Request
from fastapi.exceptions import HTTPException
from pydantic import BaseModel
from starlette.status import HTTP_404_NOT_FOUND

from loreline.health import HealthReport, HealthStatus, missing_credential
from loreline.llm import LLM_KINDS, chat_health
from loreline.models import Interaction, ModelInfo, ProviderConfig, ProviderKind
from loreline.secrets import SecretStore
from loreline.stt.catalog import list_models
from loreline.stt.registry import create_backend
from loreline.web.auth import require_auth
from loreline.web.deps import get_state, load_action_defaults
from loreline.web.schemas import OkResponse, ProviderCreate, SecretWrite

router = APIRouter(
    prefix="/api/providers", tags=["providers"], dependencies=[Depends(require_auth)]
)


class ProviderView(ProviderConfig):
    """Provider config plus a masked hint of its stored API key (read-only)."""

    secret_set: bool = False
    secret_hint: str | None = None


def _view(provider: ProviderConfig, secrets: SecretStore) -> ProviderView:
    """Decorate a stored provider with a masked secret hint for the UI."""
    hint = secrets.hint(provider.auth_ref) if provider.auth_ref else None
    return ProviderView(**provider.model_dump(), secret_set=hint is not None, secret_hint=hint)


class TestResult(BaseModel):
    """Connection-test outcome for a provider.

    Deliberately not a bool. "Healthy" is at least three separate facts - the
    endpoint answers, the credential is accepted, the vendor is not currently
    refusing - and collapsing them meant a provider with a completely invalid
    key rendered exactly like one whose base URL was a typo. The settings page
    switches on ``status`` and shows ``detail`` as the badge's tooltip, so the
    vendor's own "API key not valid" reaches the GM instead of the word "down".
    See :mod:`loreline.health`.
    """

    status: HealthStatus
    detail: str | None = None


class ProviderModelsRequest(BaseModel):
    """Connection details for listing a provider's available models.

    Works before the provider is saved (new add-provider flow): pass ``api_key``
    directly, or ``provider_id`` to reuse the stored secret when the key is blank.
    """

    kind: ProviderKind
    base_url: str | None = None
    api_key: str | None = None
    provider_id: str | None = None
    interaction: Interaction = Interaction.TRANSCRIBE
    """Scopes the returned models to what can actually serve this interaction."""


@router.get("")
async def list_providers(request: Request) -> list[ProviderView]:
    """Return all configured providers with masked key hints."""
    state = get_state(request)
    return [_view(p, state.secrets) for p in await state.providers.list()]


@router.post("", status_code=201)
async def create_provider(request: Request, body: ProviderCreate) -> ProviderView:
    """Create a provider; optionally store its API key in the same request."""
    state = get_state(request)
    provider_id = uuid.uuid4().hex
    auth_ref = f"provider:{provider_id}"
    provider = ProviderConfig(
        id=provider_id,
        auth_ref=auth_ref,
        **body.model_dump(exclude={"api_key"}),
    )
    await state.providers.upsert(provider)
    if body.api_key:
        state.secrets.set(auth_ref, body.api_key)
    return _view(provider, state.secrets)


@router.put("/{provider_id}")
async def update_provider(request: Request, provider_id: str, body: ProviderCreate) -> ProviderView:
    """Replace a provider's config; set the API key too when one is supplied."""
    state = get_state(request)
    repo = state.providers
    existing = await repo.get(provider_id)
    if existing is None:
        raise HTTPException(status_code=HTTP_404_NOT_FOUND, detail="provider not found")
    auth_ref = existing.auth_ref or f"provider:{provider_id}"
    provider = ProviderConfig(
        id=provider_id,
        auth_ref=auth_ref,
        **body.model_dump(exclude={"api_key"}),
    )
    await repo.upsert(provider)
    if body.api_key:
        state.secrets.set(auth_ref, body.api_key)
    return _view(provider, state.secrets)


@router.delete("/{provider_id}")
async def delete_provider(request: Request, provider_id: str) -> OkResponse:
    """Delete a provider and its stored secret (if any)."""
    state = get_state(request)
    existing = await state.providers.get(provider_id)
    if existing is None:
        raise HTTPException(status_code=HTTP_404_NOT_FOUND, detail="provider not found")
    if existing.auth_ref:
        state.secrets.delete(existing.auth_ref)
    await state.providers.delete(provider_id)
    return OkResponse()


@router.post("/{provider_id}/secret")
async def set_secret(request: Request, provider_id: str, body: SecretWrite) -> OkResponse:
    """Store an API key for a provider in the secret store (write-only)."""
    state = get_state(request)
    provider = await state.providers.get(provider_id)
    if provider is None:
        raise HTTPException(status_code=HTTP_404_NOT_FOUND, detail="provider not found")
    ref = provider.auth_ref or f"provider:{provider_id}"
    state.secrets.set(ref, body.value)
    if provider.auth_ref != ref:
        await state.providers.upsert(provider.model_copy(update={"auth_ref": ref}))
    return OkResponse()


@router.post("/{provider_id}/test")
async def test_provider(request: Request, provider_id: str) -> TestResult:
    """Probe the provider's endpoint and credential, and grade the answer.

    Only a missing *provider* is an HTTP error here. Everything the probe can
    run into - a rejected key, a dead host, a config this app cannot build a
    connector for - comes back as a graded :class:`TestResult`, because the
    button's job is to report a state, and an error status just makes the page
    render "down" with no explanation attached.
    """
    state = get_state(request)
    provider = await state.providers.get(provider_id)
    if provider is None:
        raise HTTPException(status_code=HTTP_404_NOT_FOUND, detail="provider not found")
    api_key = state.secrets.get(provider.auth_ref) if provider.auth_ref else None
    # Answered without a network call, and not merely as an optimisation: see
    # missing_credential on the keyless 404 that would otherwise read as a bad
    # base URL.
    report = missing_credential(provider.kind, api_key)
    if report is not None:
        return _result(report)
    if provider.kind in LLM_KINDS:
        # One probe per row, and for a kind that both summarizes and
        # transcribes (Gemini, OpenAI, OpenRouter) the chat surface is the one
        # asked. The key is the same credential either way, so a second probe
        # against the STT surface would cost a round trip to learn nothing -
        # and for Gemini the two surfaces are sibling URLs with different auth
        # headers, so it would have to build a second client to ask.
        return _result(await chat_health(config=provider, api_key=api_key))
    try:
        backend = create_backend(provider, state.secrets)
    except ValueError as exc:
        # capabilities.yaml offers this kind no transcription model, or the
        # resolved model needs a transport we have no connector for. Nothing
        # was probed, so nothing is known about the provider itself - but the
        # message says exactly what to change, so it goes to the GM rather
        # than into a 400 the page turns into a bare red badge.
        return TestResult(status=HealthStatus.UNKNOWN, detail=str(exc))
    try:
        return _result(await backend.health())
    finally:
        await backend.aclose()


def _result(report: HealthReport) -> TestResult:
    return TestResult(status=report.status, detail=report.detail)


@router.post("/models")
async def provider_models(request: Request, body: ProviderModelsRequest) -> list[ModelInfo]:
    """List a provider's available models (live /v1/models, else a curated set).

    Entries carry price/context length only where the provider publishes them
    (OpenRouter); everywhere else it's the bare id, exactly as before.
    """
    state = get_state(request)
    api_key = body.api_key
    if not api_key and body.provider_id:
        existing = await state.providers.get(body.provider_id)
        if existing is not None and existing.auth_ref:
            api_key = state.secrets.get(existing.auth_ref)
    defaults = await load_action_defaults(state)
    return await list_models(
        kind=body.kind,
        base_url=body.base_url,
        api_key=api_key,
        interaction=body.interaction,
        strict_filtering=defaults.strict_model_filtering,
    )
