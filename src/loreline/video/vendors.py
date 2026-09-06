"""How each vendor's video API is spoken: one adapter per vendor.

Every video vendor holds the same three-step conversation - submit a prompt,
poll until it finishes, fetch the bytes - and no two of them spell any step the
same way. OpenRouter posts to ``/videos``, answers with ``id``, calls success
``completed`` and serves the result from a path built off that id. xAI posts to
``/videos/generations``, answers with ``request_id``, calls success ``done`` and
hands back a URL on a storage host instead. A single client written against one
of those is a client that silently mis-parses the other: the status string it
waits for never arrives, so the job polls until its deadline and fails with
"still unknown after 60 minutes".

So the vendor differences live here, as data and parsing, and the HTTP lives in
:mod:`loreline.video.client`: the client owns the connection, the timeouts and
the one way a failed request is reported, and asks a vendor only what it cannot
know - which path, which field, which word means done. That is the same split
the STT side already makes between :mod:`loreline.stt.registry` and its
connectors, one adapter per vendor, chosen by kind, with nothing about a vendor
in the code that drives them.

Adding a third vendor is a subclass and a line in :data:`_VENDORS`.

Docs: https://openrouter.ai/docs/api-reference/videos
      https://docs.x.ai/developers/model-capabilities/video/generation
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import cast

from loreline.models import ProviderKind


class VideoError(Exception):
    """A video-generation call failed, or the generation itself did."""


@dataclass(frozen=True, slots=True)
class GenerationRequest:
    """The knobs a GM set in the generate dialog, before any vendor sees them.

    One type rather than seven arguments threaded through three layers, and the
    place to look for what a video job is actually parameterised by: it is the
    generation half of a :class:`~loreline.models.VideoJob` row.
    """

    model: str
    prompt: str
    duration: int | None = None
    resolution: str | None = None
    aspect_ratio: str | None = None
    generate_audio: bool = False
    seed: int | None = None


@dataclass(frozen=True, slots=True)
class GenerationState:
    """A poll result: still running, finished, or failed.

    ``status`` is the vendor's own word, kept for the message a failed job
    shows; ``done`` and ``failed`` are this app's reading of it, decided by the
    vendor adapter because the vocabularies do not overlap ("completed" against
    "done", and only one of the two has a "cancelled").

    ``video_url`` is set by a vendor that answers the poll with the address of
    the result rather than serving it from a path built off the job id. None
    means the vendor's own content path is the answer.
    """

    status: str
    done: bool = False
    failed: bool = False
    error: str | None = None
    video_url: str | None = None

    @property
    def finished(self) -> bool:
        return self.done or self.failed


@dataclass(frozen=True, slots=True)
class ContentTarget:
    """Where a finished generation's bytes are fetched from.

    ``authenticated`` is not hygiene, it is correctness: a vendor that answers
    with a pre-signed URL on a storage host has already put the credential in
    the query string, and such hosts reject a request that also carries an
    ``Authorization`` header (two auth mechanisms). The row's key therefore goes
    only where the vendor's own API is being called.
    """

    url: str
    authenticated: bool = True


class VideoVendor(ABC):
    """One vendor's spelling of the submit/poll/fetch conversation.

    Deliberately does no HTTP and holds no state: it names paths and reads
    bodies. Everything else - the client, the timeouts, the error message a 4xx
    turns into - is the client's, once, for every vendor.
    """

    #: Where a generation is submitted, relative to the kind's video surface.
    submit_path: str

    @abstractmethod
    def payload(self, request: GenerationRequest) -> dict[str, object]:
        """The submit body this vendor wants."""

    @abstractmethod
    def remote_id(self, body: dict[str, object]) -> str | None:
        """The upstream job handle in a submit response, or None if absent."""

    @abstractmethod
    def poll_path(self, remote_id: str) -> str:
        """Where that handle's status is read."""

    @abstractmethod
    def state(self, body: dict[str, object]) -> GenerationState:
        """One poll response, graded."""

    @abstractmethod
    def content(self, remote_id: str, state: GenerationState | None) -> ContentTarget:
        """Where the finished bytes are, given the terminal poll result."""


class OpenRouterVideo(VideoVendor):
    """OpenRouter's ``/videos`` gateway.

    Five upstream states, of which one yields bytes. ``expired`` is a failure
    from this app's point of view like the other two: it means the result was
    collected too late, which is still no video.
    """

    submit_path = "/videos"

    _DONE = "completed"
    _FAILED = frozenset({"failed", "cancelled", "expired"})

    def payload(self, request: GenerationRequest) -> dict[str, object]:
        """The ``POST /videos`` body.

        Optional parameters are omitted when unset rather than sent as null:
        models differ in which ones they accept at all (see VideoModelInfo), and
        a model handed a parameter it does not support rejects the whole
        request. ``generate_audio`` is only sent when true for the same reason -
        false is the gateway's default, so silence says it.
        """
        payload: dict[str, object] = {"model": request.model, "prompt": request.prompt}
        if request.duration is not None:
            payload["duration"] = request.duration
        if request.resolution:
            payload["resolution"] = request.resolution
        if request.aspect_ratio:
            payload["aspect_ratio"] = request.aspect_ratio
        if request.generate_audio:
            payload["generate_audio"] = True
        if request.seed is not None:
            payload["seed"] = request.seed
        return payload

    def remote_id(self, body: dict[str, object]) -> str | None:
        return _text(body.get("id"))

    def poll_path(self, remote_id: str) -> str:
        return f"/videos/{remote_id}"

    def state(self, body: dict[str, object]) -> GenerationState:
        status = _text(body.get("status")) or "unknown"
        return GenerationState(
            status=status,
            done=status == self._DONE,
            failed=status in self._FAILED,
            error=_text(body.get("error")),
        )

    def content(self, remote_id: str, state: GenerationState | None) -> ContentTarget:
        """The gateway serves the bytes itself, behind the same key."""
        return ContentTarget(f"/videos/{remote_id}/content")


class XaiVideo(VideoVendor):
    """xAI's Grok Imagine generation endpoint.

    Four upstream states and a different word for success: ``done``, not
    ``completed``. The result is a URL in the poll body rather than a path on
    the API, so the poll's answer is what the download needs and
    :class:`GenerationState` carries it.
    """

    submit_path = "/videos/generations"

    _DONE = "done"
    _FAILED = frozenset({"failed", "expired"})

    def payload(self, request: GenerationRequest) -> dict[str, object]:
        """The ``POST /videos/generations`` body.

        Unset knobs are omitted, as on the gateway, with one deliberate
        exception: ``generate_audio`` DEFAULTS TO TRUE here, so a GM who turned
        audio off has to be represented by an explicit false. Omitting it, which
        is right for OpenRouter, would silently return a clip with sound.

        ``seed`` is not sent: xAI documents no such parameter, and this vendor
        rejects unknown fields rather than ignoring them.
        """
        payload: dict[str, object] = {"model": request.model, "prompt": request.prompt}
        if request.duration is not None:
            payload["duration"] = request.duration
        if request.resolution:
            payload["resolution"] = request.resolution
        if request.aspect_ratio:
            payload["aspect_ratio"] = request.aspect_ratio
        payload["generate_audio"] = request.generate_audio
        return payload

    def remote_id(self, body: dict[str, object]) -> str | None:
        return _text(body.get("request_id"))

    def poll_path(self, remote_id: str) -> str:
        return f"/videos/{remote_id}"

    def state(self, body: dict[str, object]) -> GenerationState:
        status = _text(body.get("status")) or "unknown"
        video = body.get("video")
        url = (
            _text(cast("dict[str, object]", video).get("url")) if isinstance(video, dict) else None
        )
        return GenerationState(
            status=status,
            done=status == self._DONE,
            failed=status in self._FAILED,
            # The failure reason is an object here, not a string: {"code": …,
            # "message": …}. The message is the half a GM can act on.
            error=_error_message(body.get("error")),
            video_url=url,
        )

    def content(self, remote_id: str, state: GenerationState | None) -> ContentTarget:
        """The URL the poll handed back, fetched without the row's key.

        A generation that finished without one is a failure rather than an empty
        download: there is no second place to look, and saying so names the
        vendor's broken promise instead of writing a zero-byte file.
        """
        url = state.video_url if state else None
        if not url:
            msg = "the generation finished but carried no video URL"
            raise VideoError(msg)
        return ContentTarget(url, authenticated=False)


def _text(value: object) -> str | None:
    """A non-empty string, or None for anything else (including empty)."""
    return value if isinstance(value, str) and value else None


def _error_message(value: object) -> str | None:
    """A vendor's error, whether it spells it as a string or as an object."""
    if isinstance(value, dict):
        return _text(cast("dict[str, object]", value).get("message"))
    return _text(value)


_VENDORS: dict[ProviderKind, VideoVendor] = {
    ProviderKind.OPENROUTER: OpenRouterVideo(),
    ProviderKind.XAI: XaiVideo(),
}


def vendor_for(kind: ProviderKind) -> VideoVendor:
    """The adapter for a kind, or a ValueError naming the kind that has none.

    Raising rather than defaulting: the alternative is speaking OpenRouter's
    dialect to a vendor that does not answer in it, which fails much later and
    much less clearly. Whether a kind generates video at all is
    capabilities.yaml's answer (see :func:`loreline.video.client.supports_video`)
    and is checked before a job is ever created; this is the second half of the
    same fact, kept next to the adapters it selects from.
    """
    vendor = _VENDORS.get(kind)
    if vendor is None:
        msg = f"no video connector for kind {kind.value!r}"
        raise ValueError(msg)
    return vendor
