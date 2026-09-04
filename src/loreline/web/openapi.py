"""The API's OpenAPI document, built without running the server.

The frontend's wire types are generated from this (see ``frontend/openapi.json``
and ``npm run gen:api``), so it has to be producible from a checkout alone: no
listening port, no database, no credentials. ``create_app`` only wires routers
and logging - every stateful thing it owns is built inside its lifespan - so
the document is just the app object's own description of itself.

The settings handed in are a throwaway: a temporary data directory and blank
credentials, so a dump can never touch the real one and comes out identical on
every machine. Nothing in the document depends on them.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any


def openapi_document() -> dict[str, Any]:
    """Return the OpenAPI document as FastAPI generates it."""
    from loreline.settings import Settings  # noqa: PLC0415
    from loreline.web.app import create_app  # noqa: PLC0415

    with tempfile.TemporaryDirectory(prefix="loreline-openapi-") as tmp:
        app = create_app(
            Settings(data_dir=Path(tmp) / "data", auth_password="", jwt_secret="openapi-dump")
        )
        return app.openapi()


def openapi_json() -> str:
    """The document as the JSON that is committed, newline-terminated."""
    return json.dumps(openapi_document(), indent=2) + "\n"
