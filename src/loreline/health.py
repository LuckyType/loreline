"""What "healthy" means for a provider, and the one place that decides it.

The Test button in provider settings used to answer a single bit, and answered
it wrong. Every LLM probe was ``GET /models`` graded ``status_code < 500``, so
a completely invalid key came back healthy: Google answers a bad key with 400
and OpenAI with 401, both under the threshold. The STT side disagreed with
itself as well, grading the same request ``< 400`` in three connectors, ``<
500`` in a fourth, and "did the socket open" in four more. "Healthy" therefore
meant something different depending on which row the GM clicked, which is worse
than being uniformly weak.

Three separate facts hide behind that one bit, and they need different actions
from a GM:

* the endpoint answers at all - DNS, TCP, TLS, no typo in the base URL;
* the credential is present and accepted;
* neither of those is currently blocked by the vendor (quota, outage).

:class:`HealthStatus` distinguishes exactly those, plus an explicit "could not
tell". The states stop there on purpose: a state the settings page cannot act
on is a colour with no instruction attached.

Two rules the probes here are built around:

* **A failing probe must never be worse than no probe.** Nothing in this module
  raises, and an answer we cannot interpret is ``UNKNOWN``, never ``UNREACHABLE``.
  Reporting a working provider as broken sends a GM to re-paste a key that was
  fine all along.
* **Bounded and free.** This runs from a button click that fans out across every
  configured provider, so every probe is capped by :data:`PROBE_TIMEOUT_S` and
  every probe endpoint is a read that costs nothing and starts no capture.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from enum import StrEnum
from http import HTTPStatus
from typing import cast

import httpx

from loreline.capabilities import requires_api_key
from loreline.logging import get_logger
from loreline.models import ProviderKind

log = get_logger(__name__)

# Deliberately much tighter than the connectors' own client timeouts (60 s for
# STT, 120 s for chat): those bound a transcription, this bounds a button. One
# unresponsive provider must not hold up the rest of a "test all" fan-out.
PROBE_TIMEOUT_S = 10.0
# The websocket connectors read one server frame after the handshake; a session
# that simply waits for audio never sends one, so this is a normal, healthy end.
SOCKET_READ_TIMEOUT_S = 5.0
# Details go into a badge tooltip, so a provider that answers with a whole HTML
# error page must not put all of it there.
_MAX_DETAIL_CHARS = 300


class HealthStatus(StrEnum):
    """How far a provider got when we asked it a cheap question.

    Ordered from best to worst as the settings page renders them. The value
    strings are the wire format of ``POST /providers/{id}/test`` and the
    frontend switches on them directly, so they are API, not labels.
    """

    HEALTHY = "healthy"
    """Answered, and accepted the credential. Nothing to do."""

    DEGRADED = "degraded"
    """Answered, but could not serve the probe: rate limited, or a 5xx.

    Not a configuration problem and not something a GM can fix, which is why it
    is neither ``HEALTHY`` (the provider is unusable right now) nor a failure
    (the key and the URL are not what is wrong). Action: wait, or check quota.
    """

    UNAUTHORIZED = "unauthorized"
    """Reached the endpoint; it rejected or demanded the credential.

    Covers a missing key, a wrong key and a key without access to the API.
    Action: fix the key. The vendor's own message rides along in
    :attr:`HealthReport.detail`, which is what separates those three in
    practice ("API key not valid" vs "Method doesn't allow unregistered
    callers").
    """

    UNREACHABLE = "unreachable"
    """Never got an answer, or got one from something that is not this API.

    DNS failure, refused connection, TLS failure, timeout, or a 404 with no
    auth complaint in it. Action: fix the base URL, or check the network.
    """

    UNKNOWN = "unknown"
    """The probe ran but its answer does not decide anything.

    An unexpected status, an unreadable body, a websocket error frame we cannot
    read. Explicitly not a failure: see the module docstring on why guessing
    "broken" here is worse than admitting ignorance.
    """


@dataclass(frozen=True)
class HealthReport:
    """One probe's verdict, plus the vendor's own words where it gave any.

    ``detail`` is what makes ``UNAUTHORIZED`` actionable rather than merely red:
    "API key not valid. Please pass a valid API key." tells a GM what to do,
    and the old boolean threw it away.
    """

    status: HealthStatus
    detail: str | None = None

    @property
    def healthy(self) -> bool:
        """The one-bit summary, for call sites that genuinely only need it."""
        return self.status is HealthStatus.HEALTHY


def missing_credential(kind: ProviderKind, api_key: str | None) -> HealthReport | None:
    """Verdict for a cloud kind with no stored key, without touching the network.

    Worth short-circuiting for its own sake (a probe that cannot succeed is a
    probe not worth paying for), but it also sidesteps a real trap: Google's
    OpenAI-compatible ``/models`` answers a *keyless* request with **404
    "Requested entity was not found."**, which is indistinguishable from a
    wrong base URL by status and body alike. Verified against the live API.

    Returns None for a self-hosted kind, whose ``auth: optional`` server may
    well answer fine with no key at all, and for a kind that has one.
    """
    if api_key or not requires_api_key(kind):
        return None
    return HealthReport(HealthStatus.UNAUTHORIZED, "no API key stored for this provider")


async def probe_endpoint(
    client: httpx.AsyncClient,
    path: str,
    *,
    params: dict[str, str | int] | None = None,
    timeout_s: float = PROBE_TIMEOUT_S,
) -> HealthReport:
    """GET a cheap endpoint and grade the answer. Never raises.

    ``path`` should be a read that exercises the credential and costs nothing.
    Which path that is differs per vendor and is *not* always ``/models``: both
    Deepgram's and OpenRouter's model lists are served to anonymous callers, so
    grading them proves the host is up and nothing else. See the probe constant
    in each connector for what it asks instead, and why.

    Redirects are followed: a self-hosted endpoint bouncing http to https, or
    adding a trailing slash, is a working provider and should not read as one
    that answered strangely.
    """
    try:
        # The wrapper, not the client's own timeout, is the real bound here:
        # connectors hand this their long-lived transcription client, whose
        # timeout is sized for uploading audio rather than clicking a button.
        async with asyncio.timeout(timeout_s):
            response = await client.get(path, params=params, follow_redirects=True)
    except (TimeoutError, httpx.TimeoutException):
        return HealthReport(HealthStatus.UNREACHABLE, f"no answer within {timeout_s:.0f}s")
    except httpx.HTTPError as exc:
        return HealthReport(HealthStatus.UNREACHABLE, _transport_detail(exc))
    except Exception as exc:
        # Anything else is a bug in this app, not a verdict about the provider,
        # so it degrades to "we could not tell" rather than to "down".
        log.warning("health.probe.unexpected_error", path=path, error=str(exc))
        return HealthReport(HealthStatus.UNKNOWN, "the probe itself failed")
    return classify_response(response)


def classify_response(response: httpx.Response) -> HealthReport:
    """Grade an HTTP probe response into a state.

    Status codes alone cannot do this, which is the whole reason the old
    threshold was wrong. Observed against the live APIs:

    ===================================  ===============  ==========================
    probe                                bad key          no key
    ===================================  ===============  ==========================
    OpenAI ``GET /v1/models``            401              401
    OpenRouter ``GET /key``              401              401
    Gemini compat ``GET /openai/models`` 400              404 (!)
    Gemini native ``GET /v1beta/models`` 400 or 401       403
    AssemblyAI ``GET /v2/transcript``    401              401
    Deepgram ``GET /v1/auth/token``      401              401
    ===================================  ===============  ==========================

    Google is not even self-consistent within one column: on the native surface
    a key that is merely corrupted answers 401 while one that never existed
    answers 400. So a bare ``== 200`` would be wrong in the other direction (a
    429 means the credential is fine), and 400 has to be read rather than
    assumed: Google spends it on bad credentials while every other endpoint
    here spends it on bad requests. Hence :func:`looks_like_auth_failure`.
    """
    return classify_status(
        response.status_code, error_detail(response), auth_hint=looks_like_auth_failure(response)
    )


def classify_status(status_code: int, message: str, *, auth_hint: bool = False) -> HealthReport:
    """Shared grading for an HTTP status, whether it arrived over HTTP or as a
    rejected websocket upgrade.

    ``auth_hint`` says whether the body complained about the credential, which
    is the only way to read a 400 correctly - see :func:`looks_like_auth_failure`.
    """
    if status_code < HTTPStatus.MULTIPLE_CHOICES:
        return HealthReport(HealthStatus.HEALTHY)
    if status_code == HTTPStatus.TOO_MANY_REQUESTS:
        # Checked before the credential cases on purpose: being throttled is
        # positive evidence that the key works, not a reason to doubt it.
        return HealthReport(HealthStatus.DEGRADED, message or "rate limited")
    if status_code >= HTTPStatus.INTERNAL_SERVER_ERROR:
        # We reached the vendor and it broke. Nothing about the config is
        # wrong, and the credential is simply untested, so this is neither a
        # pass nor a failure.
        return HealthReport(HealthStatus.DEGRADED, message or f"provider returned {status_code}")
    if status_code in (HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN) or auth_hint:
        # 403 lands here rather than in a state of its own: Google spends it on
        # "Method doesn't allow unregistered callers", i.e. a missing key, and
        # every other 403 a probe can draw is still "this credential may not do
        # this". Both are fixed in the same place, so they are one state, and
        # the message says which. ``auth_hint`` is what pulls Google's 400 in
        # alongside them.
        return HealthReport(HealthStatus.UNAUTHORIZED, message or "credential rejected")
    if status_code == HTTPStatus.NOT_FOUND:
        # Reached a server, but this URL serves no such API. In practice that
        # is a mistyped base_url, which is the actionable reading; a 404 that
        # was really an auth complaint was already caught just above.
        return HealthReport(HealthStatus.UNREACHABLE, message or "no API at this URL")
    return HealthReport(HealthStatus.UNKNOWN, message or f"unexpected status {status_code}")


# Phrases that mark an error body as a credential complaint rather than any
# other 4xx. Needed because Google answers a bad key with 400, and with a
# different sentence depending on which of two sibling surfaces was asked and
# on how the key is malformed - all five observed live, same key, same day:
#   compat  /openai/models  400 "Invalid Auth key."                  (key corrupted)
#   compat  /openai/models  400 "Please pass a valid API key"        (key fabricated)
#   native  /v1beta/models  400 "API key not valid. Please pass a valid API key."
#   native  /v1beta/models  401 "Request had invalid authentication credentials. ..."
#   native  /v1beta/models  403 "Method doesn't allow unregistered callers ..."
# Matched against the lowercased message. Kept to phrases, not the bare word
# "key", which any parameter complaint would trip.
_AUTH_PHRASES = (
    "api key",
    "api_key",
    "auth key",
    "authentication",
    "unauthenticated",
    "unauthorized",
    "credential",
    "permission denied",
    "access token",
    "invalid token",
)
# Google also states the machine-readable cause in ``error.details[].reason``
# on the native surface, which beats matching prose. The compat surface omits
# it entirely, so the prose scan above cannot be dropped.
_AUTH_REASONS = (
    "API_KEY_INVALID",
    "ACCESS_TOKEN_EXPIRED",
    "CREDENTIALS_MISSING",
    "PERMISSION_DENIED",
)


def looks_like_auth_failure(response: httpx.Response) -> bool:
    """True when a 4xx body complains about the credential rather than the request."""
    return _has_auth_reason(error_body(response)) or looks_like_auth_message(error_detail(response))


def looks_like_auth_message(text: str) -> bool:
    """True when a provider's own words name a credential fault.

    Split out from :func:`looks_like_auth_failure` because the websocket
    connectors get their text from a handshake body or an error frame rather
    than from an ``httpx.Response``, and both halves must agree on what counts.
    """
    lowered = text.lower()
    return any(phrase in lowered for phrase in _AUTH_PHRASES)


def _has_auth_reason(body: dict[str, object] | None) -> bool:
    """True if a Google-style ``error.details[].reason`` names a credential fault."""
    error = (body or {}).get("error")
    if not isinstance(error, dict):
        return False
    details = cast("dict[str, object]", error).get("details")
    if not isinstance(details, list):
        return False
    for entry in cast("list[object]", details):
        if not isinstance(entry, dict):
            continue
        reason = cast("dict[str, object]", entry).get("reason")
        if isinstance(reason, str) and reason in _AUTH_REASONS:
            return True
    return False


def error_body(response: httpx.Response) -> dict[str, object] | None:
    """The error envelope of a failed response, unwrapped, or None if unreadable.

    Google's OpenAI-compatible endpoint wraps it in a one-element JSON array,
    ``[{"error": {...}}]``, where every other endpoint here returns the bare
    object - verified against the live API, and inconsistently even there: the
    same base URL's ``/models`` errors come back unwrapped. Without this a
    Gemini failure would surface as a bare "404 Not Found" instead of the
    message naming the model that does not exist.

    Note Google's error object carries ``code``/``message``/``status`` and no
    ``param``, so ``loreline.llm._rejects_parameter`` never fires for it. That
    costs nothing as long as capabilities.yaml keeps its per-model effort lists
    honest, which is where the retry would otherwise be the safety net.
    """
    try:
        payload: object = response.json()
    except ValueError:
        return None
    if isinstance(payload, list) and payload:
        payload = cast("list[object]", payload)[0]
    return cast("dict[str, object]", payload) if isinstance(payload, dict) else None


def error_detail(response: httpx.Response) -> str:
    """Best-effort human-readable reason from an HTTP error response body.

    Status lines alone ("404 Not Found") throw away the part that actually
    says what to fix - "Model 'X' is not installed locally", "API key not
    valid".
    """
    return error_message(response.text) or response.reason_phrase


def error_message(raw: str) -> str:
    """The human-readable part of an error body, JSON or not.

    Takes text rather than a response so the websocket connectors can use it
    too: a rejected upgrade carries its body as bytes on the exception, not as
    an ``httpx.Response``, and a GM should not be shown raw JSON in one place
    and a sentence in the other.

    Providers bury the sentence under varying keys, so the common ones are
    tried in turn; a body that is not JSON at all is returned as-is, which is
    what Deepgram's ``/v1/auth/token`` ("Invalid credentials.") and
    AssemblyAI's plain-text 422 need.
    """
    try:
        payload: object = json.loads(raw)
    except ValueError:
        return raw.strip()[:_MAX_DETAIL_CHARS]
    if isinstance(payload, list) and payload:
        payload = cast("list[object]", payload)[0]
    if isinstance(payload, dict):
        found = _message_from(cast("dict[str, object]", payload))
        if found:
            return found
    return raw.strip()[:_MAX_DETAIL_CHARS]


def _message_from(payload: dict[str, object]) -> str | None:
    """The message under whichever of the usual keys this vendor chose."""
    for key in ("detail", "message", "error"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
        if isinstance(value, dict):
            nested = cast("dict[str, object]", value).get("message")
            if isinstance(nested, str) and nested:
                return nested
    return None


def _transport_detail(exc: httpx.HTTPError) -> str:
    """Name the failure without leaking a URL that may carry a key in a query."""
    reason = str(exc).strip() or type(exc).__name__
    return f"could not connect: {reason}"
