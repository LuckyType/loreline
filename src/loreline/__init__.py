"""Loreline - headless tabletop session transcriber."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

# Derived, never written down, and the urge to paste a literal back in here is
# exactly what this comment exists to head off.
#
# The version of record is `[project].version` in pyproject.toml. That is what
# the release tooling bumps (see .versionrc.cjs), what uv.lock has to agree
# with before `uv sync --frozen` will run, and what the build backend copies
# into the installed distribution's metadata. A second copy in this file would
# be a copy no tooling touches, and it drifted precisely that way once: the
# v0.2.0 image reported `v0.2.0` under Settings > Client, from the
# `git describe` baked in at image build, while the header beside the app name
# still read 0.1.0 off a hardcoded constant here. One build, two answers, and
# the wrong one was the one users could see.
#
# The lookup asks the installed distribution rather than reading pyproject.toml
# off disk, because pyproject.toml is not reliably on disk where this matters,
# and because "what is actually installed" is the honest answer to the question
# anyway. Both deployments do install the package: the Dockerfile runs
# `uv sync --frozen --no-dev --extra audio --extra providers` after copying the
# source in, and a dev checkout gets the same command's editable install. Each
# writes a real dist-info, so each answers here, and `uv run` re-syncs when
# pyproject.toml's version moves, so a bumped checkout reports the new number
# without anyone thinking about it.
try:
    __version__ = version("loreline")
except PackageNotFoundError:  # pragma: no cover - only when nothing installed it
    # Deliberately not version-shaped. This string is rendered in the web
    # header and served from /api/system/healthz, so something like "0.0.0"
    # would read as a real release and be believed. A plausible wrong answer is
    # worse than an obviously absent one; that is the whole lesson above, and
    # the only way to reach this branch is importing the source tree off
    # sys.path without installing it.
    __version__ = "unknown"

__all__ = ["__version__"]
