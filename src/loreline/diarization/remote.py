"""Remote diarization provider (sherpa-onnx HTTP service)."""

from __future__ import annotations

import contextlib
from typing import cast

import httpx

from loreline.health import (
    HealthReport,
    HealthStatus,
    body_json,
    probe_endpoint_response,
    raise_for_vendor_status,
)
from loreline.httpclient import ClientHandle
from loreline.logging import get_logger
from loreline.models import SpeakerSegment

log = get_logger(__name__)


# Much shorter than a diarization request's own timeout, and for the same
# reason as the probe's below: closing the diarizer happens inside the
# stop-session request, and a service that has hung must not hold a session's
# shutdown open for two minutes to be told something it will forget by itself.
_FORGET_TIMEOUT_S = 5.0


class RemoteDiarizer:
    """Call a self-hosted diarization service that returns speaker segments.

    The service contract (see ``services/diarization`` and ``mocks/diarization``):
    ``POST {endpoint}/diarize`` multipart ``file`` (WAV) ->
    ``{"segments": [{"start": float, "end": float, "speaker": str}, ...]}``,
    plus ``DELETE {endpoint}/sessions/{session_id}``.

    A ``session_id`` sent with the audio is what makes the labels usable: the
    live path posts one utterance at a time, and a service clustering each of
    them on its own answers "Speaker 0" for whoever spoke, so every voice in a
    session ends up under one name. With the id, the service matches this
    utterance against the voices that session has already heard. Whatever ids
    this diarizer has used are dropped from the service when it is closed,
    which is the session end reaching the service without any caller having to
    remember to say so.
    """

    def __init__(
        self,
        endpoint: str,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._endpoint = endpoint
        self._http = ClientHandle(client, base_url=endpoint, timeout=120.0)
        self._client = self._http.client
        self._sessions: set[str] = set()
        self._generations: dict[str, str] = {}

    async def diarize(
        self,
        wav: bytes,
        *,
        sample_rate: int = 16000,
        min_speakers: int | None = None,
        max_speakers: int | None = None,
        session_id: str | None = None,
    ) -> list[SpeakerSegment]:
        data: dict[str, str] = {"sample_rate": str(sample_rate)}
        if min_speakers is not None:
            data["min_speakers"] = str(min_speakers)
        if max_speakers is not None:
            data["max_speakers"] = str(max_speakers)
        if session_id is not None:
            data["session_id"] = session_id
            self._sessions.add(session_id)
        files = {"file": ("audio.wav", wav, "audio/wav")}
        response = await self._client.post("/diarize", data=data, files=files)
        raise_for_vendor_status(response)
        payload: object = response.json()
        if session_id is not None:
            self._note_generation(session_id, payload)
        return _parse_segments(payload)

    def _note_generation(self, session_id: str, payload: object) -> None:
        """Say it out loud when the service restarted in the middle of a session.

        The service keeps a session's speakers in its own process memory, so a
        restart forgets them and numbers the next utterance from Speaker 0
        again: from that point one label names two people, and nothing in the
        labels says so, since "Speaker 0" is what a working service answers too.
        The service stamps every answer with an id that changes when it
        restarts, so the first answer carrying a new one is the moment to warn.

        Only a warning, deliberately. The transcript already has both halves,
        and the fix is renaming the speakers on the session page - which a human
        has to do either way, because only a human knows which half was Alice.
        """
        generation = _generation_of(payload)
        if generation is None:
            return  # a service too old to stamp its answers; nothing to compare
        previous = self._generations.get(session_id)
        self._generations[session_id] = generation
        if previous is None or previous == generation:
            return
        log.warning(
            "diarization.service_restarted",
            session_id=session_id,
            endpoint=self._endpoint,
            previous_generation=previous,
            generation=generation,
        )

    async def aclose(self) -> None:
        """Forget this diarizer's sessions at the service, then close the client.

        Best effort throughout: a service that never learned about the session,
        one too old to know the route, and one that is simply gone all mean the
        same thing here, and none of them is worth failing a session's shutdown
        over. The service evicts an idle bank on its own anyway, so the worst a
        swallowed error costs is a few hundred floats until that TTL passes.
        """
        for session_id in self._sessions:
            with contextlib.suppress(Exception):
                await self._client.delete(f"/sessions/{session_id}", timeout=_FORGET_TIMEOUT_S)
        self._sessions.clear()
        self._generations.clear()
        await self._http.aclose()


def _generation_of(payload: object) -> str | None:
    """The service process id stamped on a ``/diarize`` answer, if it sent one."""
    if not isinstance(payload, dict):
        return None
    generation = cast("dict[str, object]", payload).get("generation")
    return generation if isinstance(generation, str) and generation else None


def _parse_segments(payload: object) -> list[SpeakerSegment]:
    if not isinstance(payload, dict):
        return []
    raw_segments = cast("dict[str, object]", payload).get("segments")
    if not isinstance(raw_segments, list):
        return []
    segments: list[SpeakerSegment] = []
    for raw in cast("list[object]", raw_segments):
        if not isinstance(raw, dict):
            continue
        item = cast("dict[str, object]", raw)
        start, end, speaker = item.get("start"), item.get("end"), item.get("speaker")
        if isinstance(start, (int, float)) and isinstance(end, (int, float)):
            segments.append(
                SpeakerSegment(start=float(start), end=float(end), speaker=str(speaker))
            )
    return segments


# Much shorter than a diarization request's own timeout: ``/api/system/healthz``
# calls this while the UI polls it every few seconds, and a hung diarizer must
# not stall the whole health response.
_PROBE_TIMEOUT_S = 2.0


# What ``/healthz`` calls session speaker memory, and what to say when a
# service does not advertise it. The image before this feature accepts the
# extra ``session_id`` form field and ignores it (FastAPI drops a form field no
# route parameter names), and answers 404 to the delete this client already
# swallows - so an app newer than its diarizer silently goes back to labelling
# every utterance "Speaker 0", with nothing anywhere saying why. A flag in the
# health body is the only thing that separates the two, since both are a
# service answering 200.
_SESSION_MEMORY_KEY = "session_memory"
_NO_SESSION_MEMORY = (
    "answers, but does not remember speakers between calls, so every utterance "
    "comes back as Speaker 0 - rebuild the diarization service image"
)


async def probe_diarizer(endpoint: str, *, client: httpx.AsyncClient | None = None) -> HealthReport:
    """Whether a diarization service answers at ``endpoint``, graded like a provider.

    Hits the service's ``GET /healthz`` (see ``services/diarization``) and
    grades the answer through :mod:`loreline.health`, the same five states the
    settings page renders for a provider row. This used to return a bool from
    ``status_code < 500``, the exact defect that grading replaced everywhere
    else: a mistyped endpoint on a live host answered 404 and read as
    reachable, while a service that answered 503 during model loading read the
    same as one that was not there at all. Never raises.

    A healthy answer is read once more, for the session-memory flag. A service
    without it is graded ``DEGRADED`` rather than ``HEALTHY``: of the five
    states that is the one that means "answered, but cannot serve what was
    asked", and it is the only one that puts the reason on the settings page
    instead of a green badge over a diarizer that names the whole table after
    one person.
    """
    http = ClientHandle(client, base_url=endpoint, timeout=_PROBE_TIMEOUT_S)
    try:
        report, response = await probe_endpoint_response(
            http.client, "/healthz", timeout_s=_PROBE_TIMEOUT_S
        )
    finally:
        await http.aclose()
    if report.status is not HealthStatus.HEALTHY or response is None:
        return report
    if _advertises_session_memory(response.text):
        return report
    return HealthReport(HealthStatus.DEGRADED, _NO_SESSION_MEMORY)


def _advertises_session_memory(body: str) -> bool:
    """Whether a health body claims session speaker memory.

    Tolerant in the direction :mod:`loreline.health` insists on: a body that is
    not JSON, or is JSON that is not an object, is one this cannot read rather
    than proof of a service without the capability, and calling a working
    diarizer broken is the worse of the two errors. Only a readable object that
    leaves the flag out, or false, is read as the old service - which is exactly
    what the old service's ``{"status": "ok"}`` is.
    """
    payload = body_json(body)
    if payload is None:
        return True
    return bool(payload.get(_SESSION_MEMORY_KEY))
