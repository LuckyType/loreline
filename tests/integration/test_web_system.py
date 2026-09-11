"""Integration tests for the M11 system routes (health/update/autostart/alerts).

A fake command runner stands in for git/systemctl, and a MockTransport stands in
for outbound alert HTTP, so nothing touches the real host.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

import loreline.updater.updater as updater_module
import loreline.web.routes.system as system_route
from loreline.health import HealthReport, HealthStatus
from loreline.llm import DEFAULT_SYSTEM_PROMPT
from loreline.settings import Settings
from loreline.updater.process import CommandResult
from loreline.web.app import create_app

# The two halves of the revision the UI shows, as git would answer them: the
# SHA from rev-parse, the readable name from describe. A table rather than two
# more branches below, which keeps the runner's one method readable.
_GIT_OUTPUT = {
    ("git", "rev-parse"): "commit-sha\n",
    ("git", "describe"): "v0.1.0-4-gcommit\n",
}


class FakeRunner:
    """Stand-in for git/systemctl with a togglable autostart state."""

    def __init__(self, *, previous: str | None = None) -> None:
        self.calls: list[list[str]] = []
        self.enabled = False
        # Where the reflog says HEAD was before it last moved. None for a
        # checkout that has never moved, where git exits non-zero for HEAD@{1}.
        self.previous = previous

    async def __call__(self, argv: list[str], *, cwd: str | None = None) -> CommandResult:
        self.calls.append(argv)
        if "HEAD@{1}" in argv:
            return self._reflog(argv)
        git_output = _GIT_OUTPUT.get((argv[0], argv[1] if len(argv) > 1 else ""))
        if git_output is not None:
            return CommandResult(0, git_output, "")
        if argv[0] == "bash":
            return CommandResult(0, "Update complete.", "")
        if argv[:2] == ["systemctl", "is-enabled"]:
            text = "enabled\n" if self.enabled else "disabled\n"
            return CommandResult(0 if self.enabled else 1, text, "")
        if argv[:2] == ["sudo", "systemctl"] and argv[2:3] in (["enable"], ["disable"]):
            self.enabled = argv[2] == "enable"
        return CommandResult(0, "", "")

    def _reflog(self, argv: list[str]) -> CommandResult:
        """What git says about HEAD@{1}: the SHA, its name, or a non-zero exit."""
        if self.previous is None:
            return CommandResult(128, "", "fatal: ambiguous argument 'HEAD@{1}'")
        text = self.previous if argv[1] == "rev-parse" else f"v0.1.0-2-g{self.previous[:7]}"
        return CommandResult(0, text + "\n", "")


@pytest.fixture
def alert_requests() -> list[httpx.Request]:
    return []


@pytest_asyncio.fixture
async def client(
    settings: Settings, alert_requests: list[httpx.Request]
) -> AsyncIterator[AsyncClient]:
    def handle(request: httpx.Request) -> httpx.Response:
        alert_requests.append(request)
        return httpx.Response(200)

    def alert_client_factory() -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(handle))

    app = create_app(
        settings, command_runner=FakeRunner(), alert_client_factory=alert_client_factory
    )
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac


async def test_healthz_extended(client: AsyncClient) -> None:
    body = (await client.get("/api/system/healthz")).json()
    assert body["status"] in {"ok", "degraded"}
    assert body["capture_status"] == "idle"
    assert body["disk_total_bytes"] > 0
    assert body["alerts_enabled"] is False
    # No diarization endpoint configured -> nothing probed, nothing claimed.
    assert body["diarizer_endpoint"] is None
    assert body["diarizer_reachable"] is None


async def test_healthz_reports_the_diarizers_graded_verdict(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Once an endpoint is configured, health says whether it actually answers,
    graded like a provider row rather than as a bool: a service answering 503
    while its model loads is reachable and degraded, not absent."""
    probed: list[str] = []
    verdict = HealthReport(HealthStatus.HEALTHY)

    async def fake_probe(endpoint: str, **_kwargs: object) -> HealthReport:
        probed.append(endpoint)
        return verdict

    monkeypatch.setattr(system_route, "probe_diarizer", fake_probe)
    monkeypatch.setattr(system_route, "_diarizer_probe", None)  # drop any cached verdict

    await client.put(
        "/api/system/defaults",
        json={
            "stt_model": "",
            "diar_mode": "remote",
            "diar_endpoint": "http://diarization:8001",
            "summarize_model": "",
        },
    )

    body = (await client.get("/api/system/healthz")).json()
    assert body["diarizer_endpoint"] == "http://diarization:8001"
    assert body["diarizer_reachable"] is True
    assert body["diarizer_status"] == "healthy"
    assert body["diarizer_detail"] is None
    assert probed == ["http://diarization:8001"]

    # Polled again straight away: served from cache, not re-probed - /healthz is
    # hit every few seconds by the UI.
    await client.get("/api/system/healthz")
    assert probed == ["http://diarization:8001"]

    # A service that answered badly is still one that answered; only a host
    # that gave nothing (or a URL no diarizer lives at) is unreachable.
    monkeypatch.setattr(system_route, "_diarizer_probe", None)
    verdict = HealthReport(HealthStatus.DEGRADED, "model loading")
    body = (await client.get("/api/system/healthz")).json()
    assert body["diarizer_reachable"] is True
    assert body["diarizer_status"] == "degraded"
    assert body["diarizer_detail"] == "model loading"

    monkeypatch.setattr(system_route, "_diarizer_probe", None)
    verdict = HealthReport(HealthStatus.UNREACHABLE, "could not connect: refused")
    body = (await client.get("/api/system/healthz")).json()
    assert body["diarizer_reachable"] is False
    assert body["diarizer_status"] == "unreachable"


async def test_diarizer_probe_checks_the_given_endpoint_not_the_stored_default(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The capture panel probes whatever is currently typed in, which may
    differ from (or not yet be) the saved default - /healthz only ever grades
    ``defaults.diar_endpoint`` and would miss this value entirely."""
    probed: list[str] = []

    async def fake_probe(endpoint: str, **_kwargs: object) -> HealthReport:
        probed.append(endpoint)
        return HealthReport(HealthStatus.DEGRADED, "model loading")

    monkeypatch.setattr(system_route, "probe_diarizer", fake_probe)

    # No stored default configured at all, unlike the healthz test above.
    resp = await client.get(
        "/api/system/diarizer/probe", params={"endpoint": "http://typed-in:9000"}
    )
    assert resp.status_code == 200
    body = resp.json()
    # Degraded still answered: reachable is everything but unreachable.
    assert body["reachable"] is True
    assert body["status"] == "degraded"
    assert body["detail"] == "model loading"
    assert probed == ["http://typed-in:9000"]


async def test_diarizer_probe_is_uncached_unlike_the_stored_defaults_probe(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``_diarizer_status`` caches the stored default for the /healthz poll;
    this route answers a one-off question about a value that can change on
    every keystroke, so probing the same endpoint twice must not be served
    from that (or any) cache."""
    probed: list[str] = []

    async def fake_probe(endpoint: str, **_kwargs: object) -> HealthReport:
        probed.append(endpoint)
        return HealthReport(HealthStatus.HEALTHY)

    monkeypatch.setattr(system_route, "probe_diarizer", fake_probe)

    endpoint = "http://same:8001"
    await client.get("/api/system/diarizer/probe", params={"endpoint": endpoint})
    await client.get("/api/system/diarizer/probe", params={"endpoint": endpoint})
    assert probed == [endpoint, endpoint]


async def test_revision(client: AsyncClient) -> None:
    """Both halves: the SHA /rollback takes back, and the name a person reads."""
    body = (await client.get("/api/system/revision")).json()
    assert body["commit"] == "commit-sha"
    assert body["described"] == "v0.1.0-4-gcommit"
    # A checkout that has never been updated has nowhere to roll back to, and
    # that is not a reason it cannot: the page hides the button, nothing more.
    assert body["previous_commit"] is None
    assert body["previous_described"] is None
    assert body["rollback_unavailable"] is None


async def test_revision_names_the_commit_to_roll_back_to(settings: Settings) -> None:
    """Once the checkout has moved, the response carries where it moved from."""
    app = create_app(settings, command_runner=FakeRunner(previous="abc1234def"))
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            body = (await ac.get("/api/system/revision")).json()

    assert body["commit"] == "commit-sha"
    assert body["previous_commit"] == "abc1234def"
    assert body["previous_described"] == "v0.1.0-2-gabc1234"
    assert body["rollback_unavailable"] is None


async def test_revision_in_a_container_with_nothing_baked(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A Docker image built with no --build-arg: an unknown revision, not an error.

    Worth an endpoint test rather than only a unit one, because "unknown" is the
    case the UI used to be stuck in and the route has to keep answering 200 for
    it. Note the runner never sees a git call: in a container git cannot work at
    all, so current_revision does not spend a subprocess proving it again.
    """
    marker = tmp_path / "dockerenv"
    marker.touch()
    monkeypatch.setattr(updater_module, "_DOCKER_MARKER", marker)
    runner = FakeRunner()
    app = create_app(settings, command_runner=runner)
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            response = await ac.get("/api/system/revision")

    assert response.status_code == 200
    body = response.json()
    assert body["commit"] is None
    assert body["described"] is None
    assert body["previous_commit"] is None
    assert body["previous_described"] is None
    # A container cannot roll back however many updates it has seen, and the
    # page shows this sentence in place of the offer rather than a dead button.
    assert "Docker deployment" in body["rollback_unavailable"]
    assert not any(call[0] == "git" for call in runner.calls)


async def test_update(client: AsyncClient) -> None:
    body = (await client.post("/api/system/update")).json()
    assert body["ok"] is True
    assert body["new_commit"] == "commit-sha"
    assert "Update complete." in body["output"]


async def test_rollback(client: AsyncClient) -> None:
    body = (await client.post("/api/system/rollback", json={"commit": "deadbeef"})).json()
    assert body["ok"] is True


async def test_rollback_rejects_non_commit_looking_input(client: AsyncClient) -> None:
    # Guards against argument injection into `git reset --hard <commit>`: a
    # value starting with "-" (or containing non-hex chars) must never reach
    # the subprocess call as a bare argv element.
    resp = await client.post("/api/system/rollback", json={"commit": "--upload-pack=x"})
    assert resp.status_code == 422


async def test_autostart_toggle(client: AsyncClient) -> None:
    assert (await client.get("/api/system/autostart")).json()["enabled"] is False
    put = await client.put("/api/system/autostart", json={"enabled": True})
    assert put.json()["enabled"] is True
    assert (await client.get("/api/system/autostart")).json()["enabled"] is True


async def test_autostart_toggle_failure_reports_conflict(settings: Settings) -> None:
    """A real toggle failure (e.g. the sudoers rule rejecting the call) must
    surface as an HTTP error, not as a silent False - see AutostartToggleError
    in loreline.updater.autostart."""

    class FailingRunner:
        async def __call__(self, argv: list[str], *, cwd: str | None = None) -> CommandResult:
            if argv[:2] == ["systemctl", "is-enabled"]:
                return CommandResult(1, "disabled\n", "")
            if argv[:3] == ["sudo", "systemctl", "enable"]:
                return CommandResult(1, "", "a password is required")
            return CommandResult(0, "", "")

    app = create_app(settings, command_runner=FailingRunner())
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.put("/api/system/autostart", json={"enabled": True})
            assert resp.status_code == 409


async def test_action_defaults_roundtrip(client: AsyncClient) -> None:
    empty = (await client.get("/api/system/defaults")).json()
    assert empty == {
        "stt_provider": "",
        "stt_model": "",
        "diar_mode": "",
        "diar_endpoint": "",
        "summarize_provider": "",
        "summarize_model": "",
        # Never-saved prompt is served as the concrete built-in text, so the
        # settings UI always shows editable instructions.
        "summarize_prompt": DEFAULT_SYSTEM_PROMPT,
        "video_provider": "",
        "video_model": "",
        "summarize_reasoning_effort": "",
        "strict_model_filtering": True,
    }

    put = await client.put(
        "/api/system/defaults",
        json={
            "stt_provider": "prov-stt",
            "stt_model": "nova-3",
            "diar_mode": "remote",
            "diar_endpoint": "http://diarizer:8001",
            "summarize_provider": "prov-llm",
            "summarize_model": "gpt-4o-mini",
        },
    )
    assert put.status_code == 200
    fetched = (await client.get("/api/system/defaults")).json()
    assert fetched["stt_provider"] == "prov-stt"
    assert fetched["stt_model"] == "nova-3"
    assert fetched["diar_mode"] == "remote"
    assert fetched["diar_endpoint"] == "http://diarizer:8001"
    assert fetched["summarize_provider"] == "prov-llm"
    assert fetched["summarize_model"] == "gpt-4o-mini"


async def test_summarize_prompt_default_tracking_and_reset(client: AsyncClient) -> None:
    """Saving the built-in text (or blank) stores nothing, so untouched setups
    keep tracking future default improvements; a custom prompt round-trips; and
    clearing the field resets to the built-in default."""
    saved = await client.put("/api/system/defaults", json={"summarize_prompt": "Nur Stichpunkte."})
    assert saved.json()["summarize_prompt"] == "Nur Stichpunkte."
    assert (await client.get("/api/system/defaults")).json()["summarize_prompt"] == (
        "Nur Stichpunkte."
    )

    # Clearing the field is the reset gesture: served (and echoed) as the default.
    reset = await client.put("/api/system/defaults", json={"summarize_prompt": ""})
    assert reset.json()["summarize_prompt"] == DEFAULT_SYSTEM_PROMPT
    fetched = (await client.get("/api/system/defaults")).json()
    assert fetched["summarize_prompt"] == DEFAULT_SYSTEM_PROMPT

    # Saving the served default text verbatim also stores blank (tracks the built-in).
    await client.put("/api/system/defaults", json={"summarize_prompt": DEFAULT_SYSTEM_PROMPT})
    assert (await client.get("/api/system/defaults")).json()["summarize_prompt"] == (
        DEFAULT_SYSTEM_PROMPT
    )


async def test_alert_channels_crud_and_test(
    client: AsyncClient, alert_requests: list[httpx.Request]
) -> None:
    assert (await client.get("/api/system/alerts/channels")).json() == []

    created = await client.post(
        "/api/system/alerts/channels",
        json={
            "type": "telegram",
            "enabled": True,
            "min_level": "warning",
            "chat_id": "5",
            "token": "bot-token",
        },
    )
    assert created.status_code == 201
    channel = created.json()
    assert channel["type"] == "telegram"
    assert channel["token_set"] is True
    assert "token" not in channel  # secret never returned
    channel_id = channel["id"]

    listed = (await client.get("/api/system/alerts/channels")).json()
    assert len(listed) == 1
    assert listed[0]["chat_id"] == "5"
    # health reflects that at least one channel is enabled
    assert (await client.get("/api/system/healthz")).json()["alerts_enabled"] is True

    # update: disable + raise the gate; omitting the token keeps the stored one
    updated = await client.put(
        f"/api/system/alerts/channels/{channel_id}",
        json={"type": "telegram", "enabled": False, "min_level": "error", "chat_id": "5"},
    )
    assert updated.json()["enabled"] is False
    assert updated.json()["token_set"] is True

    result = (await client.post(f"/api/system/alerts/channels/{channel_id}/test")).json()
    assert result["ok"] is True
    assert result["detail"] is None  # nothing to explain when it worked
    assert len(alert_requests) >= 1

    assert (await client.delete(f"/api/system/alerts/channels/{channel_id}")).status_code == 200
    assert (await client.get("/api/system/alerts/channels")).json() == []
    assert (await client.get("/api/system/healthz")).json()["alerts_enabled"] is False
    assert (await client.delete(f"/api/system/alerts/channels/{channel_id}")).status_code == 404


async def test_ops_endpoints_require_auth(tmp_path: Path) -> None:
    secured = Settings(data_dir=tmp_path / "data", auth_password="secret", jwt_secret="k")
    app = create_app(secured, command_runner=FakeRunner())
    async with (
        LifespanManager(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac,
    ):
        assert (await ac.post("/api/system/update")).status_code == 401
        assert (await ac.get("/api/system/autostart")).status_code == 401
        # naming an endpoint makes the server issue an outbound request, same
        # as the stored-default probe behind healthz - authed like every other
        # mutating-adjacent route here, not left open like healthz itself.
        assert (
            await ac.get("/api/system/diarizer/probe", params={"endpoint": "http://x"})
        ).status_code == 401
        # The snapshot is behind the cookie too: version, free disk, capture
        # state, the diarizer endpoint and the STT vendor's error text are not
        # for anyone who can reach the port.
        assert (await ac.get("/api/system/healthz")).status_code == 401
        # What stays open for external pollers is liveness, and only that.
        assert (await ac.get("/api/system/livez")).status_code == 200


async def test_webhook_channel_needs_a_real_url(client: AsyncClient) -> None:
    """A webhook that is not a URL is refused by the API, client or no client.

    The bug this closes: `not-a-url` saved, sat in the table looking configured
    and delivered nothing, and nothing on the page ever said so.
    """
    rejected = await client.post(
        "/api/system/alerts/channels",
        json={"type": "webhook", "url": "not-a-url"},
    )
    assert rejected.status_code == 422
    error = rejected.json()["detail"][0]
    assert error["loc"] == ["body", "url"]  # the message names the field
    assert "absolute http:// or https:// URL" in error["msg"]
    assert (await client.get("/api/system/alerts/channels")).json() == []

    # A webhook with no URL at all is the same broken row by another route.
    assert (
        await client.post("/api/system/alerts/channels", json={"type": "webhook"})
    ).status_code == 422

    # ...and a real one still saves, unchanged.
    created = await client.post(
        "/api/system/alerts/channels",
        json={"type": "webhook", "url": "https://hooks.example/loreline", "min_level": "error"},
    )
    assert created.status_code == 201
    assert created.json()["url"] == "https://hooks.example/loreline"
    assert created.json()["min_level"] == "error"


async def test_ntfy_channel_needs_a_real_server(client: AsyncClient) -> None:
    """Same check on the other URL-shaped field, whose default hides it."""
    rejected = await client.post(
        "/api/system/alerts/channels",
        json={"type": "ntfy", "server": "ntfy.example", "topic": "loreline"},
    )
    assert rejected.status_code == 422
    error = rejected.json()["detail"][0]
    assert error["loc"] == ["body", "server"]
    assert "absolute http:// or https:// URL" in error["msg"]

    ok = await client.post(
        "/api/system/alerts/channels",
        json={"type": "ntfy", "server": "https://ntfy.example", "topic": "loreline"},
    )
    assert ok.status_code == 201
    assert ok.json()["server"] == "https://ntfy.example"

    # A Telegram channel carries the ntfy default in `server` and never uses
    # it, so the check must not fire on a field that channel type ignores.
    assert (
        await client.post("/api/system/alerts/channels", json={"type": "telegram", "chat_id": "5"})
    ).status_code == 201


async def test_failed_test_says_why_without_leaking_the_token(settings: Settings) -> None:
    """The test route hands back the reason, scrubbed of the channel's token.

    Telegram carries the bot token in the request path, and a vendor that
    echoes the request back would otherwise put it in the UI and the log.
    """
    token = "1234567:AA-super-secret-bot-token"

    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"description": f"Unauthorized: {request.url}"})

    app = create_app(
        settings,
        command_runner=FakeRunner(),
        alert_client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handle)),
    )
    async with (
        LifespanManager(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac,
    ):
        created = await ac.post(
            "/api/system/alerts/channels",
            json={"type": "telegram", "chat_id": "5", "token": token},
        )
        channel_id = created.json()["id"]
        result = (await ac.post(f"/api/system/alerts/channels/{channel_id}/test")).json()

    assert result["ok"] is False
    assert result["detail"].startswith("HTTP 401")
    assert token not in result["detail"]
    assert "/bot***" in result["detail"]
