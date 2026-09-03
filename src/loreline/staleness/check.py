"""Run the whole staleness check and hand back one report.

This is the CI-facing entry point (``loreline check-capabilities`` calls it).
It wires the three halves together and adds the one piece of policy neither of
them should own: what counts as a failure.

Failure policy, and why it is shaped like this:

* The offline half - a recorded sunset date that has passed while the model is
  still offered - is the only check that fails a build by default. It cannot
  produce a false positive, because it asks nobody anything.
* An unreachable, rate limiting or credential-less vendor never fails
  anything. It is reported as "not checked" and the exit code ignores it, so a
  vendor outage cannot turn a green pipeline red.
* Everything a vendor did answer is reported at WARNING or INFO and does not
  fail by default. ``--fail-on warning`` is there for a repo that wants the
  stricter gate on a scheduled run.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import date
from enum import StrEnum

from loreline import capabilities
from loreline.capability_config import CapabilityConfig
from loreline.models import Interaction, ProviderKind
from loreline.staleness.catalog import (
    DEFAULT_TIMEOUT_S,
    CatalogProbe,
    CatalogStatus,
    ClientFactory,
    credential_from_env,
    endpoint_for,
    probe,
)
from loreline.staleness.compare import compare
from loreline.staleness.deprecation import FAIL_HORIZON_DAYS, WARN_HORIZON_DAYS, check_dates
from loreline.staleness.report import Finding, Severity, StalenessReport, order


class FailOn(StrEnum):
    """Lowest severity that should make the check exit non-zero."""

    ERROR = "error"
    WARNING = "warning"
    INFO = "info"
    NEVER = "never"


def should_fail(report: StalenessReport, threshold: FailOn) -> bool:
    if threshold is FailOn.NEVER:
        return False
    return bool(report.at_least(Severity(threshold.value)))


async def run_check(
    *,
    config: CapabilityConfig | None = None,
    offline: bool = False,
    today: date | None = None,
    warn_days: int = WARN_HORIZON_DAYS,
    fail_days: int = FAIL_HORIZON_DAYS,
    request_timeout: float = DEFAULT_TIMEOUT_S,
    credentials: Mapping[ProviderKind, str] | None = None,
    client_factory: ClientFactory | None = None,
) -> StalenessReport:
    """Check the curated file against the calendar and against the vendors.

    ``offline=True`` runs the date half only, which needs no network and no
    credentials. Otherwise every vendor that publishes a catalogue is asked,
    with whatever key the environment supplies; the ones that need a key and
    have none are recorded as not checked rather than skipped silently.
    """
    cfg = config or capabilities.config()
    # Local and exact, and kept first so a date failure stands on its own
    # whatever the vendors do or do not say next.
    findings: list[Finding] = check_dates(
        cfg, today=today, warn_days=warn_days, fail_days=fail_days
    )
    probes: tuple[CatalogProbe, ...] = ()
    if not offline:
        probes = await gather_probes(
            cfg,
            request_timeout=request_timeout,
            credentials=credentials,
            client_factory=client_factory,
        )
        findings.extend(compare(cfg, probes, today=today, horizon_days=warn_days))
    return StalenessReport(order(findings), probes)


def _pairs(config: CapabilityConfig) -> list[tuple[ProviderKind, Interaction]]:
    """Every provider and interaction to ask about, in a stable order."""
    return [
        (kind, interaction)
        for kind in sorted(config.providers, key=lambda k: k.value)
        for interaction in sorted(config.providers[kind].interactions, key=lambda i: i.value)
    ]


async def gather_probes(
    config: CapabilityConfig,
    *,
    request_timeout: float = DEFAULT_TIMEOUT_S,
    credentials: Mapping[ProviderKind, str] | None = None,
    client_factory: ClientFactory | None = None,
) -> tuple[CatalogProbe, ...]:
    """Ask every vendor once per distinct catalogue URL.

    One URL often serves several interactions (OpenAI's ``/v1/models`` is the
    catalogue for both transcription and summarization), and fetching it twice
    would double the load on a vendor for an identical answer. The fetch is
    shared and the result is re-labelled per interaction.
    """
    keys = _pairs(config)
    # Group by the address actually being called, preserving first-seen order.
    groups: dict[tuple[ProviderKind, str | None], list[Interaction]] = {}
    for kind, interaction in keys:
        spec = config.providers[kind]
        url = endpoint_for(spec, interaction)
        groups.setdefault((kind, url), []).append(interaction)

    async def run(kind: ProviderKind, interactions: list[Interaction]) -> list[CatalogProbe]:
        spec = config.providers[kind]
        api_key = (credentials or {}).get(kind) or credential_from_env(spec)
        first = await probe(
            kind,
            interactions[0],
            spec=spec,
            api_key=api_key,
            client_factory=client_factory,
            request_timeout=request_timeout,
        )
        return [first, *(replace(first, interaction=i) for i in interactions[1:])]

    results = await asyncio.gather(
        *(run(kind, interactions) for (kind, _url), interactions in groups.items()),
        return_exceptions=True,
    )
    probes: list[CatalogProbe] = []
    for ((kind, url), interactions), result in zip(groups.items(), results, strict=True):
        if isinstance(result, BaseException):
            # probe() is written not to raise, so this is the guard against a
            # bug in it rather than against a vendor. Either way the check
            # reports "not checked" instead of dying.
            probes.extend(
                CatalogProbe(
                    kind,
                    interaction,
                    url,
                    CatalogStatus.UNREACHABLE,
                    f"could not check: {type(result).__name__}: {result}",
                )
                for interaction in interactions
            )
        else:
            probes.extend(result)
    return tuple(probes)


def summarize(report: StalenessReport) -> str:
    """One line for a CI job summary."""
    counts: list[str] = []
    for severity in (Severity.ERROR, Severity.WARNING, Severity.INFO):
        matching = [f for f in report.findings if f.severity is severity]
        if matching:
            counts.append(f"{len(matching)} {severity.value}")
    unchecked = len(report.unchecked())
    found = ", ".join(counts) if counts else "no drift"
    return f"{found}; {unchecked} catalogue(s) not checked"


__all__: Sequence[str] = [
    "FailOn",
    "gather_probes",
    "run_check",
    "should_fail",
    "summarize",
]
