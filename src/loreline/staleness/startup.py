"""Startup warning about the GM's own favourite models, and nothing else.

Scoped to ``ProviderConfig.favorite_models`` deliberately. The full staleness
check has plenty to say about a catalogue of a hundred curated models, and
saying it at every boot would produce a wall of warnings nobody reads, which is
the same as no warning at all. A favourite is a model someone picked on
purpose and expects to find in the picker tonight, so the two things worth
interrupting them about are that it has gone away and that it is about to.

Fail soft, harder than anywhere else in this feature: this runs on the path
that brings the app up. It never raises, never blocks startup (the caller runs
it as a background task), takes a short timeout, and stays silent unless it is
*sure*. Sure means every catalogue that could answer for that provider did
answer: if a vendor is down, or one of a multi-catalogue kind's endpoints
failed, the model is not declared missing, because "your model is gone" is a
much more expensive thing to say wrongly than to not say at all.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from datetime import date
from typing import Protocol

from loreline import capabilities
from loreline.capability_config import CapabilityConfig
from loreline.catalog import CatalogProbe, ClientFactory, probe
from loreline.logging import get_logger
from loreline.models import Interaction, ProviderConfig, ProviderKind
from loreline.staleness.deprecation import (
    FAIL_HORIZON_DAYS,
    WARN_HORIZON_DAYS,
    model_date_findings,
)
from loreline.staleness.report import Code, Finding, Severity

log = get_logger(__name__)

# Shorter than the CI check's budget: a boot-time courtesy has no business
# holding a connection open for twenty seconds per vendor.
STARTUP_TIMEOUT_S = 8.0
# Whole-check ceiling across every vendor, so a pathological transport that
# ignores its own timeout still cannot keep this task alive indefinitely.
STARTUP_BUDGET_S = 30.0


class KeyStore(Protocol):
    """The slice of :class:`loreline.secrets.SecretStore` this needs."""

    def get(self, name: str) -> str | None: ...


def _api_key(provider: ProviderConfig, secrets: KeyStore) -> str | None:
    return secrets.get(provider.auth_ref) if provider.auth_ref else None


async def stale_favorites(
    providers: Sequence[ProviderConfig],
    *,
    api_key_for: Callable[[ProviderConfig], str | None],
    config: CapabilityConfig | None = None,
    today: date | None = None,
    warn_days: int = WARN_HORIZON_DAYS,
    fail_days: int = FAIL_HORIZON_DAYS,
    request_timeout: float = STARTUP_TIMEOUT_S,
    client_factory: ClientFactory | None = None,
) -> list[Finding]:
    """Favourite models that are retiring, retired, or no longer listed."""
    cfg = config or capabilities.config()
    now = today or date.today()
    findings: list[Finding] = []
    # One fetch per catalogue, however many provider rows share it: two
    # OpenRouter rows with different keys still list the same models.
    cache: dict[tuple[ProviderKind, str | None, Interaction], CatalogProbe] = {}
    for provider in providers:
        if not provider.enabled or not provider.favorite_models:
            continue
        spec = cfg.provider(provider.kind)
        if spec is None:
            continue
        for favorite in provider.favorite_models:
            curated = next((m for m in spec.models if m.id == favorite), None)
            if curated is not None:
                findings.extend(
                    model_date_findings(
                        provider.kind,
                        curated,
                        now=now,
                        warn_days=warn_days,
                        fail_days=fail_days,
                    )
                )
        probes: list[CatalogProbe] = []
        for interaction in spec.interactions:
            key = (provider.kind, provider.base_url, interaction)
            if key not in cache:
                cache[key] = await probe(
                    provider.kind,
                    interaction,
                    spec=spec,
                    api_key=api_key_for(provider),
                    base_url=provider.base_url,
                    client_factory=client_factory,
                    request_timeout=request_timeout,
                )
            probes.append(cache[key])
        findings.extend(_missing_favorites(provider, probes))
    return findings


def _missing_favorites(provider: ProviderConfig, probes: Sequence[CatalogProbe]) -> list[Finding]:
    """Favourites absent from a set of catalogues that all answered fully.

    The all-or-nothing rule is what keeps a partial answer from producing a
    false alarm. A kind that serves several interactions has several
    catalogues, and a favourite only has to appear in one of them - a video
    model is not in the chat list - so a single unreachable endpoint makes the
    whole question unanswerable for that provider.
    """
    if not probes or any(not p.usable or p.partial for p in probes):
        return []
    return [
        Finding(
            severity=Severity.ERROR,
            code=Code.MODEL_RETIRED,
            kind=provider.kind,
            model=favorite,
            curated="favorite",
            vendor="absent",
            message=(
                f"favorite model of provider {provider.name!r} is no longer "
                "listed by the vendor: pick a replacement in Settings > Providers"
            ),
        )
        for favorite in provider.favorite_models
        if not any(p.lists(favorite) for p in probes)
    ]


async def warn_about_stale_favorites(
    providers: Sequence[ProviderConfig],
    *,
    secrets: KeyStore,
    config: CapabilityConfig | None = None,
    today: date | None = None,
    warn_days: int = WARN_HORIZON_DAYS,
    fail_days: int = FAIL_HORIZON_DAYS,
    client_factory: ClientFactory | None = None,
    budget: float = STARTUP_BUDGET_S,
) -> list[Finding]:
    """Log a warning per stale favourite. Never raises, whatever happens.

    Returns the findings as well so a test can assert on them, but the caller
    on the startup path ignores the value: the log line, which the UI's live
    log panel shows, is the whole point.
    """
    try:
        async with asyncio.timeout(budget):
            findings = await stale_favorites(
                providers,
                api_key_for=lambda p: _api_key(p, secrets),
                config=config,
                today=today,
                warn_days=warn_days,
                fail_days=fail_days,
                client_factory=client_factory,
            )
    except asyncio.CancelledError:
        # Shutdown, or the budget running out. Both mean "stop", not "fail".
        raise
    except Exception as exc:
        # There is no failure mode of a courtesy check worth a traceback on the
        # startup path, including a bug in the check itself.
        log.warning("staleness.favorites.failed", error=f"{type(exc).__name__}: {exc}")
        return []
    for finding in findings:
        emit = log.error if finding.severity is Severity.ERROR else log.warning
        emit(
            "staleness.favorite",
            code=finding.code.value,
            provider=finding.kind.value,
            model=finding.model,
            detail=finding.message,
        )
    return findings
