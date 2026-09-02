"""What a staleness check found, and how it reads on a terminal.

Kept apart from both the probes and the comparison so the three halves stay
independently reusable: the planned sync script wants the probes without any of
this, and a future UI panel would want the findings without the text renderer.

A finding is never a fix. Nothing here writes to capabilities.yaml; the whole
output is a list of statements about a difference, each carrying what the file
says and what the vendor says so a human can decide which one is wrong.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum

from loreline.models import Interaction, ProviderKind
from loreline.staleness.catalog import CatalogProbe, CatalogStatus


class Severity(StrEnum):
    """How much a finding should cost.

    ERROR is reserved for things that are wrong *today* and were caused by us:
    a model we offer that the vendor no longer lists, a sunset date that has
    already passed. WARNING is a difference that will bite later or that needs
    a human to adjudicate. INFO is a candidate for review, never a failure.
    """

    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


_RANK: dict[Severity, int] = {Severity.INFO: 0, Severity.WARNING: 1, Severity.ERROR: 2}


class Code(StrEnum):
    """Stable identifiers for each kind of finding, for filtering and grepping."""

    MODEL_RETIRED = "model.retired"
    MODEL_UNCURATED = "model.uncurated"
    FACT_MISMATCH = "fact.mismatch"
    DEPRECATION_PASSED = "deprecation.passed"
    DEPRECATION_NEAR = "deprecation.near"
    DEPRECATION_UNPARSEABLE = "deprecation.unparseable"
    DEPRECATION_UNRECORDED = "deprecation.unrecorded"
    DEPRECATION_MISMATCH = "deprecation.mismatch"


@dataclass(frozen=True, slots=True)
class Finding:
    """One difference between the curated file and the world."""

    severity: Severity
    code: Code
    kind: ProviderKind
    message: str
    interaction: Interaction | None = None
    model: str | None = None
    # The yaml field this is about, dotted as it is written there
    # ("llm.context_length", "video.durations"). None for whole-model findings.
    fact: str | None = None
    curated: str | None = None
    vendor: str | None = None

    def line(self) -> str:
        where = self.kind.value
        if self.interaction is not None:
            where = f"{where}/{self.interaction.value}"
        if self.model:
            where = f"{where} {self.model}"
        return f"{self.severity.value.upper():<7} {where}: {self.message}"

    def as_dict(self) -> dict[str, str | None]:
        return {
            "severity": self.severity.value,
            "code": self.code.value,
            "provider": self.kind.value,
            "interaction": self.interaction.value if self.interaction else None,
            "model": self.model,
            "fact": self.fact,
            "curated": self.curated,
            "vendor": self.vendor,
            "message": self.message,
        }


@dataclass(frozen=True, slots=True)
class StalenessReport:
    """Findings plus the provenance of every answer they are based on.

    The probes are part of the report, not debris from producing it. "No
    findings" means something completely different depending on whether a
    vendor was checked and agreed or was never reached at all, and a report
    that did not say which would be the false clean bill of health this whole
    feature exists to avoid.
    """

    findings: tuple[Finding, ...] = ()
    probes: tuple[CatalogProbe, ...] = ()

    def worst(self) -> Severity | None:
        return max((f.severity for f in self.findings), key=lambda s: _RANK[s], default=None)

    def at_least(self, severity: Severity) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if _RANK[f.severity] >= _RANK[severity])

    def unchecked(self) -> tuple[CatalogProbe, ...]:
        """Probes that produced no usable answer, in report order."""
        return tuple(p for p in self.probes if not p.usable)

    def as_dict(self) -> dict[str, object]:
        return {
            "findings": [f.as_dict() for f in self.findings],
            "probes": [
                {
                    "provider": p.kind.value,
                    "interaction": p.interaction.value,
                    "endpoint": p.endpoint,
                    "status": p.status.value,
                    "detail": p.detail,
                    "models": len(p.models),
                    "partial": p.partial,
                }
                for p in self.probes
            ],
        }


@dataclass(slots=True)
class _Section:
    title: str
    lines: list[str] = field(default_factory=list[str])


def _severity_sections(findings: Sequence[Finding]) -> list[_Section]:
    sections: list[_Section] = []
    for severity in (Severity.ERROR, Severity.WARNING, Severity.INFO):
        matching = [f for f in findings if f.severity is severity]
        if matching:
            sections.append(
                _Section(f"{severity.value}s ({len(matching)})", [f.line() for f in matching])
            )
    return sections


def render(report: StalenessReport) -> str:
    """The human-readable report, worst first.

    Deliberately plain text: this runs in CI logs and in a terminal, where
    colour codes and boxes are noise. The "not checked" block is always
    printed, even when empty of surprises, because a reader has to be able to
    tell "the vendor agrees" from "we never asked".
    """
    sections = _severity_sections(report.findings)
    checked = [p for p in report.probes if p.usable]
    lines: list[str] = []
    for section in sections:
        lines.append(f"== {section.title}")
        lines.extend(f"   {line}" for line in section.lines)
        lines.append("")
    if not report.probes:
        # The offline run. Saying "0 of 0 checked" would read as a failure
        # rather than as the deliberate no-network mode it is.
        lines.append("== checked (offline: no vendor was asked)")
    else:
        lines.append(f"== checked ({len(checked)} of {len(report.probes)} catalogues)")
    for probe in report.probes:
        marker = "ok  " if probe.usable else "skip"
        where = f"{probe.kind.value}/{probe.interaction.value}"
        lines.append(f"   {marker} {where}: {probe.detail}")
    if not report.findings:
        lines.append("")
        # The healthy state, and it has to read as one. An offline run has
        # nothing to say about vendors, so it says the one thing it did check.
        lines.append(
            "no recorded sunset date is due or imminent"
            if not report.probes
            else "no drift found in what was checked"
        )
    return "\n".join(lines)


def order(findings: Iterable[Finding]) -> tuple[Finding, ...]:
    """Findings sorted worst first, then by provider, model and code.

    Stable ordering matters more than it looks: this output is diffed between
    CI runs to see what changed, and a set-iteration order would make every run
    look different.
    """
    return tuple(
        sorted(
            findings,
            key=lambda f: (
                -_RANK[f.severity],
                f.kind.value,
                f.interaction.value if f.interaction else "",
                f.model or "",
                f.code.value,
                f.fact or "",
            ),
        )
    )


__all__ = [
    "CatalogProbe",
    "CatalogStatus",
    "Code",
    "Finding",
    "Severity",
    "StalenessReport",
    "order",
    "render",
]
