"""OpenRouter video-generation API client.

Video generation is asynchronous *at the source*, which is what shapes this
module. Unlike chat completions, there is no request that returns the result:

    POST /videos             -> {"id", "status", "polling_url", …}
    GET  /videos/{id}        -> {"status": pending|in_progress|completed|
                                            failed|cancelled|expired, "error", …}
    GET  /videos/{id}/content -> the encoded video bytes
    GET  /videos/models      -> per-model parameter support (read through
                                the shared catalogue reader, loreline.catalog)

A generation runs for minutes, so nothing here blocks a request thread; the
polling loop lives in :mod:`loreline.video.jobs`, which owns the job row.

Written against ``httpx`` rather than OpenRouter's official SDK for the same
reason :mod:`loreline.llm` is: one HTTP client, one auth path, one set of
mockable transports in the tests. The endpoints are a handful of plain REST
calls and the SDK would pin an upper bound on ``pydantic`` for them.
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

log = get_logger(__name__)

# A start/poll call is a small JSON round trip; only the content download moves
# real bytes, so it gets its own, much longer budget.
_TIMEOUT_S = 60.0
_DOWNLOAD_TIMEOUT_S = 600.0

# Upstream job states. Only `completed` yields bytes; the other three terminal
# states are failures from this app's point of view, `expired` included - it
# means the result was collected too late, which is still no video.
_TERMINAL_OK = "completed"
_TERMINAL_FAILED = frozenset({"failed", "cancelled", "expired"})

ClientFactory = Callable[[], httpx.AsyncClient]


class VideoError(Exception):
    """A video-generation call failed, or the generation itself did."""


def supports_video(kind: ProviderKind) -> bool:
    """Whether a provider kind can generate video.

    OpenRouter only: it is the one configured provider here that exposes a
    video API at all. A plain OpenAI-compatible chat endpoint (Ollama, LM
    Studio, vLLM) has no equivalent, so those must not be offered. Answered
    from the one capability table - see loreline.capabilities.
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
    # The kind's video surface: the gateway base plus the same attribution
    # headers the chat connector sends, both declared in capabilities.yaml.
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
    model: str,
    prompt: str,
    duration: int | None = None,
    resolution: str | None = None,
    aspect_ratio: str | None = None,
    generate_audio: bool = False,
    seed: int | None = None,
) -> dict[str, object]:
    """The ``POST /videos`` body.

    Optional parameters are omitted when unset rather than sent as null:
    models differ in which ones they accept at all (see VideoModelInfo), and a
    model handed a parameter it does not support rejects the whole request.
    ``generate_audio`` is only sent when true for the same reason.
    """
    payload: dict[str, object] = {"model": model, "prompt": prompt}
    if duration is not None:
        payload["duration"] = duration
    if resolution:
        payload["resolution"] = resolution
    if aspect_ratio:
        payload["aspect_ratio"] = aspect_ratio
    if generate_audio:
        payload["generate_audio"] = True
    if seed is not None:
        payload["seed"] = seed
    return payload


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
    client = _client(config, api_key, client_factory)
    try:
        try:
            response = await client.post("/videos", json=payload)
        except httpx.HTTPError as exc:
            msg = f"could not reach {client.base_url}: {exc}"
            raise VideoError(msg) from exc
        if response.status_code >= HTTPStatus.BAD_REQUEST:
            raise VideoError(_error_detail(response))
        body = response.json()
        remote_id = cast("dict[str, object]", body).get("id") if isinstance(body, dict) else None
        if not isinstance(remote_id, str) or not remote_id:
            msg = "provider accepted the request but returned no job id"
            raise VideoError(msg)
        return remote_id
    finally:
        await client.aclose()


class GenerationState:
    """A poll result: still running, finished, or failed.

    A tiny class rather than a bare tuple because all three call sites read
    the fields by name, and `done`/`failed` are not the same question.
    """

    def __init__(self, status: str, error: str | None = None) -> None:
        self.status = status
        self.error = error

    @property
    def done(self) -> bool:
        return self.status == _TERMINAL_OK

    @property
    def failed(self) -> bool:
        return self.status in _TERMINAL_FAILED

    @property
    def finished(self) -> bool:
        return self.done or self.failed


async def poll_generation(
    *,
    config: ProviderConfig,
    api_key: str | None,
    remote_id: str,
    client_factory: ClientFactory | None = None,
) -> GenerationState:
    """One status check for a submitted generation."""
    client = _client(config, api_key, client_factory)
    try:
        try:
            response = await client.get(f"/videos/{remote_id}")
        except httpx.HTTPError as exc:
            msg = f"could not reach {client.base_url}: {exc}"
            raise VideoError(msg) from exc
        if response.status_code >= HTTPStatus.BAD_REQUEST:
            raise VideoError(_error_detail(response))
        body = response.json()
        if not isinstance(body, dict):
            msg = "unexpected polling response"
            raise VideoError(msg)
        entry = cast("dict[str, object]", body)
        status = entry.get("status")
        error = entry.get("error")
        return GenerationState(
            status=status if isinstance(status, str) else "unknown",
            error=error if isinstance(error, str) else None,
        )
    finally:
        await client.aclose()


async def download_video(
    *,
    config: ProviderConfig,
    api_key: str | None,
    remote_id: str,
    client_factory: ClientFactory | None = None,
) -> bytes:
    """Fetch a completed generation's bytes.

    Downloaded rather than linked on purpose: OpenRouter's result URLs expire,
    and a session's video should still play months later, next to its audio.
    """
    client = _client(config, api_key, client_factory, timeout=_DOWNLOAD_TIMEOUT_S)
    try:
        try:
            response = await client.get(f"/videos/{remote_id}/content")
        except httpx.HTTPError as exc:
            msg = f"could not reach {client.base_url}: {exc}"
            raise VideoError(msg) from exc
        if response.status_code >= HTTPStatus.BAD_REQUEST:
            raise VideoError(_error_detail(response))
        if not response.content:
            msg = "provider reported the video ready but returned no content"
            raise VideoError(msg)
        return response.content
    finally:
        await client.aclose()
