"""One rule for who closes an ``httpx.AsyncClient``.

Every HTTP-speaking component here takes an optional client so a test can hand
in one built on ``httpx.MockTransport``. When none is handed in the component
builds its own and is the only thing that knows about it, so it must close it;
when one is handed in the caller keeps the lifetime (the test's ``async with``
block, a shared pool) and closing it from inside would pull the rug out from
under everyone else using it. Six components restated that rule by hand, two
lines in ``__init__`` and two in ``aclose``, and a copy is where such a rule
drifts. This is the one copy.
"""

from __future__ import annotations

import httpx


class ClientHandle:
    """The client a component talks through, and whether closing it is its job.

    ``client`` is the injected one when there is one, else a fresh
    ``httpx.AsyncClient`` built from the keyword arguments (``base_url``,
    ``headers``, ``timeout`` and whatever else the constructor takes). Only a
    client built here is closed by :meth:`aclose`; an injected one belongs to
    whoever injected it.
    """

    def __init__(
        self,
        client: httpx.AsyncClient | None,
        *,
        base_url: str,
        headers: dict[str, str] | None = None,
        timeout: float,
    ) -> None:
        self.owned = client is None
        self.client = client or httpx.AsyncClient(
            base_url=base_url, headers=headers or {}, timeout=timeout
        )

    async def aclose(self) -> None:
        """Close the client, but only if it was built here."""
        if self.owned:
            await self.client.aclose()
