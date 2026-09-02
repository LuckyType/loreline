"""Sunset dates that have passed or are close, checked without a network call.

The half of the staleness feature with teeth, and the reason it can have them:
capabilities.yaml already records the vendor-announced retirement date next to
the model it retires, so this is a comparison between two things we already
have, the file and the calendar. Nothing here talks to a vendor, so nothing
here fails soft. Fail soft is for an API that might be down; a date arithmetic
that "degraded gracefully" would just be a check that does not check.

Two thresholds, and the gap between them is the whole design:

* Inside 30 days: a warning, and CI stays green. A model that still works for
  another month is not an emergency, and a GM mid-campaign should not lose it
  the day we notice the announcement.
* Inside 7 days: CI fails. This is the last responsible moment - the model is
  about to stop answering, and a red pipeline is what forces the replacement
  to be chosen by someone rather than discovered by a player mid-session.
* Already past while still offered: worse than either, so it fails too.

There is deliberately no "this provider will have nothing left" check. It
cannot happen in a file that loads: every interaction a provider offers models
for must mark exactly one of them ``default: true``, and a default may not
carry a sunset date, so at least one undated offered model always survives.
Several models retiring on the same day is therefore a load error long before
it is a staleness finding, and a check for it here would be dead code. The
guard test in tests/unit/test_staleness.py pins that.

The intent is that an entry stays in the file until it is genuinely about to
break, instead of being removed early out of tidiness or, far more likely, not
at all because nobody remembered.
"""

from __future__ import annotations

from datetime import date

from loreline.capability_config import CapabilityConfig, ModelSpec
from loreline.models import ProviderKind
from loreline.staleness.report import Code, Finding, Severity

# Roughly one release cycle here: long enough that the warning arrives before
# the model does, short enough not to be permanent background noise.
WARN_HORIZON_DAYS = 30
# One working week: enough time to pick a successor, edit the yaml and ship it,
# and not enough to keep putting off. Below this the build goes red.
FAIL_HORIZON_DAYS = 7


def check_dates(
    config: CapabilityConfig,
    *,
    today: date | None = None,
    warn_days: int = WARN_HORIZON_DAYS,
    fail_days: int = FAIL_HORIZON_DAYS,
) -> list[Finding]:
    """Every curated model whose recorded sunset has passed or is imminent.

    Exact and deterministic: same file plus same date gives the same answer,
    which is what lets this run as a gating CI step while the vendor checks
    stay advisory.
    """
    now = today or date.today()
    findings: list[Finding] = []
    for kind, spec in config.providers.items():
        for model in spec.models:
            findings.extend(
                model_date_findings(kind, model, now=now, warn_days=warn_days, fail_days=fail_days)
            )
    return findings


def model_date_findings(
    kind: ProviderKind,
    model: ModelSpec,
    *,
    now: date,
    warn_days: int = WARN_HORIZON_DAYS,
    fail_days: int = FAIL_HORIZON_DAYS,
) -> list[Finding]:
    """The date findings for a single curated model.

    Public because the startup check asks the same question about one model at
    a time (the ones a GM has favourited) rather than about the whole file, and
    the two must never disagree about what "close to retirement" means.
    """
    if not model.deprecated:
        return []
    try:
        retires = date.fromisoformat(model.deprecated)
    except ValueError:
        # The schema takes this field as a free string, so a typo lands here
        # rather than at load time, and a date nobody can parse is a date
        # nobody will ever act on. An error even for a hidden model: this one
        # is a mistake in the file rather than a fact about the world.
        return [
            Finding(
                severity=Severity.ERROR,
                code=Code.DEPRECATION_UNPARSEABLE,
                kind=kind,
                model=model.id,
                fact="deprecated",
                curated=model.deprecated,
                message=(
                    f"deprecated: {model.deprecated!r} is not an ISO date "
                    "(expected YYYY-MM-DD), so nothing can act on it"
                ),
            )
        ]
    days = (retires - now).days
    # Hidden models are exempt from the hard failure, deliberately. A hidden
    # entry is described but offered by no picker, so its retirement cannot
    # break anyone's session - and failing a build over one would force churn
    # with no user visible upside. It is still reported, at INFO, because a
    # retired hidden model is dead weight somebody should delete.
    if model.hidden:
        return _hidden_finding(kind, model, retires, days, warn_days)
    if days < fail_days:
        return [_finding(kind, model, retires, days, Severity.ERROR, hard=True)]
    if days < warn_days:
        return [_finding(kind, model, retires, days, Severity.WARNING, hard=False)]
    return []


def _hidden_finding(
    kind: ProviderKind, model: ModelSpec, retires: date, days: int, warn_days: int
) -> list[Finding]:
    if days >= warn_days:
        return []
    return [
        Finding(
            severity=Severity.INFO,
            code=Code.DEPRECATION_PASSED if days < 0 else Code.DEPRECATION_NEAR,
            kind=kind,
            model=model.id,
            fact="deprecated",
            curated=model.deprecated,
            message=(
                f"{_when(retires, days)}, and is hidden: no picker offers it, so nothing "
                "breaks, but the entry is dead weight and can be deleted"
            ),
        )
    ]


def _finding(
    kind: ProviderKind,
    model: ModelSpec,
    retires: date,
    days: int,
    severity: Severity,
    *,
    hard: bool,
) -> Finding:
    """One actionable line.

    Everything a reader of a red build needs is in it: the provider kind and
    model (from the finding's own fields), the date, how many days are left,
    that the model is currently offered, and what to do about it. Nobody should
    have to open the yaml to find out what a failure means.
    """
    action = (
        "remove it or point the entry at its successor; this fails the build until then"
        if hard
        else "choose its successor before it goes"
    )
    return Finding(
        severity=severity,
        code=Code.DEPRECATION_PASSED if days < 0 else Code.DEPRECATION_NEAR,
        kind=kind,
        model=model.id,
        fact="deprecated",
        curated=model.deprecated,
        message=f"{_when(retires, days)}, and is still offered: {action}",
    )


def _when(retires: date, days: int) -> str:
    """ "retires in 5 days (2026-09-24)", or the past tense of it."""
    stamp = retires.isoformat()
    if days < 0:
        return f"retired {-days} days ago ({stamp})"
    if days == 0:
        return f"retires today ({stamp})"
    return f"retires in {days} {'day' if days == 1 else 'days'} ({stamp})"
