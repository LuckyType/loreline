"""The one health probe, kind by kind, at its entry point.

Every provider row is asked one question, does this key work at the surface
capabilities.yaml declares for it, and answered as a HealthReport without a
connector being built. The HTTP kinds are asked through an
``httpx.MockTransport`` under the client the probe builds itself, so the URL
and the credential header it sends are what these tests see; the socket kinds
are asked against the mock servers in ``mocks/``. How an answer is graded is
pinned in test_provider_health.py; this file pins which surface each kind is
asked at, how the credential is spelled there, and what is sent first.
"""

from __future__ import annotations

import json
from collections.abc import Callable

import httpx
import pytest
from websockets.asyncio.server import ServerConnection, serve
from websockets.http11 import Request, Response

import loreline.stt.backends._ws as ws_helpers
from loreline.health import HealthStatus
from loreline.health_probe import probe_provider, probe_surface, probe_target
from loreline.models import Interaction, ProviderConfig, ProviderKind
from mocks.assemblyai_ws import assemblyai_handler
from mocks.deepgram_ws import deepgram_handler
from mocks.gemini_live_ws import gemini_live_handler


def _row(kind: ProviderKind, base_url: str | None = None) -> ProviderConfig:
    return ProviderConfig(id="p1", name=kind.value, kind=kind, base_url=base_url)


class _Seen:
    """What the probe put on the wire, and a canned answer for it."""

    def __init__(self, response: httpx.Response | None = None) -> None:
        self.requests: list[httpx.Request] = []
        self._response = response or httpx.Response(200, json={"data": []})

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._response

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)

    @property
    def only(self) -> httpx.Request:
        assert len(self.requests) == 1, self.requests
        return self.requests[0]


# --- which surface answers for a kind -------------------------------------


class TestProbeTarget:
    @pytest.mark.parametrize(
        "kind",
        [
            ProviderKind.OPENAI,
            ProviderKind.OPENROUTER,
            ProviderKind.GEMINI,
            ProviderKind.OPENAI_COMPAT,
        ],
    )
    def test_a_summarizing_kind_is_asked_on_its_chat_surface(self, kind: ProviderKind) -> None:
        """One probe per row. The key is the same credential on every surface
        of a row, so a second probe against the transcription surface would
        learn nothing, and for Gemini it would need a second client with a
        different auth header to ask."""
        assert probe_target(kind) == (Interaction.SUMMARIZE, None)

    @pytest.mark.parametrize("kind", [ProviderKind.DEEPGRAM, ProviderKind.ASSEMBLYAI])
    def test_a_streaming_kind_is_asked_on_its_socket(self, kind: ProviderKind) -> None:
        """Their default models stream, so the socket is the surface a session
        would actually use."""
        assert probe_target(kind) == (Interaction.TRANSCRIBE, "realtime")


# --- the HTTP kinds ----------------------------------------------------------


async def test_openai_is_asked_for_its_models_with_a_bearer_token() -> None:
    seen = _Seen(
        httpx.Response(
            401,
            json={
                "error": {
                    "message": "Incorrect API key provided: sk-proj-****s000.",
                    "type": "invalid_request_error",
                    "code": "invalid_api_key",
                }
            },
        )
    )
    report = await probe_provider(
        _row(ProviderKind.OPENAI), "sk-bad", http_transport=seen.transport
    )

    assert str(seen.only.url) == "https://api.openai.com/v1/models"
    assert seen.only.headers["authorization"] == "Bearer sk-bad"
    assert report.status is HealthStatus.UNAUTHORIZED
    assert report.detail is not None and "Incorrect API key" in report.detail


async def test_openrouter_is_asked_about_the_key_not_the_catalogue() -> None:
    """OpenRouter serves /models to anonymous callers (verified live: 425 models
    with no Authorization header at all), so grading it would call any key
    healthy. Its surfaces declare /key, which describes the calling key and
    401s without one, and the attribution headers ride along."""
    seen = _Seen(httpx.Response(200, json={"data": {"label": "test", "usage": 0}}))
    report = await probe_provider(
        _row(ProviderKind.OPENROUTER), "sk-or-v1-x", http_transport=seen.transport
    )

    assert str(seen.only.url) == "https://openrouter.ai/api/v1/key"
    assert seen.only.headers["authorization"] == "Bearer sk-or-v1-x"
    assert seen.only.headers["x-title"] == "Loreline"
    assert report.status is HealthStatus.HEALTHY


async def test_gemini_is_asked_on_its_chat_shim_and_its_400_reads_as_a_bad_key() -> None:
    """Google's OpenAI-compatible shim is a sibling path of the native base with
    Bearer auth, and it spends 400 on a bad key where everyone else spends
    401: the answer that made ``< 500`` report an invalid key as healthy."""
    seen = _Seen(httpx.Response(400, json={"error": {"code": 400, "message": "Invalid Auth key."}}))
    report = await probe_provider(_row(ProviderKind.GEMINI), "bad", http_transport=seen.transport)

    assert str(seen.only.url) == "https://generativelanguage.googleapis.com/v1beta/openai/models"
    assert seen.only.headers["authorization"] == "Bearer bad"
    assert "x-goog-api-key" not in seen.only.headers
    assert report.status is HealthStatus.UNAUTHORIZED
    assert report.detail == "Invalid Auth key."


async def test_a_self_hosted_row_is_asked_at_its_own_base_url_with_or_without_a_key() -> None:
    """``auth: optional``: an Ollama or LM Studio server may check no key, so
    "no key" is not a verdict there and the endpoint gets to answer."""
    seen = _Seen()
    row = _row(ProviderKind.OPENAI_COMPAT, base_url="http://llm:1234/v1")
    report = await probe_provider(row, None, http_transport=seen.transport)

    assert str(seen.only.url) == "http://llm:1234/v1/models"
    assert "authorization" not in seen.only.headers
    assert report.status is HealthStatus.HEALTHY


async def test_a_self_hosted_row_without_a_base_url_is_unknown_not_down() -> None:
    """Nothing was probed, so nothing is known about a server; the message
    names the missing base URL, which is what the GM has to fix."""
    report = await probe_provider(_row(ProviderKind.OPENAI_COMPAT), "k")
    assert report.status is HealthStatus.UNKNOWN
    assert report.detail is not None and "base URL" in report.detail


async def test_a_cloud_kind_without_a_key_is_answered_without_a_call() -> None:
    """Google's compat /models answers a keyless request with 404 (verified
    live), which a probe would read as a wrong base URL. So the probe does not
    ask."""

    def never(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("a keyless cloud row must not be probed")

    report = await probe_provider(
        _row(ProviderKind.GEMINI), None, http_transport=httpx.MockTransport(never)
    )
    assert report.status is HealthStatus.UNAUTHORIZED
    assert report.detail == "no API key stored for this provider"


async def test_a_rate_limit_is_a_working_credential() -> None:
    """Being throttled means the key was recognised; the provider just cannot
    serve a session right now."""
    seen = _Seen(httpx.Response(429, json={"error": {"message": "Rate limit exceeded"}}))
    report = await probe_provider(_row(ProviderKind.OPENAI), "k", http_transport=seen.transport)
    assert report.status is HealthStatus.DEGRADED
    assert report.detail == "Rate limit exceeded"


async def test_a_dead_host_is_unreachable() -> None:
    def refuse(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("[Errno 111] Connection refused")

    row = _row(ProviderKind.OPENAI_COMPAT, base_url="http://llm:1234/v1")
    report = await probe_provider(row, "k", http_transport=httpx.MockTransport(refuse))
    assert report.status is HealthStatus.UNREACHABLE
    assert report.detail is not None and "Connection refused" in report.detail


# --- the socket kinds ---------------------------------------------------------

_Handshakes = list[Request]


def _capture(handshakes: _Handshakes) -> Callable[[ServerConnection, Request], Response | None]:
    def process_request(_connection: ServerConnection, request: Request) -> Response | None:
        handshakes.append(request)
        return None

    return process_request


async def test_deepgram_opens_its_socket_with_a_token_header() -> None:
    """The socket, not the batch API, because nova-3 streams; and the key is
    spelled ``Token``, never ``Bearer``, which Deepgram would reject exactly
    like a bad key."""
    handshakes: _Handshakes = []

    async with serve(
        deepgram_handler, "127.0.0.1", 0, process_request=_capture(handshakes)
    ) as server:
        port = server.sockets[0].getsockname()[1]
        report = await probe_provider(
            _row(ProviderKind.DEEPGRAM, f"ws://127.0.0.1:{port}"), "secret"
        )

    assert report.status is HealthStatus.HEALTHY
    assert handshakes[0].headers["Authorization"] == "Token secret"


async def test_deepgram_is_told_to_close_the_stream_first() -> None:
    """``CloseStream`` is the surface's declared first frame (capabilities.yaml,
    not connector code): it makes Deepgram answer and hang up at once instead
    of waiting for audio that never comes."""
    frames: list[object] = []

    async def recording(ws: ServerConnection) -> None:
        frames.append(json.loads(await ws.recv()))
        await ws.send(json.dumps({"type": "Metadata", "duration": 0.0}))

    async with serve(recording, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        report = await probe_provider(
            _row(ProviderKind.DEEPGRAM, f"ws://127.0.0.1:{port}"), "secret"
        )

    assert report.status is HealthStatus.HEALTHY
    assert frames == [{"type": "CloseStream"}]


async def test_assemblyai_reads_the_greeting_without_speaking_first() -> None:
    """AssemblyAI greets with Begin as soon as the socket opens, so the surface
    declares no frame and the probe reads the greeting. The bare key is the
    whole Authorization header there."""
    handshakes: _Handshakes = []

    async with serve(
        assemblyai_handler, "127.0.0.1", 0, process_request=_capture(handshakes)
    ) as server:
        port = server.sockets[0].getsockname()[1]
        report = await probe_provider(
            _row(ProviderKind.ASSEMBLYAI, f"ws://127.0.0.1:{port}"), "secret"
        )

    assert report.status is HealthStatus.HEALTHY
    assert handshakes[0].headers["Authorization"] == "secret"


async def test_a_rejected_upgrade_reads_as_an_auth_failure() -> None:
    """A bad key never opens the socket: the vendor rejects the HTTP upgrade,
    and websockets hands back the response, which is the only reason a socket
    kind can tell a wrong key from a wrong host at all. Deepgram's real answer
    is 401 with this body."""

    async def reject(connection: ServerConnection, _request: Request) -> Response:
        return connection.respond(
            401,
            '{"category":"UNAUTHORIZED","message":"Authentication failed.",'
            '"details":"Check that you are using the correct credentials."}',
        )

    async def handler(ws: ServerConnection) -> None:  # pragma: no cover - never reached
        await ws.wait_closed()

    async with serve(handler, "127.0.0.1", 0, process_request=reject) as server:
        port = server.sockets[0].getsockname()[1]
        report = await probe_provider(_row(ProviderKind.DEEPGRAM, f"ws://127.0.0.1:{port}"), "bad")

    assert report.status is HealthStatus.UNAUTHORIZED
    # The sentence, not the raw JSON: the badge tooltip shows this verbatim.
    assert report.detail == "Authentication failed."


@pytest.mark.parametrize("kind", [ProviderKind.DEEPGRAM, ProviderKind.ASSEMBLYAI])
async def test_nothing_listening_is_unreachable_not_unauthorized(kind: ProviderKind) -> None:
    """Port 1 refuses the connection. That is the base URL being wrong, which is
    a different fix from a rejected key, so the two must not share a badge."""
    report = await probe_provider(_row(kind, "ws://127.0.0.1:1"), "x")
    assert report.status is HealthStatus.UNREACHABLE
    assert report.detail is not None


async def test_gemini_live_carries_the_key_in_the_query_and_a_quiet_session_is_healthy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Live socket authenticates with ``?key=`` rather than a header, which
    the surface says and the probe spells; and a session that opens and then
    waits for audio has said everything a probe needs to hear. The per-kind
    choice asks Gemini on its chat surface, so this is the surface asked
    directly."""
    handshakes: _Handshakes = []
    monkeypatch.setattr(ws_helpers, "SOCKET_READ_TIMEOUT_S", 0.1)

    async with serve(
        gemini_live_handler, "127.0.0.1", 0, process_request=_capture(handshakes)
    ) as server:
        port = server.sockets[0].getsockname()[1]
        report = await probe_surface(
            _row(ProviderKind.GEMINI, f"ws://127.0.0.1:{port}"),
            "secret",
            Interaction.TRANSCRIBE,
            "realtime",
        )

    assert report.status is HealthStatus.HEALTHY
    assert handshakes[0].path.endswith("?key=secret")
    assert "Authorization" not in handshakes[0].headers


# --- the surfaces the per-kind choice does not ask, declared truthfully -----


async def test_deepgram_batch_asks_what_the_key_is_not_what_the_models_are() -> None:
    """Verified live: Deepgram serves /v1/models to anonymous callers, 200 and
    the full catalogue with no Authorization header, so its batch surface
    declares /v1/auth/token, which answers a bogus key 401 in plain text."""
    seen = _Seen(httpx.Response(401, text="Invalid credentials."))
    report = await probe_surface(
        _row(ProviderKind.DEEPGRAM),
        "bad",
        Interaction.TRANSCRIBE,
        "batch",
        http_transport=seen.transport,
    )

    assert str(seen.only.url) == "https://api.deepgram.com/v1/auth/token"
    assert seen.only.headers["authorization"] == "Token bad"
    assert report.status is HealthStatus.UNAUTHORIZED
    assert report.detail == "Invalid credentials."


async def test_assemblyai_batch_lists_one_transcript() -> None:
    """There is no model list to ask: AssemblyAI's only /models route serves its
    LLM gateway. Listing one transcript exercises the credential, and a bogus
    key answers 401 with the sentence the GM should see."""
    seen = _Seen(
        httpx.Response(401, json={"error": "Authentication error, API token missing/invalid"})
    )
    report = await probe_surface(
        _row(ProviderKind.ASSEMBLYAI),
        "bad",
        Interaction.TRANSCRIBE,
        "batch",
        http_transport=seen.transport,
    )

    assert str(seen.only.url) == "https://api.assemblyai.com/v2/transcript?limit=1"
    assert seen.only.headers["authorization"] == "bad"
    assert report.status is HealthStatus.UNAUTHORIZED
    assert report.detail == "Authentication error, API token missing/invalid"


async def test_gemini_native_base_reads_googles_400_as_a_bad_key() -> None:
    """The native surface takes ``x-goog-api-key`` and grades unlike the chat
    shim: a bad key is 400 with a machine-readable API_KEY_INVALID reason, no
    key at all 403 PERMISSION_DENIED (both pinned from live calls). Neither is
    evidence of health, which is what a status threshold got wrong."""
    bad_key = {
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
    }
    seen = _Seen(httpx.Response(400, json=bad_key))
    report = await probe_surface(
        _row(ProviderKind.GEMINI),
        "bad",
        Interaction.TRANSCRIBE,
        "batch",
        http_transport=seen.transport,
    )

    assert str(seen.only.url) == "https://generativelanguage.googleapis.com/v1beta/models"
    assert seen.only.headers["x-goog-api-key"] == "bad"
    assert "authorization" not in seen.only.headers
    assert report.status is HealthStatus.UNAUTHORIZED
    assert report.detail == "API key not valid. Please pass a valid API key."

    no_key = {
        "error": {
            "code": 403,
            "message": (
                "Method doesn't allow unregistered callers (callers without established identity)."
            ),
            "status": "PERMISSION_DENIED",
        }
    }
    seen = _Seen(httpx.Response(403, json=no_key))
    report = await probe_surface(
        _row(ProviderKind.GEMINI),
        "bad",
        Interaction.TRANSCRIBE,
        "batch",
        http_transport=seen.transport,
    )
    assert report.status is HealthStatus.UNAUTHORIZED


async def test_openrouter_transcription_surface_asks_the_key_route_too() -> None:
    """The batch transcription surface reuses the OpenAI-compatible wire format
    but must not be probed with /models, for the same reason as its chat
    surface."""
    seen = _Seen(httpx.Response(200, json={"data": {"label": "test"}}))
    report = await probe_surface(
        _row(ProviderKind.OPENROUTER),
        "k",
        Interaction.TRANSCRIBE,
        "batch",
        http_transport=seen.transport,
    )
    assert str(seen.only.url) == "https://openrouter.ai/api/v1/key"
    assert seen.only.headers["http-referer"] == "https://github.com/LuckyType/loreline"
    assert report.status is HealthStatus.HEALTHY
