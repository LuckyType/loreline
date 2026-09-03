"""The startup warning about a GM's favourite models.

Everything here is about restraint. This check runs while the app is coming up,
so the tests that matter most are the ones asserting it stays quiet: when the
vendor is unreachable, when only some of a provider's catalogues answered, when
a provider is disabled, and when nothing has been favourited at all. A false
"your model is gone" at boot is worse than no warning, and an exception on the
startup path is worse than both.
"""

from __future__ import annotations

from datetime import date

import httpx
import pytest

from loreline.capability_config import CapabilityConfig
from loreline.models import Protocol, ProviderConfig, ProviderKind
from loreline.staleness.report import Code, Severity
from loreline.staleness.startup import stale_favorites, warn_about_stale_favorites

TODAY = date(2026, 9, 2)


class _Keys:
    """A secret store that always has a key, so credentials never gate a test."""

    def get(self, name: str) -> str | None:
        return f"key-for-{name}"


class _BrokenKeys:
    """A secret store that has fallen over."""

    def get(self, name: str) -> str | None:
        raise RuntimeError("secret store is unreadable")


def _provider(
    kind: ProviderKind = ProviderKind.OPENROUTER,
    *,
    favorites: list[str] | None = None,
    enabled: bool = True,
) -> ProviderConfig:
    return ProviderConfig(
        id="p1",
        name="Table provider",
        kind=kind,
        auth_ref="provider:p1",
        protocol=Protocol.HTTP_BATCH,
        favorite_models=favorites if favorites is not None else ["openai/whisper-large-v3-turbo"],
        enabled=enabled,
    )


# The undated, offered model every synthetic provider needs: the schema wants
# exactly one default per offered interaction and refuses a dated one.
CURRENT_MODEL: dict[str, object] = {
    "id": "some-model",
    "interactions": ["transcribe"],
    "default": True,
    "transcribe": {"batch": True},
}


def _spec(catalog: str | None = None, **overrides: object) -> dict[str, object]:
    """A transcribing provider, with a catalogue where the test needs one.

    The schema wants a surface for every interaction, so a placeholder batch
    surface is always present; only the catalogue is what these tests read.
    """
    surfaces: dict[str, object] = {
        "transcribe": {"batch": {"url": "https://example.invalid/v1", "auth": "bearer"}}
    }
    if catalog:
        surfaces["catalog"] = {"transcribe": {"url": catalog, "auth": "bearer"}}
    spec: dict[str, object] = {
        "label": "Test",
        "key_url": "https://example.invalid/keys",
        "surfaces": surfaces,
        "interactions": ["transcribe"],
        "models": [dict(CURRENT_MODEL)],
    }
    spec.update(overrides)
    return spec


def _config_with_dated_model(model_id: str, deprecated: str) -> CapabilityConfig:
    """A capability config carrying one dated model, built from nothing.

    Deliberately synthetic. Pointing a date test at a real entry in
    capabilities.yaml makes it a test of today's curation rather than of the
    check, and it breaks the day somebody retires that model - which is the
    exact event this feature exists to make easy.
    """
    dated: dict[str, object] = {
        "id": model_id,
        "interactions": ["transcribe"],
        "transcribe": {"batch": True},
        "deprecated": deprecated,
    }
    providers: dict[str, object] = {kind.value: _spec() for kind in ProviderKind}
    providers[ProviderKind.OPENROUTER.value] = _spec(
        catalog="https://example.invalid/models",
        models=[dated, dict(CURRENT_MODEL)],
    )
    return CapabilityConfig.model_validate({"version": 1, "providers": providers})


def _factory(handler: httpx.MockTransport) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=handler)


def _listing(*model_ids: str) -> httpx.MockTransport:
    body = {"data": [{"id": model_id} for model_id in model_ids]}
    return httpx.MockTransport(lambda _r: httpx.Response(200, json=body))


def _unreachable() -> httpx.MockTransport:
    def handle(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    return httpx.MockTransport(handle)


async def _findings(
    provider: ProviderConfig,
    transport: httpx.MockTransport,
    *,
    config: CapabilityConfig | None = None,
):  # type: ignore[no-untyped-def]
    return await stale_favorites(
        [provider],
        api_key_for=lambda _p: "k",
        config=config,
        today=TODAY,
        client_factory=lambda: _factory(transport),
    )


async def test_a_favorite_the_vendor_dropped_is_reported() -> None:
    """Every catalogue this kind serves answered, and none of them lists it."""
    transport = _listing("openai/whisper-large-v3", "openai/gpt-transcribe")
    findings = await _findings(_provider(), transport)
    assert [(f.code, f.model) for f in findings] == [
        (Code.MODEL_RETIRED, "openai/whisper-large-v3-turbo")
    ]
    assert findings[0].severity is Severity.ERROR
    assert "Table provider" in findings[0].message


async def test_a_favorite_that_is_still_listed_says_nothing() -> None:
    transport = _listing("openai/whisper-large-v3-turbo")
    assert await _findings(_provider(), transport) == []


async def test_an_unreachable_vendor_produces_no_warning() -> None:
    """The one that matters. A vendor being down at boot must not tell a GM
    their model has been retired."""
    assert await _findings(_provider(), _unreachable()) == []


async def test_one_failed_catalogue_silences_the_whole_provider() -> None:
    """OpenRouter serves three disjoint catalogues and a favourite only has to
    appear in one of them, so a single unreachable endpoint makes the question
    unanswerable. Answering it anyway would report every video favourite as
    retired whenever the video endpoint hiccuped."""

    def handle(request: httpx.Request) -> httpx.Response:
        if "videos" in str(request.url):
            raise httpx.ReadTimeout("timed out")
        return httpx.Response(200, json={"data": [{"id": "openai/whisper-large-v3"}]})

    assert await _findings(_provider(), httpx.MockTransport(handle)) == []


async def test_a_deprecated_favorite_is_reported_without_any_vendor() -> None:
    """The offline half: the yaml already records the sunset date, so this
    warning arrives even on a machine with no network at all."""
    config = _config_with_dated_model("retiring-model", "2026-09-24")
    findings = await _findings(
        _provider(favorites=["retiring-model"]), _unreachable(), config=config
    )
    assert [(f.code, f.model, f.severity) for f in findings] == [
        (Code.DEPRECATION_NEAR, "retiring-model", Severity.WARNING)
    ]


async def test_a_retired_favorite_is_an_error_from_the_file_alone() -> None:
    """What a GM sees when they boot the app after the date has passed and
    nobody has updated the file."""
    config = _config_with_dated_model("retiring-model", "2026-01-01")
    findings = await _findings(
        _provider(favorites=["retiring-model"]), _unreachable(), config=config
    )
    assert [(f.code, f.severity) for f in findings] == [(Code.DEPRECATION_PASSED, Severity.ERROR)]


async def test_a_favorite_with_no_date_at_all_is_silent_offline() -> None:
    """The normal case, and the one that must stay quiet: nothing curated is
    near retirement, so a boot with no reachable vendor says nothing."""
    config = _config_with_dated_model("retiring-model", "2027-12-31")
    assert await _findings(_provider(favorites=["some-model"]), _unreachable(), config=config) == []


@pytest.mark.parametrize(
    "provider",
    [_provider(favorites=[]), _provider(enabled=False)],
    ids=["no favorites", "disabled"],
)
async def test_a_provider_with_nothing_to_check_is_never_called(
    provider: ProviderConfig,
) -> None:
    """Startup makes no outbound call at all for a row that has favourited
    nothing, which is every row in a fresh install."""
    calls: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, json={"data": []})

    assert await _findings(provider, httpx.MockTransport(handle)) == []
    assert calls == []


async def test_two_rows_of_one_kind_share_a_single_fetch() -> None:
    """Two OpenRouter providers with different keys still list the same models,
    and a boot should not pay for the same catalogue twice."""
    calls: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, json={"data": [{"id": "openai/whisper-large-v3-turbo"}]})

    second = _provider().model_copy(update={"id": "p2", "name": "Second"})
    findings = await stale_favorites(
        [_provider(), second],
        api_key_for=lambda _p: "k",
        today=TODAY,
        client_factory=lambda: _factory(httpx.MockTransport(handle)),
    )
    assert findings == []
    # Three catalogues for the kind, fetched once each rather than twice.
    assert len(calls) == 3


async def test_the_self_hosted_kind_is_asked_at_its_own_address() -> None:
    """Its catalogue endpoint is a template over the operator's base URL, which
    only the provider row knows - so this check can reach a server the CI one
    never can. The stored base URL already carries the version segment, which
    the template also spells, and asking for /v1/v1/models would 404."""
    calls: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, json={"data": [{"id": "Systran/faster-whisper-large-v3"}]})

    provider = _provider(ProviderKind.OPENAI_COMPAT, favorites=["whisper-1"]).model_copy(
        update={"base_url": "http://speaches:8000/v1"}
    )
    findings = await stale_favorites(
        [provider],
        api_key_for=lambda _p: None,
        today=TODAY,
        client_factory=lambda: _factory(httpx.MockTransport(handle)),
    )
    assert calls == ["http://speaches:8000/v1/models"] * len(calls)
    assert [f.model for f in findings] == ["whisper-1"]


async def test_the_wrapper_swallows_everything_and_still_returns() -> None:
    """Nothing on the startup path may raise, including a bug in this check or
    a secret store that has fallen over."""
    findings = await warn_about_stale_favorites(
        [_provider()],
        secrets=_BrokenKeys(),
        today=TODAY,
        client_factory=lambda: _factory(_listing("openai/whisper-large-v3")),
    )
    assert findings == []


async def test_the_wrapper_logs_what_it_finds() -> None:
    findings = await warn_about_stale_favorites(
        [_provider()],
        secrets=_Keys(),
        today=TODAY,
        client_factory=lambda: _factory(_listing("openai/whisper-large-v3")),
    )
    assert [f.code for f in findings] == [Code.MODEL_RETIRED]
