"""What holds the version to one source, now that it is derived rather than typed.

Loreline states its version in three places somebody reads: the web header,
which gets it from /api/system/healthz, the `loreline version` command, and the
`loreline.startup` log line. All three read ``loreline.__version__``, which
comes from the installed distribution's metadata and therefore from
pyproject.toml, the file the release tooling bumps. src/loreline/__init__.py
carries the argument for doing it that way.

These tests exist because the failure they catch is a quiet one. A stale
version constant breaks nothing: it just answers with an old number, and the
only person who finds out is whoever compares the header against the revision
under Settings > Client, which is what happened with v0.2.0. Nothing about that
class of bug makes a test suite fail on its own, so it is asserted here
deliberately.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path
from typing import Any

import pytest

import loreline
from loreline.web.app import OPENAPI_DOCUMENT_VERSION

REPO_ROOT = Path(__file__).resolve().parents[2]
PYPROJECT = REPO_ROOT / "pyproject.toml"
OPENAPI_DOCUMENT = REPO_ROOT / "frontend" / "openapi.json"

# Both files are skipped rather than assumed, because the suite is also
# runnable against an installed Loreline with no checkout around it, and a
# missing source tree is not a failing version.
needs_checkout = pytest.mark.skipif(
    not PYPROJECT.exists() or not OPENAPI_DOCUMENT.exists(),
    reason="needs a source checkout, not just an installed distribution",
)


@needs_checkout
def test_the_reported_version_is_the_declared_one() -> None:
    """``__version__`` agrees with pyproject.toml, the version of record.

    A failure here is one of two things. Either somebody wrote a literal back
    into src/loreline/__init__.py, in which case the two numbers will disagree
    the moment the next release bumps one of them, or the environment holds an
    install from before a bump, which `uv sync` fixes. `uv run pytest` re-syncs
    first, so in the ordinary way of running the suite only the first is left.
    """
    pyproject: dict[str, Any] = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    declared: str = pyproject["project"]["version"]
    assert loreline.__version__ == declared, (
        f"loreline.__version__ is {loreline.__version__!r} but pyproject.toml declares "
        f"{declared!r}; if these differ by a release, re-run `uv sync`"
    )


@needs_checkout
def test_the_api_document_does_not_carry_the_release_version() -> None:
    """The committed OpenAPI document declares the API's version, not the build's.

    Checked against the constant rather than against a spelling, so that
    bumping the wire contract is one edit in one place. What must not happen is
    ``__version__`` finding its way back in: the document is generated and
    committed, and pre-commit and CI both diff it against a fresh dump, so a
    version that moves every release would fail those checks on the first push
    after every release. src/loreline/web/app.py has the reasoning.
    """
    document: dict[str, Any] = json.loads(OPENAPI_DOCUMENT.read_text(encoding="utf-8"))
    assert document["info"]["version"] == OPENAPI_DOCUMENT_VERSION
    assert document["info"]["version"] != loreline.__version__
