"""Grading rules for provider health probes.

Every status code and body below is pinned from a real call to the vendor,
made while writing this module. That is the point: the old probes were graded
by threshold, and the thresholds were wrong because vendors disagree about
which status a bad key gets (Google 400, OpenAI 401, Deepgram and AssemblyAI
401) and about whether an unauthenticated caller gets an error at all
(Deepgram's and OpenRouter's model lists are public).

Nothing here touches the network: the live answers are frozen into
``httpx.MockTransport`` responses.
"""

from __future__ import annotations

import anyio
import httpx
import pytest

from loreline.health import (
    HealthReport,
    HealthStatus,
    classify_response,
    error_detail,
    missing_credential,
    probe_endpoint,
)
from loreline.models import ProviderKind


def _response(status: int, *, json: object = None, text: str = "") -> httpx.Response:
    request = httpx.Request("GET", "https://example.test/models")
    if json is not None:
        return httpx.Response(status, json=json, request=request)
    return httpx.Response(status, text=text, request=request)


# --- the live vendor answers, and what each one means ----------------------

# (label, response, expected status, expected detail fragment)
_LIVE_ANSWERS = [
    (
        "google-compat-bad-key",
        # GET .../v1beta/openai/models, Bearer <corrupted real key>. Google
        # spends 400 on this where everyone else spends 401, which is exactly
        # what made "< 500" report an invalid key as healthy.
        _response(400, json={"error": {"code": 400, "message": "Invalid Auth key."}}),
        HealthStatus.UNAUTHORIZED,
        "Invalid Auth key.",
    ),
    (
        "google-compat-fabricated-key",
        # Same route, a key that never existed: a different sentence again, so
        # matching one exact string would not have covered it.
        _response(
            400,
            json={
                "error": {
                    "code": 400,
                    "message": "Please pass a valid API key",
                    "status": "INVALID_ARGUMENT",
                }
            },
        ),
        HealthStatus.UNAUTHORIZED,
        "Please pass a valid API key",
    ),
    (
        "google-native-bad-key",
        # GET .../v1beta/models with x-goog-api-key. The native surface adds a
        # machine-readable reason the compat surface omits.
        _response(
            400,
            json={
                "error": {
                    "code": 400,
                    "message": "API key not valid. Please pass a valid API key.",
                    "status": "INVALID_ARGUMENT",
                    "details": [
                        {
                            "@type": "type.googleapis.com/google.rpc.ErrorInfo",
                            "reason": "API_KEY_INVALID",
                            "domain": "googleapis.com",
                        }
                    ],
                }
            },
        ),
        HealthStatus.UNAUTHORIZED,
        "API key not valid",
    ),
    (
        "google-native-no-key",
        _response(
            403,
            json={
                "error": {
                    "code": 403,
                    "message": (
                        "Method doesn't allow unregistered callers (callers without "
                        "established identity). Please use API Key or other form of API "
                        "consumer identity to call this API."
                    ),
                    "status": "PERMISSION_DENIED",
                }
            },
        ),
        HealthStatus.UNAUTHORIZED,
        "unregistered callers",
    ),
    (
        "openai-bad-key",
        _response(
            401,
            json={
                "error": {
                    "message": "Incorrect API key provided: sk-proj-****s000.",
                    "type": "invalid_request_error",
                    "code": "invalid_api_key",
                }
            },
        ),
        HealthStatus.UNAUTHORIZED,
        "Incorrect API key provided",
    ),
    (
        "assemblyai-bad-key",
        _response(401, json={"error": "Authentication error, API token missing/invalid"}),
        HealthStatus.UNAUTHORIZED,
        "Authentication error",
    ),
    (
        "deepgram-bad-key",
        # /v1/auth/token answers in plain text, so the detail has to come from
        # the raw body rather than from any JSON key.
        _response(401, text="Invalid credentials."),
        HealthStatus.UNAUTHORIZED,
        "Invalid credentials.",
    ),
    (
        "openai-models-ok",
        _response(200, json={"object": "list", "data": []}),
        HealthStatus.HEALTHY,
        None,
    ),
    (
        "wrong-base-url-on-a-live-host",
        # GET https://generativelanguage.googleapis.com/v1nope/openai/models:
        # a real host answering 404 with an empty body. Nothing about the
        # credential is wrong here, and saying "auth failed" would send the GM
        # to the wrong field.
        _response(404, text=""),
        HealthStatus.UNREACHABLE,
        None,
    ),
    (
        "rate-limited",
        _response(429, json={"error": {"message": "Rate limit exceeded"}}),
        HealthStatus.DEGRADED,
        "Rate limit exceeded",
    ),
    (
        "provider-outage",
        _response(503, text="upstream unavailable"),
        HealthStatus.DEGRADED,
        "upstream unavailable",
    ),
    (
        "a-400-that-is-not-about-the-key",
        # The reason a 400 cannot simply be read as "bad credential" either.
        _response(400, json={"error": {"message": "Unsupported value: 'limit'"}}),
        HealthStatus.UNKNOWN,
        "Unsupported value",
    ),
]


@pytest.mark.parametrize(("label", "response", "expected", "fragment"), _LIVE_ANSWERS)
def test_classify_live_vendor_answers(
    label: str, response: httpx.Response, expected: HealthStatus, fragment: str | None
) -> None:
    report = classify_response(response)
    assert report.status is expected, label
    if fragment is not None:
        assert report.detail is not None and fragment in report.detail, label


def test_healthy_is_the_only_status_that_reads_as_healthy() -> None:
    """Nothing but HEALTHY may satisfy a caller that only wants the bit.

    DEGRADED in particular: the credential is fine, but the provider cannot
    serve a session, and letting it pass as healthy would be the old bug in a
    new place.
    """
    assert HealthReport(HealthStatus.HEALTHY).healthy is True
    for status in HealthStatus:
        if status is not HealthStatus.HEALTHY:
            assert HealthReport(status).healthy is False


# --- probing, and the promise that a broken probe is never a verdict -------


async def test_probe_reports_a_dead_host_as_unreachable() -> None:
    def refuse(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("[Errno -2] Name or service not known")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(refuse), base_url="https://nope.test"
    ) as client:
        report = await probe_endpoint(client, "/models")

    assert report.status is HealthStatus.UNREACHABLE
    assert report.detail is not None
    assert "Name or service not known" in report.detail


async def test_probe_never_raises_and_never_calls_a_provider_broken() -> None:
    """An exception from inside the probe is our bug, not the provider's fault.

    It has to reach the route as UNKNOWN: raising would fail the whole
    "test all" fan-out, and reporting UNREACHABLE would accuse a provider that
    may well be fine.
    """

    def explode(_request: httpx.Request) -> httpx.Response:
        raise RuntimeError("bug in the probe")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(explode), base_url="https://example.test"
    ) as client:
        report = await probe_endpoint(client, "/models")

    assert report.status is HealthStatus.UNKNOWN


async def test_probe_is_bounded_even_when_the_client_is_not() -> None:
    """The button's deadline, not the connector's, is what bounds a probe.

    Connectors hand their transcription client to the probe, and that client's
    timeout is sized for uploading audio (60 s) or a long completion (120 s).
    One unresponsive provider must not hold the settings page for that long.
    """

    async def hang(_request: httpx.Request) -> httpx.Response:
        await anyio.sleep(30)
        raise AssertionError("should have been cancelled")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(hang), base_url="https://example.test", timeout=120.0
    ) as client:
        with anyio.fail_after(5):
            report = await probe_endpoint(client, "/models", timeout_s=0.05)

    assert report.status is HealthStatus.UNREACHABLE
    assert report.detail is not None and "no answer" in report.detail


# --- the keyless short circuit --------------------------------------------


def test_missing_credential_answers_a_cloud_kind_without_a_network_call() -> None:
    """Not only an optimisation. Google's OpenAI-compatible /models answers a
    keyless request with 404 "Requested entity was not found." (verified live),
    which is indistinguishable from a mistyped base URL, so a provider with no
    key at all would otherwise read as unreachable."""
    report = missing_credential(ProviderKind.GEMINI, None)
    assert report is not None
    assert report.status is HealthStatus.UNAUTHORIZED
    assert report.detail == "no API key stored for this provider"


def test_missing_credential_leaves_a_self_hosted_kind_to_the_endpoint() -> None:
    """``auth: optional`` in capabilities.yaml: an Ollama or LM Studio server
    may accept anonymous calls, so "no key" is not a verdict there."""
    assert missing_credential(ProviderKind.OPENAI_COMPAT, None) is None
    assert missing_credential(ProviderKind.GEMINI, "a-key") is None


# --- detail extraction ------------------------------------------------------


def test_error_detail_unwraps_googles_array_envelope() -> None:
    """The chat routes wrap the error in a one-element array where /models does
    not. Same vendor, same base URL, two shapes - so the unwrapper lives with
    the grading rather than being written twice."""
    request = httpx.Request("POST", "https://example.test/chat/completions")
    wrapped = httpx.Response(
        400,
        json=[{"error": {"code": 400, "message": "Invalid Auth key.", "status": "INVALID"}}],
        request=request,
    )
    assert error_detail(wrapped) == "Invalid Auth key."
    assert classify_response(wrapped).status is HealthStatus.UNAUTHORIZED


def test_error_detail_falls_back_to_the_raw_body_then_the_status_line() -> None:
    assert error_detail(_response(401, text="Invalid credentials.")) == "Invalid credentials."
    assert error_detail(_response(404, text="")) == "Not Found"
