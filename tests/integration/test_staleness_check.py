"""The CI gate end to end: every vendor asked once, silence never read as absence.

The reader's own behaviour (what a body becomes, what a failure becomes) is
pinned in test_catalog_reader.py and the comparison arithmetic in
tests/unit/test_staleness.py. What is pinned here is the wiring between them:
the gate fetches one URL once however many interactions it serves, an
unusable probe yields no finding, and the offline run asks nobody.
"""

from __future__ import annotations

import httpx
from test_catalog_reader import OPENAI_BODY, serving

from loreline.capabilities import config
from loreline.catalog import CatalogStatus, probe
from loreline.models import Interaction, ProviderKind
from loreline.staleness.check import gather_probes, run_check
from loreline.staleness.compare import compare


def _factory(handler: httpx.MockTransport) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=handler)


async def test_one_url_serving_two_interactions_is_fetched_once() -> None:
    """OpenAI's /v1/models is the catalogue for transcription and for
    summarization alike. Asking twice would double the load on a vendor for an
    identical answer."""
    calls: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, json=OPENAI_BODY)

    probes = await gather_probes(
        config(),
        credentials={ProviderKind.OPENAI: "k"},
        client_factory=lambda: _factory(httpx.MockTransport(handle)),
    )
    openai = [p for p in probes if p.kind is ProviderKind.OPENAI]
    assert {p.interaction for p in openai} == {Interaction.TRANSCRIBE, Interaction.SUMMARIZE}
    assert all(p.status is CatalogStatus.OK for p in openai)
    assert len([c for c in calls if "api.openai.com" in c]) == 1


async def test_an_unusable_probe_produces_no_finding() -> None:
    """An empty catalogue is the most dangerous answer there is: read as
    truth it would report every curated model as retired at once. The reader
    marks it unreadable, and the comparison must honour that."""
    result = await probe(
        ProviderKind.OPENROUTER,
        Interaction.SUMMARIZE,
        client_factory=lambda: _factory(serving({"data": []})),
    )
    assert result.status is CatalogStatus.UNREADABLE
    assert compare(config(), [result]) == []


async def test_a_run_where_every_vendor_is_down_reports_no_drift() -> None:
    """The requirement, end to end: an offline CI machine produces a report
    full of "not checked" and not one claim about a model."""

    def handle(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    report = await run_check(
        credentials=dict.fromkeys(ProviderKind, "k"),
        client_factory=lambda: _factory(httpx.MockTransport(handle)),
    )
    assert report.probes
    assert all(not p.usable for p in report.probes)
    assert not [f for f in report.findings if f.code.value.startswith("model.")]


async def test_the_offline_run_asks_nobody() -> None:
    report = await run_check(offline=True)
    assert report.probes == ()
    # Whatever it finds, it can only have come from the file's own dates.
    assert all(f.fact == "deprecated" for f in report.findings)
