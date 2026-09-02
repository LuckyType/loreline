"""The capability config, served to the browser.

The pickers need the same answers the backend has: which providers can serve
which interaction, which models a vendor offers, whether a given model
diarizes, takes a glossary, exposes a reasoning effort, or accepts a twelve
second video. Those facts used to be written twice, once in
loreline/capabilities.py and again by hand in TypeScript, and the two had
drifted. This endpoint is what lets the browser stop restating them.

Unauthenticated on purpose: the payload is the capabilities.yaml shipped in the
package, identical for every install, and the login screen itself needs it to
render the first-run provider wizard. It contains no keys, no base URLs an
operator configured, and no session data.
"""

from __future__ import annotations

from fastapi import APIRouter

from loreline.capabilities import config
from loreline.capability_config import CapabilityConfig

router = APIRouter(prefix="/api/capabilities", tags=["capabilities"])


@router.get("", response_model=CapabilityConfig)
async def get_capabilities() -> CapabilityConfig:
    """Everything the UI needs to decide what to show, hide and badge."""
    return config()
