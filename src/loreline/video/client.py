"""OpenRouter video-generation API client.

Video generation is asynchronous *at the source*, which is what shapes this
module. Unlike chat completions, there is no request that returns the result:

    POST /videos             -> {"id", "status", "polling_url", …}
    GET  /videos/{id}        -> {"status": pending|in_progress|completed|
                                            failed|cancelled|expired, "error", …}
    GET  /videos/{id}/content -> the encoded video bytes
    GET  /videos/models      -> per-model parameter support

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

from loreline.capabilities import supports
from loreline.logging import get_logger
from loreline.models import Interaction, ProviderConfig, ProviderKind, VideoModelInfo

log = get_logger(__name__)

_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
# A start/poll call is a small JSON round trip; only the content download moves
# real bytes, so it gets its own, much longer budget.
_TIMEOUT_S = 60.0
_DOWNLOAD_TIMEOUT_S = 600.0

# Same leaderboard attribution headers the chat connector sends.
_OPENROUTER_HEADERS = {
    "HTTP-Referer": "https://github.com/LuckyType/loreline",
    "X-Title": "Loreline",
}

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


def base_url(config: ProviderConfig) -> str:
    return (config.base_url or _OPENROUTER_BASE_URL).rstrip("/")


def _client(
    config: ProviderConfig,
    api_key: str | None,
    factory: ClientFactory | None,
    *,
    timeout: float = _TIMEOUT_S,
) -> httpx.AsyncClient:
    if factory is not None:
        return factory()
    headers = {**_OPENROUTER_HEADERS}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return httpx.AsyncClient(base_url=base_url(config), headers=headers, timeout=timeout)


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

    Best-effort, like the chat model catalog: a failed fetch yields an empty
    list rather than raising, so the dialog can still open and say so instead
    of erroring the page.
    """
    client = _client(config, api_key, client_factory)
    try:
        response = await client.get("/videos/models")
        response.raise_for_status()
        return _parse_models(response.json())
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("video.models.fetch_failed", error=str(exc))
        return []
    finally:
        await client.aclose()


def _int_list(raw: object) -> list[int] | None:
    if not isinstance(raw, list):
        return None
    return [v for v in cast("list[object]", raw) if isinstance(v, int)]


def _str_list(raw: object) -> list[str] | None:
    if not isinstance(raw, list):
        return None
    return [v for v in cast("list[object]", raw) if isinstance(v, str)]


def _parse_models(payload: object) -> list[VideoModelInfo]:
    data: object = payload
    if isinstance(payload, dict):
        data = cast("dict[str, object]", payload).get("data", [])
    items = cast("list[object]", data) if isinstance(data, list) else []
    models: list[VideoModelInfo] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        entry = cast("dict[str, object]", item)
        model_id = entry.get("id")
        if not isinstance(model_id, str):
            continue
        name = entry.get("name")
        description = entry.get("description")
        models.append(
            VideoModelInfo(
                id=model_id,
                name=name if isinstance(name, str) else model_id,
                description=description if isinstance(description, str) else None,
                supported_durations=_int_list(entry.get("supported_durations")),
                supported_resolutions=_str_list(entry.get("supported_resolutions")),
                supported_aspect_ratios=_str_list(entry.get("supported_aspect_ratios")),
                supported_sizes=_str_list(entry.get("supported_sizes")),
                generate_audio=entry.get("generate_audio") is True,
                seed=entry.get("seed") is True,
            )
        )
    return sorted(models, key=lambda m: m.id)


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
