"""Tests for the self-updater and autostart toggle (fake command runner)."""

from __future__ import annotations

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


# --- Docker deployment: the watchtower HTTP trigger --------------------------


def _in_container_updater(
    handler: Callable[[httpx.Request], httpx.Response], *, token: str = "s3cret"
) -> tuple[Updater, FakeRunner]:
    """An updater that believes it's containerised, wired to a fake watchtower."""
    runner = FakeRunner(lambda argv: CommandResult(0, "sha\n", ""))
    updater = Updater(
        app_dir=Path("/app"),
        runner=runner,
        in_container=True,
        watchtower_url="http://watchtower:8080/v1/update",
        watchtower_token=token,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    return updater, runner


async def test_update_in_container_triggers_watchtower() -> None:
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200)

    updater, runner = _in_container_updater(handle)
    result = await updater.update()

    assert result.ok
    assert "Watchtower" in result.output
    # "triggered", never "complete": the container answering is the one being
    # replaced, so it cannot have watched the update finish.
    assert "complete" not in result.output.lower()
    # One line, so Settings > Client shows it as the message instead of burying
    # it in the output pane.
    assert "\n" not in result.output
    assert len(seen) == 1
    assert str(seen[0].url) == "http://watchtower:8080/v1/update"
    # The header 1.7.1's RequireToken actually compares, not the bare `Token:`
    # one its docs at that tag also describe.
    assert seen[0].headers["authorization"] == "Bearer s3cret"
    # And still no attempt at the source-only script from inside a container.
    assert not any(a[0] == "bash" for a in runner.calls)


async def test_update_in_container_treats_a_read_timeout_as_triggered() -> None:
    """/v1/update answers only once the update it started is over - i.e. never."""

    def handle(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    updater, _ = _in_container_updater(handle)
    result = await updater.update()

    assert result.ok
    assert "Watchtower" in result.output


async def test_update_in_container_falls_back_when_watchtower_is_absent() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    updater, _ = _in_container_updater(handle)
    result = await updater.update()

    assert not result.ok
    assert "deploy/update.sh" in result.output


async def test_update_in_container_without_a_token_never_calls_out() -> None:
    calls: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200)

    updater, _ = _in_container_updater(handle, token="")
    result = await updater.update()

    assert not result.ok
    assert "deploy/update.sh" in result.output
    assert not calls


async def test_update_in_container_reports_a_rejected_token() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401)

    updater, _ = _in_container_updater(handle)
    result = await updater.update()

    assert not result.ok
    assert "WATCHTOWER_HTTP_API_TOKEN" in result.output
    assert "\n" not in result.output


async def test_rollback_in_container_never_triggers_watchtower() -> None:
    """Watchtower only moves forward, and cleanup already deleted the old image."""
    calls: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200)

    updater, _ = _in_container_updater(handle)
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
