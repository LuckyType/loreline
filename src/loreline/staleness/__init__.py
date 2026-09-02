"""Is capabilities.yaml still true? Two checks, both fail soft.

capabilities.yaml is hand curated, so it goes stale the moment a vendor retires
a model or ships a generation. Neither check here can fix that (they never
write the file); they report, and a human decides which side is wrong.

* :mod:`loreline.staleness.check` is the CI-facing one, run by
  ``loreline check-capabilities``. It asks every vendor that publishes a
  catalogue what it lists today, compares the derivable facts, and reports
  recorded sunset dates that have passed or are close - that last part needing
  no network at all.
* :mod:`loreline.staleness.startup` is the boot-time one, scoped to the models
  a GM actually favourited, because a warning about the whole catalogue at
  every startup is noise nobody reads.

Neither may ever block startup, raise, or fail CI because a vendor was
unreachable. Silence and a labelled "could not check" are the only two
acceptable outcomes of a failed lookup, and both checks are built so that
absence of evidence is never reported as evidence of absence.

The three halves are separate on purpose - :mod:`catalog` works out what the
vendor says, :mod:`compare` diffs that against the config, :mod:`report`
renders it - so the planned sync script, which will regenerate the derivable
fields rather than have them hand edited, can reuse the first half as is.
"""

from loreline.staleness.check import FailOn, run_check, should_fail, summarize
from loreline.staleness.compare import NOT_CHECKED_NOTE
from loreline.staleness.report import Finding, Severity, StalenessReport, render
from loreline.staleness.startup import stale_favorites, warn_about_stale_favorites

__all__ = [
    "NOT_CHECKED_NOTE",
    "FailOn",
    "Finding",
    "Severity",
    "StalenessReport",
    "render",
    "run_check",
    "should_fail",
    "stale_favorites",
    "summarize",
    "warn_about_stale_favorites",
]
