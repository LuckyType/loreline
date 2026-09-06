"""Tests for the self-updater and autostart toggle (fake command runner)."""

from __future__ import annotations

import base64
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from loreline.updater import Autostart, Updater
from loreline.updater.autostart import AutostartToggleError, AutostartUnavailableError
from loreline.updater.process import CommandResult


class FakeRunner:
    """Records argv and returns a result from an injected handler."""

    def __init__(self, handler: Callable[[list[str]], CommandResult]) -> None:
        self.calls: list[list[str]] = []
        self._handler = handler

    async def __call__(self, argv: list[str], *, cwd: str | None = None) -> CommandResult:
        self.calls.append(argv)
        return self._handler(argv)


def _is_rev_parse(argv: list[str]) -> bool:
    return argv[:2] == ["git", "rev-parse"]


async def test_current_revision() -> None:
    runner = FakeRunner(lambda argv: CommandResult(0, "abc123\n", ""))
    updater = Updater(app_dir=Path("/app"), runner=runner)
    assert await updater.current_revision() == "abc123"


async def test_current_revision_failure() -> None:
    runner = FakeRunner(lambda argv: CommandResult(128, "", "not a repo"))
    updater = Updater(app_dir=Path("/app"), runner=runner)
    assert await updater.current_revision() is None


async def test_update_reports_commits() -> None:
    head = {"value": "old-sha"}

    def handle(argv: list[str]) -> CommandResult:
        if _is_rev_parse(argv):
            return CommandResult(0, head["value"] + "\n", "")
        if argv[0] == "bash":
            head["value"] = "new-sha"
            return CommandResult(0, "Update complete.", "")
        return CommandResult(0, "", "")

    runner = FakeRunner(handle)
    result = await Updater(app_dir=Path("/app"), runner=runner).update()
    assert result.ok
    assert result.previous_commit == "old-sha"
    assert result.new_commit == "new-sha"
    assert "Update complete." in result.output


async def test_update_refuses_in_container() -> None:
    runner = FakeRunner(lambda argv: CommandResult(0, "sha\n", ""))
    updater = Updater(app_dir=Path("/app"), runner=runner, in_container=True)

    result = await updater.update()

    assert not result.ok
    assert result.previous_commit == "sha"
    assert result.new_commit == "sha"
    assert "deploy/update.sh" in result.output
    # Never even tried to run the source-deployment script.
    assert not any(a[0] == "bash" for a in runner.calls)


async def test_rollback_refuses_in_container() -> None:
    runner = FakeRunner(lambda argv: CommandResult(0, "sha\n", ""))
    updater = Updater(app_dir=Path("/app"), runner=runner, in_container=True)

    result = await updater.rollback("deadbeef")

    assert not result.ok
    assert "deploy/update.sh" in result.output
    assert not any(a[:2] == ["git", "reset"] for a in runner.calls)


# --- Docker deployment: the WUD HTTP trigger ---------------------------------
# Two steps, in this order: POST /api/containers/watch to make WUD re-check the
# registry and report what it found, then POST the container's own trigger route
# if that report says an update exists. The second step is skipped when it
# doesn't, because WUD's docker trigger builds the tag to pull out of
# updateKind.remoteValue, which is undefined until an update is detected.

_WUD_IMAGE = "luckytype/loreline"
_WATCH_URL = "http://wud:3000/api/containers/watch"
_TRIGGER_URL = "http://wud:3000/api/containers/c0ffee/triggers/docker/local"


def _wud_container(*, update_available: bool = True, image: str = _WUD_IMAGE) -> dict[str, object]:
    """One entry of WUD's watch list, trimmed to the fields the updater reads."""
    return {
        "id": "c0ffee",
        "name": "loreline-app-1",
        "watcher": "local",
        "image": {"name": image, "tag": {"value": "latest"}},
        "updateAvailable": update_available,
    }


def _in_container_updater(
    handler: Callable[[httpx.Request], httpx.Response], *, password: str = "s3cret"
) -> tuple[Updater, FakeRunner]:
    """An updater that believes it's containerised, wired to a fake WUD."""
    runner = FakeRunner(lambda argv: CommandResult(0, "sha\n", ""))
    updater = Updater(
        app_dir=Path("/app"),
        runner=runner,
        in_container=True,
        wud_url="http://wud:3000",
        wud_user="loreline",
        wud_password=password,
        wud_image=_WUD_IMAGE,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    return updater, runner


def _wud_routes(
    seen: list[httpx.Request],
    *,
    containers: list[dict[str, object]] | None = None,
    trigger: Callable[[httpx.Request], httpx.Response] | None = None,
) -> Callable[[httpx.Request], httpx.Response]:
    """Answer the watch call with ``containers``, everything else with ``trigger``."""
    listed = [_wud_container()] if containers is None else containers

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/api/containers/watch":
            return httpx.Response(200, json=listed)
        return trigger(request) if trigger else httpx.Response(200)

    return handle


async def test_update_in_container_watches_then_triggers_wud() -> None:
    seen: list[httpx.Request] = []
    updater, runner = _in_container_updater(_wud_routes(seen))

    result = await updater.update()

    assert result.ok
    assert "WUD" in result.output
    # "triggered", never "complete": the container answering is the one being
    # replaced, so it cannot have watched the update finish.
    assert "complete" not in result.output.lower()
    # One line, so Settings > Client shows it as the message instead of burying
    # it in the output pane.
    assert "\n" not in result.output
    # Watch first, then the trigger - and the trigger is addressed by the id the
    # watch call just reported, never a remembered one.
    assert [str(r.url) for r in seen] == [_WATCH_URL, _TRIGGER_URL]
    assert [r.method for r in seen] == ["POST", "POST"]
    # WUD looks the container up itself, so the trigger carries no body.
    assert seen[1].content == b""
    # And still no attempt at the source-only script from inside a container.
    assert not any(a[0] == "bash" for a in runner.calls)


async def test_update_in_container_sends_basic_auth_on_both_calls() -> None:
    seen: list[httpx.Request] = []
    updater, _ = _in_container_updater(_wud_routes(seen))

    await updater.update()

    # HTTP Basic, not a bearer token: WUD holds only an htpasswd hash of the
    # password, so the plaintext travels on every request.
    expected = "Basic " + base64.b64encode(b"loreline:s3cret").decode()
    assert [r.headers["authorization"] for r in seen] == [expected, expected]


async def test_update_in_container_reports_up_to_date_without_triggering() -> None:
    """No update means no trigger: WUD would try to pull a tag named 'undefined'."""
    seen: list[httpx.Request] = []
    updater, _ = _in_container_updater(
        _wud_routes(seen, containers=[_wud_container(update_available=False)])
    )

    result = await updater.update()

    assert result.ok
    assert "up to date" in result.output.lower()
    assert "\n" not in result.output
    assert [str(r.url) for r in seen] == [_WATCH_URL]


async def test_update_in_container_treats_a_read_timeout_as_triggered() -> None:
    """The trigger recreates the caller, so it can't answer before killing it."""
    seen: list[httpx.Request] = []

    def times_out(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    updater, _ = _in_container_updater(_wud_routes(seen, trigger=times_out))
    result = await updater.update()

    assert result.ok
    assert "triggered" in result.output.lower()
    assert len(seen) == 2


async def test_update_in_container_falls_back_when_wud_is_absent() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    updater, _ = _in_container_updater(handle)
    result = await updater.update()

    assert not result.ok
    assert "deploy/update.sh" in result.output


async def test_update_in_container_reports_a_container_wud_does_not_watch() -> None:
    """WUD is up but the app lost its wud.watch label, or the image was renamed."""
    seen: list[httpx.Request] = []
    updater, _ = _in_container_updater(
        _wud_routes(seen, containers=[_wud_container(image="someone/else")])
    )

    result = await updater.update()

    assert not result.ok
    assert "deploy/update.sh" in result.output
    assert "\n" not in result.output
    # Nothing else was asked to update in its place.
    assert [str(r.url) for r in seen] == [_WATCH_URL]


async def test_update_in_container_without_a_password_never_calls_out() -> None:
    calls: list[httpx.Request] = []
    updater, _ = _in_container_updater(_wud_routes(calls), password="")

    result = await updater.update()

    assert not result.ok
    assert "deploy/update.sh" in result.output
    assert not calls


async def test_update_in_container_reports_rejected_credentials() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401)

    updater, _ = _in_container_updater(handle)
    result = await updater.update()

    assert not result.ok
    assert "LORELINE_WUD_PASSWORD" in result.output
    assert "WUD_AUTH_BASIC_LORELINE_HASH" in result.output
    assert "\n" not in result.output


async def test_update_in_container_reports_a_failed_trigger() -> None:
    """WUD answers 500 when its own pull or recreate raised."""
    seen: list[httpx.Request] = []
    updater, _ = _in_container_updater(
        _wud_routes(seen, trigger=lambda request: httpx.Response(500))
    )

    result = await updater.update()

    assert not result.ok
    assert "500" in result.output
    assert "deploy/update.sh" in result.output
    assert "\n" not in result.output


async def test_rollback_in_container_never_triggers_wud() -> None:
    """WUD only moves forward, and prune already deleted the old image."""
    calls: list[httpx.Request] = []
    updater, _ = _in_container_updater(_wud_routes(calls))

    result = await updater.rollback("deadbeef")

    assert not result.ok
    assert "deploy/update.sh" in result.output
    assert not calls


async def test_update_failure_captured() -> None:
    def handle(argv: list[str]) -> CommandResult:
        if argv[0] == "bash":
            return CommandResult(1, "", "boom")
        return CommandResult(0, "sha\n", "")

    result = await Updater(app_dir=Path("/app"), runner=FakeRunner(handle)).update()
    assert not result.ok
    assert result.returncode == 1
    assert "boom" in result.output


async def test_rollback_runs_sequence() -> None:
    def handle(argv: list[str]) -> CommandResult:
        return CommandResult(0, "sha\n" if _is_rev_parse(argv) else "", "")

    runner = FakeRunner(handle)
    result = await Updater(app_dir=Path("/app"), unit="loreline", runner=runner).rollback("dead")
    assert result.ok
    steps = [a for a in runner.calls if not _is_rev_parse(a)]
    assert steps[0] == ["git", "reset", "--hard", "dead"]
    assert steps[1][:2] == ["uv", "sync"]
    assert steps[2] == [
        "sudo",
        "systemd-run",
        "--on-active=2",
        "--unit=loreline-restart",
        "--collect",
        "systemctl",
        "restart",
        "loreline",
    ]


async def test_rollback_stops_on_failure() -> None:
    def handle(argv: list[str]) -> CommandResult:
        if argv[:2] == ["git", "reset"]:
            return CommandResult(1, "", "cannot reset")
        if _is_rev_parse(argv):
            return CommandResult(0, "sha\n", "")
        return CommandResult(0, "", "")

    runner = FakeRunner(handle)
    result = await Updater(app_dir=Path("/app"), runner=runner).rollback("x")
    assert not result.ok
    assert not any(a[:2] == ["uv", "sync"] for a in runner.calls)


async def test_autostart_toggle() -> None:
    state = {"enabled": False}

    def handle(argv: list[str]) -> CommandResult:
        if argv[:2] == ["systemctl", "is-enabled"]:
            text = "enabled\n" if state["enabled"] else "disabled\n"
            return CommandResult(0 if state["enabled"] else 1, text, "")
        if argv[:3] == ["sudo", "systemctl", "enable"]:
            state["enabled"] = True
            return CommandResult(0, "", "")
        if argv[:3] == ["sudo", "systemctl", "disable"]:
            state["enabled"] = False
            return CommandResult(0, "", "")
        return CommandResult(0, "", "")

    autostart = Autostart(unit="loreline", runner=FakeRunner(handle))
    assert await autostart.is_enabled() is False
    assert await autostart.set_enabled(True) is True
    assert await autostart.is_enabled() is True
    assert await autostart.set_enabled(False) is False


async def test_autostart_unavailable_in_container() -> None:
    def handle(argv: list[str]) -> CommandResult:
        raise AssertionError(f"should never run a command in a container: {argv}")

    autostart = Autostart(unit="loreline", runner=FakeRunner(handle), in_container=True)

    with pytest.raises(AutostartUnavailableError, match="Docker deployment"):
        await autostart.is_enabled()
    with pytest.raises(AutostartUnavailableError, match="Docker deployment"):
        await autostart.set_enabled(True)


async def test_autostart_toggle_failure() -> None:
    """The unit exists (is-enabled works) but the sudoers rule rejects the
    enable call - a real failure, distinct from autostart being unavailable
    on this platform, and distinct from the unit simply being disabled."""

    def handle(argv: list[str]) -> CommandResult:
        if argv[:2] == ["systemctl", "is-enabled"]:
            return CommandResult(1, "disabled\n", "")
        if argv[:3] == ["sudo", "systemctl", "enable"]:
            return CommandResult(1, "", "a password is required")
        return CommandResult(0, "", "")

    autostart = Autostart(unit="loreline", runner=FakeRunner(handle))

    with pytest.raises(AutostartToggleError, match="password"):
        await autostart.set_enabled(True)
