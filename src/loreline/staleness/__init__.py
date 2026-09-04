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

The write half is :mod:`loreline.staleness.sync`, run by ``loreline
sync-capabilities``. It is the only thing here that may change the file, and it
changes only the fields a vendor catalogue genuinely publishes, for models that
are already curated - never a hand annotation, and never the curation itself.

Neither check may ever block startup, raise, or fail CI because a vendor was
unreachable. Silence and a labelled "could not check" are the only two
acceptable outcomes of a failed lookup, and both checks are built so that
absence of evidence is never reported as evidence of absence.

The halves are separate on purpose. What the vendor says comes from
:mod:`loreline.catalog`, the one catalogue reader this package consumes but
does not own (the pickers and the video dialog read the same probe);
:mod:`compare` diffs that against the config, :mod:`report` renders it, and
:mod:`sync` reuses the same fetch and the same definition of drift, so the
checker and the regenerator can never disagree about whether a field is stale.
"""

from loreline.staleness.check import FailOn, run_check, should_fail, summarize
from loreline.staleness.compare import NOT_CHECKED_NOTE
from loreline.staleness.report import Finding, Severity, StalenessReport, render
from loreline.staleness.startup import stale_favorites, warn_about_stale_favorites
from loreline.staleness.sync import (
    NEVER_WRITTEN_NOTE,
    SyncPlan,
    SyncRefusedError,
    run_sync,
)
from loreline.staleness.sync import render as render_sync
from loreline.staleness.sync import write as write_sync

__all__ = [
    "NEVER_WRITTEN_NOTE",
    "NOT_CHECKED_NOTE",
    "FailOn",
    "Finding",
    "Severity",
    "StalenessReport",
    "SyncPlan",
    "SyncRefusedError",
    "render",
    "render_sync",
    "run_check",
    "run_sync",
    "should_fail",
    "stale_favorites",
    "summarize",
    "warn_about_stale_favorites",
    "write_sync",
]
