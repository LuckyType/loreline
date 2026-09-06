"""Video-generation API client, one HTTP path for every video vendor.

Video generation is asynchronous *at the source*, which is what shapes this
module. Unlike chat completions, there is no request that returns the result:

    POST <submit path>       -> a job handle
    GET  <poll path>         -> a status, until it is terminal
    <fetch the bytes>        -> the encoded video

A generation runs for minutes, so nothing here blocks a request thread; the
polling loop lives in :mod:`loreline.video.jobs`, which owns the job row.

Which path, which field carries the handle, which word means success and where
the finished bytes are all differ per vendor, and are answered by
:mod:`loreline.video.vendors` - one adapter per vendor, exactly as the STT side
has one connector per vendor and transport. What stays here is everything that
does not differ: the client, the two timeouts, and the single way a failed
request becomes a message a GM can act on.

Written against ``httpx`` rather than a vendor SDK for the same reason
:mod:`loreline.llm` is: one HTTP client, one auth path, one set of mockable
transports in the tests. These are a handful of plain REST calls and an SDK
would pin an upper bound on ``pydantic`` for them.
"""

from __future__ import annotations

from collections.abc import Callable
from http import HTTPStatus
from typing import cast

import httpx

from loreline.capabilities import supports, surface_for
from loreline.catalog import VendorModel, VendorVideo, probe
from loreline.logging import get_logger
from loreline.models import Interaction, ProviderConfig, ProviderKind, VideoModelInfo
from loreline.video.vendors import (
    GenerationRequest,
    GenerationState,
    VideoError,
    vendor_for,
)

log = get_logger(__name__)

# A start/poll call is a small JSON round trip; only the content download moves
# real bytes, so it gets its own, much longer budget.
_TIMEOUT_S = 60.0
_DOWNLOAD_TIMEOUT_S = 600.0

ClientFactory = Callable[[], httpx.AsyncClient]

__all__ = [
    "ClientFactory",
    "GenerationRequest",
    "GenerationState",
    "VideoError",
    "build_payload",
    "download_video",
    "list_video_models",
    "poll_generation",
    "start_generation",
    "supports_video",
]


def supports_video(kind: ProviderKind) -> bool:
    """Whether a provider kind can generate video.

    OpenRouter and xAI today: they are the configured providers here that
    expose a video API at all. A plain OpenAI-compatible chat endpoint (Ollama,
    LM Studio, vLLM) has no equivalent, so those must not be offered. Answered
    from the one capability table - see loreline.capabilities - so declaring the
    interaction in capabilities.yaml is what turns a kind on, and
    :func:`loreline.video.vendors.vendor_for` is what it then needs.
    """
    return supports(kind, Interaction.VIDEO)


def _client(
    config: ProviderConfig,
    api_key: str | None,
    factory: ClientFactory | None,
    *,
    timeout: float = _TIMEOUT_S,
) -> httpx.AsyncClient:
    if factory is not None:
        return factory()
    # The kind's video surface: the vendor's base plus whatever headers it
    # declares (OpenRouter's attribution pair), both from capabilities.yaml.
    endpoint = surface_for(config, Interaction.VIDEO)
    return httpx.AsyncClient(
        base_url=endpoint.url, headers=endpoint.request_headers(api_key), timeout=timeout
    )


def _error_detail(response: httpx.Response) -> str:
    """The provider's own message where it has one, else the status line."""
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        error = cast("dict[str, object]", payload).get("error")
        if isinstance(error, dict):
            message = cast("dict[str, object]", error).get("message")
            if isinstance(message, str) and message:
                return message
        elif isinstance(error, str) and error:
            return error
    return f"{response.status_code} {response.reason_phrase}"


def _body(response: httpx.Response) -> dict[str, object]:
    """A JSON object response, or a VideoError naming what arrived instead."""
    try:
        payload = response.json()
    except ValueError as exc:
        msg = "the provider answered with something that is not JSON"
        raise VideoError(msg) from exc
    if not isinstance(payload, dict):
        msg = "unexpected response shape"
        raise VideoError(msg)
    return cast("dict[str, object]", payload)


async def list_video_models(
    *,
    config: ProviderConfig,
    api_key: str | None,
    client_factory: ClientFactory | None = None,
) -> list[VideoModelInfo]:
    """Video models and the parameters each one accepts.

    A projection of the one catalogue reader, :mod:`loreline.catalog`: the
    kind's video ``catalog`` surface, read once, fail soft. An unusable probe
    (no such catalogue, vendor down, shape moved) yields an empty list rather
    than raising, so the dialog can still open and say so instead of erroring
    the page. The lists stay None where the vendor published none, which the
    form reads as "this model takes no such parameter".

    An empty list is also the ordinary answer for a vendor that publishes no
    video catalogue at all (xAI): the dialog then builds its controls from the
    model's ``video`` block in capabilities.yaml, which is its primary source in
    either case - the vendor's own lists are the fallback for a model that file
    does not annotate.
    """
    answer = await probe(
        config.kind,
        Interaction.VIDEO,
        api_key=api_key,
        base_url=config.base_url,
        client_factory=client_factory,
        request_timeout=_TIMEOUT_S,
    )
    if not answer.usable:
        return []
    return sorted((_video_row(m) for m in answer.models), key=lambda m: m.id)


def _video_row(model: VendorModel) -> VideoModelInfo:
    video = model.video or VendorVideo()
    return VideoModelInfo(
        id=model.id,
        name=model.name or model.id,
        description=model.description,
        supported_durations=list(video.durations) if video.durations is not None else None,
        supported_resolutions=list(video.resolutions) if video.resolutions is not None else None,
        supported_aspect_ratios=(
            list(video.aspect_ratios) if video.aspect_ratios is not None else None
        ),
        supported_sizes=list(video.sizes) if video.sizes is not None else None,
        # The reader keeps "the vendor said nothing" apart from "no"; a form
        # knob the vendor did not vouch for is simply not offered.
        generate_audio=video.audio is True,
        seed=video.seed is True,
    )


def build_payload(
    *,
    kind: ProviderKind,
    model: str,
    prompt: str,
    duration: int | None = None,
    resolution: str | None = None,
    aspect_ratio: str | None = None,
    generate_audio: bool = False,
    seed: int | None = None,
) -> dict[str, object]:
    """The submit body, in this vendor's spelling.

    Scoped by kind because the bodies genuinely differ, and not only in field
    names: ``generate_audio`` defaults to false on OpenRouter and true on xAI,
    so "the GM wants no audio" is silence on one and an explicit false on the
    other. See :mod:`loreline.video.vendors` for each.
    """
    return vendor_for(kind).payload(
        GenerationRequest(
            model=model,
            prompt=prompt,
            duration=duration,
            resolution=resolution,
            aspect_ratio=aspect_ratio,
            generate_audio=generate_audio,
            seed=seed,
        )
    )


async def start_generation(
    *,
    config: ProviderConfig,
    api_key: str | None,
    payload: dict[str, object],
    client_factory: ClientFactory | None = None,
) -> str:
    """Submit a generation and return the upstream job id.

    Raises :class:`VideoError` on anything but a successful submission - an
    unsupported parameter, an unknown model or a billing failure all surface
    here as the provider's own message.
    """
    vendor = vendor_for(config.kind)
    client = _client(config, api_key, client_factory)
    try:
        response = await _request(client, "POST", vendor.submit_path, json=payload)
        remote_id = vendor.remote_id(_body(response))
        if not remote_id:
            msg = "provider accepted the request but returned no job id"
            raise VideoError(msg)
        return remote_id
    finally:
        await client.aclose()


async def poll_generation(
    *,
    config: ProviderConfig,
    api_key: str | None,
    remote_id: str,
    client_factory: ClientFactory | None = None,
) -> GenerationState:
    """One status check for a submitted generation."""
    vendor = vendor_for(config.kind)
    client = _client(config, api_key, client_factory)
    try:
        response = await _request(client, "GET", vendor.poll_path(remote_id))
        return vendor.state(_body(response))
    finally:
        await client.aclose()


async def download_video(
    *,
    config: ProviderConfig,
    api_key: str | None,
    remote_id: str,
    state: GenerationState | None = None,
    client_factory: ClientFactory | None = None,
) -> bytes:
    """Fetch a completed generation's bytes.

    Downloaded rather than linked on purpose: both vendors' result URLs expire
    (OpenRouter has a literal ``expired`` job state, and xAI hands back a URL on
    a storage host), and a session's video should still play months later, next
    to its audio.

    ``state`` is the terminal poll result, because for one vendor that *is* the
    address: xAI answers the poll with the URL rather than serving the bytes
    from a path built off the job id. The credential goes only where the
    vendor's own API is called (see ContentTarget).
    """
    vendor = vendor_for(config.kind)
    target = vendor.content(remote_id, state)
    client = _client(
        config,
        api_key if target.authenticated else None,
        client_factory,
        timeout=_DOWNLOAD_TIMEOUT_S,
    )
    try:
        response = await _request(client, "GET", target.url)
        if not response.content:
            msg = "provider reported the video ready but returned no content"
            raise VideoError(msg)
        return response.content
    finally:
        await client.aclose()


async def _request(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    json: dict[str, object] | None = None,
) -> httpx.Response:
    """One call, with the two failures every caller reports the same way.

    A transport error names the host that could not be reached; a 4xx or 5xx
    carries the provider's own message, because "400 Bad Request" never says
    which parameter was wrong.
    """
    try:
        response = await client.request(method, url, json=json)
    except httpx.HTTPError as exc:
        msg = f"could not reach {client.base_url}: {exc}"
        raise VideoError(msg) from exc
    if response.status_code >= HTTPStatus.BAD_REQUEST:
        raise VideoError(_error_detail(response))
    return response
