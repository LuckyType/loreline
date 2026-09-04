"""One question per provider row: does this key work at this surface?

Answered as a :class:`~loreline.health.HealthReport` by :func:`probe_provider`,
and never by building a connector. The Test button used to do exactly that:
for a kind that transcribes it asked the registry for a whole transcription
connector, which made the registry resolve a model nobody had chosen, and then
called that connector's own ``health`` method, one of eight copies of the same
idea graded eight slightly different ways. The question has nothing to do with
a model. It is answered by the surface capabilities.yaml declares for the kind
(ADR 0002) and the credential alone, and the grading is :mod:`loreline.health`'s,
the same for every row, for the remote diarizer and for the ``/healthz`` badge.

Which surface answers for a kind is :func:`probe_target`; how it is asked
follows the surface's shape:

* an HTTP surface: ``GET`` the ``health`` path it declares, or ``/models``
  when it declares none (every OpenAI-shaped server and Google's native base
  serve one), with the surface's auth scheme, graded by
  :func:`loreline.health.classify_response`;
* a socket surface: open it with the surface's auth (a header, or the key in
  the query string for Gemini Live), send the first frame it declares if any,
  and grade the handshake and the first reply
  (:func:`loreline.stt.backends._ws.probe_socket`).

A different question from the catalogue probe (:mod:`loreline.catalog`),
which asks a vendor what it lists; this asks whether one row can be used at
all, and answers without touching the network when it already knows
(:func:`loreline.health.missing_credential`).
"""

from __future__ import annotations

import json
from collections.abc import Callable

import httpx

from loreline.capabilities import default_model, is_realtime_model, supports, surface, surface_for
from loreline.capability_config import Transport
from loreline.health import (
    PROBE_TIMEOUT_S,
    HealthReport,
    HealthStatus,
    missing_credential,
    probe_endpoint,
)
from loreline.models import Interaction, ProviderConfig, ProviderKind
from loreline.stt.backends._ws import probe_socket

ClientFactory = Callable[[], httpx.AsyncClient]

# What an HTTP surface is asked when it declares no health path: free,
# exercises the key, and implemented by every OpenAI-compatible server and by
# Google's native base alike. A surface whose list is public (OpenRouter,
# Deepgram) or absent (AssemblyAI) declares its own question in the yaml.
DEFAULT_HEALTH_PATH = "/models"


def probe_target(kind: ProviderKind) -> tuple[Interaction, Transport | None] | None:
    """The one surface that answers for a kind, or None for a kind with none.

    A kind that summarizes is asked on its chat surface, whatever else it
    does. One probe per row: the key is the same credential on every surface
    of that row, so for a kind that both summarizes and transcribes (Gemini,
    OpenAI, OpenRouter, the self-hosted kind) a second probe against the
    transcription surface would cost a round trip to learn nothing, and for
    Gemini the two are sibling URLs with different auth headers, so it would
    have to build a second client to ask. This is the choice the settings
    route made before the probe moved here, kept on purpose.

    Everything else is asked on the transcription surface its default model
    runs on: the socket for Deepgram and AssemblyAI, whose house transport is
    streaming. A kind that only generates video is asked there.
    """
    if supports(kind, Interaction.SUMMARIZE):
        return Interaction.SUMMARIZE, None
    if supports(kind, Interaction.TRANSCRIBE):
        return Interaction.TRANSCRIBE, _house_transport(kind)
    if supports(kind, Interaction.VIDEO):
        return Interaction.VIDEO, None
    return None


def _house_transport(kind: ProviderKind) -> Transport:
    """The transport the kind's default transcription model runs on.

    Falls back to whichever transcription surface the kind does declare, so a
    kind that streams only or posts only is asked where it can answer.
    """
    streams = is_realtime_model(kind, default_model(kind, Interaction.TRANSCRIBE))
    preferred: Transport = "realtime" if streams else "batch"
    if surface(kind, Interaction.TRANSCRIBE, preferred) is not None:
        return preferred
    return "batch" if streams else "realtime"


async def probe_provider(
    config: ProviderConfig,
    api_key: str | None,
    *,
    client_factory: ClientFactory | None = None,
) -> HealthReport:
    """Whether this provider row works: its key at its declared surface.

    The entry point behind ``POST /providers/{id}/test``. Never raises: every
    outcome, a rejected key, a dead host, a row this app cannot locate a
    surface for, is a graded report, because the button's job is to render a
    state and an exception would render "down" with no explanation attached.
    ``client_factory`` lets a test hand in an ``httpx.MockTransport`` client
    for an HTTP surface; socket surfaces are reached through the row's own
    ``base_url``, which is how the mock servers are pointed at.
    """
    # Answered without a network call, and not merely as an optimisation: see
    # missing_credential on the keyless 404 that would otherwise read as a bad
    # base URL.
    report = missing_credential(config.kind, api_key)
    if report is not None:
        return report
    target = probe_target(config.kind)
    if target is None:
        # Nothing was probed, so nothing is known: a kind the yaml declares no
        # surface for cannot be down, only unconfigured.
        return HealthReport(
            HealthStatus.UNKNOWN, f"{config.kind.value} declares no surface to probe"
        )
    interaction, transport = target
    return await probe_surface(
        config, api_key, interaction, transport, client_factory=client_factory
    )


async def probe_surface(
    config: ProviderConfig,
    api_key: str | None,
    interaction: Interaction,
    transport: Transport | None = None,
    *,
    client_factory: ClientFactory | None = None,
) -> HealthReport:
    """Ask one declared surface of this row its health question. Never raises.

    The rung :func:`probe_provider` stands on, public so the question can be
    put to any surface a kind declares (a test pinning what a vendor's batch
    surface is asked, say) without going through the per-kind choice.
    """
    try:
        endpoint = surface_for(config, interaction, transport)
    except ValueError as exc:
        # Nothing was probed, and the message names the missing piece (a
        # self-hosted row with no base URL), which is what the GM has to fix.
        return HealthReport(HealthStatus.UNKNOWN, str(exc))
    probe = endpoint.health
    if endpoint.surface.socket:
        frame = json.dumps(probe.frame) if probe is not None and probe.frame is not None else None
        return await probe_socket(
            endpoint.url_with_key(api_key), endpoint.request_headers(api_key), frame
        )
    if client_factory is not None:
        client = client_factory()
    else:
        client = httpx.AsyncClient(
            base_url=endpoint.url,
            headers=endpoint.request_headers(api_key),
            timeout=PROBE_TIMEOUT_S,
        )
    path = probe.path if probe is not None and probe.path else DEFAULT_HEALTH_PATH
    params: dict[str, str | int] | None = dict(probe.params) if probe and probe.params else None
    try:
        return await probe_endpoint(client, path, params=params)
    finally:
        await client.aclose()
