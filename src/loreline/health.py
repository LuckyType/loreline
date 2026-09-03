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
from websockets.exceptions import InvalidStatus

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
    return looks_like_auth_body(response.text)


def looks_like_auth_body(raw: str) -> bool:
    """:func:`looks_like_auth_failure` for a body read off an exception."""
    return _has_auth_reason(body_json(raw)) or looks_like_auth_message(error_message(raw))


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
    return body_json(response.text)


def body_json(raw: str) -> dict[str, object] | None:
    """:func:`error_body` for a body that never was an ``httpx.Response``.

    A rejected websocket upgrade carries its body as bytes on the exception,
    and both halves must unwrap Google's array envelope the same way.
    """
    try:
        payload: object = json.loads(raw)
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
    """The message under whichever of the usual keys this vendor chose.

    ``err_msg`` is Deepgram's, whose REST errors are flat
    ``{"err_code": ..., "err_msg": ..., "request_id": ...}`` objects rather
    than an ``error`` envelope; without it a 402 reached the GM as raw JSON.
    """
    for key in ("detail", "message", "error", "err_msg"):
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


# ---------------------------------------------------------------------------
# The request path: is another request worth making?
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RequestFailure:
    """Why one real request failed, and whether repeating it can ever work.

    A probe asks "can this provider serve a session". A failed transcription
    asks something else: "will the next utterance fail the same way". The five
    :class:`HealthStatus` states cannot answer the second question on their
    own, because one of them covers both answers - a 429 is ``DEGRADED``
    whether the vendor is throttling us for two seconds or the account has no
    credits left, and those want opposite handling. So ``terminal`` is a second
    axis over the same five states rather than a sixth state.
    """

    terminal: bool
    """True when every subsequent request to this provider fails identically.

    Retrying then costs two doomed API calls per utterance and yields nothing;
    the run should move to another provider, and stop when there is none.
    """

    status: HealthStatus
    """What the failure says about the provider, in the probe vocabulary."""

    detail: str
    """The vendor's own sentence, which is what a GM can act on."""


# Bodies that say the account rather than the moment is what is wrong. All
# verbatim, from vendor docs or from the deployment that reported this bug;
# matched lowercased, and kept to phrases rather than bare words ("credits",
# "quota") that an ordinary throttle also uses.
#   OpenAI 429      "You have no credits remaining. Add credits to continue
#                    using the API at .../settings/organization/billing/."
#                   error.type "insufficient_quota"
#   OpenAI 429      "You exceeded your current quota, please check your plan
#                    and billing details."  (the older wording, same type)
#   OpenRouter 402  "Your account or API key has insufficient credits. Add
#                    more credits and retry the request."
#   Deepgram 402    "Project does not have enough credits for an ASR request
#                    and does not have an overage agreement."
#                   err_code "ASR_PAYMENT_REQUIRED"
#   AssemblyAI 400  "Your current account balance is negative. Please top up
#                    to continue using the API."
_BILLING_PHRASES = (
    "no credits remaining",
    "add credits",
    "more credits",
    "insufficient credits",
    "insufficient_quota",
    "enough credits",
    "exceeded your current quota",
    "plan and billing",
    "balance is negative",
    "top up",
    "payment_required",
    "payment required",
)
# What a vendor says when it means "come back later", which outranks the
# phrases above. Google needs this: on the free tier an ordinary per-minute
# throttle answers 429 with the exact sentence OpenAI spends on an exhausted
# account - "You exceeded your current quota, please check your plan and
# billing details." - so the prose alone would abort a healthy job every time a
# free key hit its RPM ceiling. What separates them is that Google attaches
#   "details": [{"@type": ".../google.rpc.RetryInfo", "retryDelay": "23s"}]
# and a vendor that tells you when to come back has not cut you off.
_RETRY_MARKERS = ("retrydelay", "retry_delay", "retry-after", "try again in")


def classify_request_error(exc: BaseException) -> RequestFailure:
    """Grade an exception raised while transcribing. Never raises.

    Status codes alone cannot decide this, the same way they could not grade a
    probe, and 429 is the case that matters: a rate limit is transient and
    aborting a job over one would be worse than the bug this exists to fix,
    while "no credits remaining" is terminal and retrying it is pure waste.
    Only the body tells them apart. Observed per vendor:

    ================  ========================================  ==========
    answer            body                                      verdict
    ================  ========================================  ==========
    OpenAI 429        type ``insufficient_quota``               terminal
    OpenAI 429        code ``rate_limit_exceeded``              transient
    OpenAI 401        "Incorrect API key provided: sk-..."      terminal
    OpenRouter 402    "insufficient credits"                    terminal
    Deepgram 402      ``ASR_PAYMENT_REQUIRED``                  terminal
    Deepgram 429      "Too many requests. Please try again"     transient
    AssemblyAI 400    "account balance is negative"             terminal
    AssemblyAI 401    "Authentication error, API token ..."     terminal
    Google 400/401    "API key not valid. ..."                  terminal
    Gemini 429        quota prose + ``retryDelay``              transient
    any 5xx, timeout, dropped socket                            transient
    ================  ========================================  ==========

    Two deviations from :func:`classify_status`, which grades the same answers
    for the settings page:

    * **404 is terminal here.** A probe reads it as a mistyped base URL; a
      transcription request reads it as a model or route that does not exist
      (Deepgram spends 404 on "No such model/language/tier combination
      found."). Both are unfixable from inside a run, so both stop.
    * **A transport failure is transient here.** The probe calls it
      ``UNREACHABLE`` and a GM fixes the URL; mid-run it is far more often a
      dropped socket or a slow upload, and one bad minute must not end a
      session that would have recovered on the next utterance.
    """
    answer = _http_answer(exc)
    if answer is None:
        # No status: a timeout, a refused or dropped connection, a protocol
        # error, or a connector raising on its own (AssemblyAI's job status).
        # Nothing here distinguishes a blip from a wall, so keep the old
        # behaviour and fail over per utterance.
        return RequestFailure(
            terminal=False, status=HealthStatus.UNREACHABLE, detail=_exception_detail(exc)
        )
    status_code, raw = answer
    message = error_message(raw)
    if _out_of_credit(status_code, raw):
        # Graded DEGRADED rather than UNAUTHORIZED even where the sentence
        # mentions the key ("Your account or API key has insufficient
        # credits"): the credential was accepted, the vendor simply will not
        # serve it, which is what DEGRADED already means. Terminal all the
        # same, because a balance does not refill by being asked again.
        return RequestFailure(
            terminal=True, status=HealthStatus.DEGRADED, detail=message or _exception_detail(exc)
        )
    report = classify_status(status_code, message, auth_hint=looks_like_auth_body(raw))
    return RequestFailure(
        terminal=_is_terminal(status_code, report.status),
        status=report.status,
        detail=report.detail or _exception_detail(exc),
    )


def _out_of_credit(status_code: int, raw: str) -> bool:
    """True when the answer blames the account's balance rather than the moment.

    This is the crux of the bug: the same 429 carries both readings and only
    the body separates them.
    """
    if status_code >= HTTPStatus.INTERNAL_SERVER_ERROR:
        return False
    if any(marker in raw.lower() for marker in _RETRY_MARKERS):
        return False
    # 402 needs no body: OpenRouter and Deepgram both spend it on exactly this,
    # and the status means nothing else.
    return status_code == HTTPStatus.PAYMENT_REQUIRED or looks_like_billing(raw)


def _is_terminal(status_code: int, status: HealthStatus) -> bool:
    """Whether a graded answer means every later request fails the same way."""
    if status is HealthStatus.UNAUTHORIZED:
        # A key is not accepted on the fourth utterance having been rejected on
        # the first three.
        return True
    # UNREACHABLE from here is always a 404 (a transport failure never reaches
    # this function), i.e. a model or a route that does not exist.
    return status_code == HTTPStatus.NOT_FOUND


def looks_like_billing(raw: str) -> bool:
    """True when an error body blames the account's balance or quota.

    The whole body, not only the sentence: OpenAI states the cause in
    ``error.type`` ("insufficient_quota") and Deepgram in ``err_code``, and a
    vendor that adds a third field name should not need this list edited again.
    """
    return any(phrase in raw.lower() for phrase in _BILLING_PHRASES)


def _http_answer(exc: BaseException) -> tuple[int, str] | None:
    """The status and raw body of a rejected request, whichever way it arrived.

    A batch connector raises ``httpx.HTTPStatusError``; a streaming one is
    rejected during the websocket upgrade, which is a plain HTTP response
    carried on ``InvalidStatus`` - the same pairing
    :func:`loreline.stt.backends._ws.classify_handshake_error` reads for
    probes. Anything else answered with no status at all.
    """
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code, exc.response.text
    if isinstance(exc, InvalidStatus):
        return exc.response.status_code, exc.response.body.decode("utf-8", "replace")
    return None


def _exception_detail(exc: BaseException) -> str:
    """The exception's own words, or its type when it carries none."""
    return str(exc).strip()[:_MAX_DETAIL_CHARS] or type(exc).__name__
