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


# `in_container` is spelled out in every test below rather than left to the
# default, which reads /.dockerenv on whatever host is running the suite. It
# decides where current_revision looks, so leaving it implicit would make these
# tests quietly answer a different question inside a container than outside one.


async def test_current_revision() -> None:
    runner = FakeRunner(lambda argv: CommandResult(0, "abc123\n", ""))
    updater = Updater(app_dir=Path("/app"), runner=runner, in_container=False)
    assert await updater.current_revision() == "abc123"


async def test_current_revision_failure() -> None:
    runner = FakeRunner(lambda argv: CommandResult(128, "", "not a repo"))
    updater = Updater(app_dir=Path("/app"), runner=runner, in_container=False)
    assert await updater.current_revision() is None


async def test_current_described_reports_the_tag_and_the_distance() -> None:
    """One string carrying the last tag, how far past it this is, and the SHA."""
    runner = FakeRunner(lambda argv: CommandResult(0, "v0.2.0-93-ge57029c\n", ""))
    updater = Updater(app_dir=Path("/app"), runner=runner, in_container=False)

    assert await updater.current_described() == "v0.2.0-93-ge57029c"
    assert runner.calls == [["git", "describe", "--tags", "--always"]]


async def test_current_revision_in_a_container_reads_what_was_baked_in() -> None:
    """The image has no .git, so the build-time value is the only truth there is."""
    runner = FakeRunner(lambda argv: CommandResult(0, "from-git\n", ""))
    updater = Updater(
        app_dir=Path("/app"),
        runner=runner,
        in_container=True,
        build_commit="baked-sha",
        build_described="v0.2.0-93-gbaked",
    )

    assert await updater.current_revision() == "baked-sha"
    assert await updater.current_described() == "v0.2.0-93-gbaked"
    # The part worth guarding: not that git lost, but that git was never asked.
    # It could only ever fail here, and this runs on every visit to
    # Settings > Client.
    assert runner.calls == []


async def test_current_revision_in_a_container_with_nothing_baked() -> None:
    """A `docker build` with no --build-arg knows nothing, and says so."""
    runner = FakeRunner(lambda argv: CommandResult(0, "from-git\n", ""))
    updater = Updater(app_dir=Path("/app"), runner=runner, in_container=True)

    assert await updater.current_revision() is None
    assert await updater.current_described() is None
    assert runner.calls == []


async def test_current_revision_outside_a_container_prefers_git() -> None:
    """A checkout moves with every pull; a baked value is frozen at build time."""
    runner = FakeRunner(lambda argv: CommandResult(0, "from-git\n", ""))
    updater = Updater(
        app_dir=Path("/app"),
        runner=runner,
        in_container=False,
        build_commit="baked-sha",
        build_described="v0.2.0-93-gbaked",
    )

    assert await updater.current_revision() == "from-git"
    assert await updater.current_described() == "from-git"


async def test_current_revision_outside_a_container_falls_back_to_the_baked_value() -> None:
    """git is authoritative where it works, and it does not always work.

    A source deployment installed from a tarball, or one whose checkout this
    process cannot read, has no answer from git - and a package built with the
    revision baked in still knows what it was built from.
    """
    runner = FakeRunner(lambda argv: CommandResult(128, "", "not a git repository"))
    updater = Updater(
        app_dir=Path("/app"),
        runner=runner,
        in_container=False,
        build_commit="baked-sha",
        build_described="v0.2.0-93-gbaked",
    )

    assert await updater.current_revision() == "baked-sha"
    assert await updater.current_described() == "v0.2.0-93-gbaked"


async def test_current_revision_treats_empty_git_output_as_no_answer() -> None:
    """Exit 0 and nothing on stdout must not beat a perfectly good fallback."""
    runner = FakeRunner(lambda argv: CommandResult(0, "\n", ""))
    updater = Updater(
        app_dir=Path("/app"), runner=runner, in_container=False, build_commit="baked-sha"
    )

    assert await updater.current_revision() == "baked-sha"


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
    result = await Updater(app_dir=Path("/app"), runner=runner, in_container=False).update()
    assert result.ok
    assert result.previous_commit == "old-sha"
    assert result.new_commit == "new-sha"
    assert "Update complete." in result.output


async def test_update_refuses_in_container() -> None:
    # build_commit, not the runner's answer: a container reports the revision
    # baked into its image and never runs git at all (see current_revision).
    runner = FakeRunner(lambda argv: CommandResult(0, "unused\n", ""))
    updater = Updater(app_dir=Path("/app"), runner=runner, in_container=True, build_commit="sha")

    result = await updater.update()

    assert not result.ok
    assert result.previous_commit == "sha"
    assert result.new_commit == "sha"
    assert "deploy/update.sh" in result.output
    # The refusal has to name the way out, not just the refusal. An operator who
    # wants this button working learns the updater profile exists from here or
    # from nowhere, and the message used to point at a systemd timer that is as
    # absent from a container as the unit it had just said was missing.
    assert "--profile updater" in result.output
    assert "UPDATER_TOKEN" in result.output
    assert "systemctl" not in result.output
    # Never even tried to run the source-deployment script.
    assert not any(a[0] == "bash" for a in runner.calls)


async def test_rollback_refuses_in_container() -> None:
    runner = FakeRunner(lambda argv: CommandResult(0, "unused\n", ""))
    updater = Updater(app_dir=Path("/app"), runner=runner, in_container=True, build_commit="sha")

    result = await updater.rollback("deadbeef")

    assert not result.ok
    assert "deploy/update.sh" in result.output
    assert not any(a[:2] == ["git", "reset"] for a in runner.calls)


# --- Docker deployment: the updater service ----------------------------------
# One POST, one bearer token, no arguments. The service answers after pulling
# and before recreating this container, which is why the result can be believed:
# `changed` distinguishes "a newer image was pulled and the app is on its way
# out" from "nothing to do, nothing restarted", and a failure comes back as the
# update script's own transcript rather than a summary of it.

_UPDATE_URL = "http://updater:8080/update"
_TOKEN = "s3cret-token"


def _in_container_updater(
    handler: Callable[[httpx.Request], httpx.Response], *, token: str = _TOKEN
) -> tuple[Updater, FakeRunner]:
    """An updater that believes it's containerised, wired to a fake service."""
    runner = FakeRunner(lambda argv: CommandResult(0, "sha\n", ""))
    updater = Updater(
        app_dir=Path("/app"),
        runner=runner,
        in_container=True,
        updater_url="http://updater:8080",
        updater_token=token,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    return updater, runner


def _answers(
    seen: list[httpx.Request],
    *,
    status: int = 200,
    body: dict[str, object] | None = None,
    changed: bool = True,
) -> Callable[[httpx.Request], httpx.Response]:
    """Record every request and answer with one service reply."""
    reply: dict[str, object] = (
        {"ok": True, "changed": changed, "returncode": 0, "output": "image_changed=1"}
        if body is None
        else body
    )

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status, json=reply)

    return handle


async def test_update_in_container_posts_once_to_the_updater() -> None:
    seen: list[httpx.Request] = []
    updater, runner = _in_container_updater(_answers(seen))

    result = await updater.update()

    assert result.ok
    # One line, so Settings > Client shows it as the message instead of burying
    # it in the output pane.
    assert "\n" not in result.output
    assert "recreated" in result.output.lower()
    # Exactly one request, to the one route, and it carries no body at all:
    # there is nothing in it naming an image, a tag or a container.
    assert [str(r.url) for r in seen] == [_UPDATE_URL]
    assert [r.method for r in seen] == ["POST"]
    assert seen[0].content == b""
    # And still no attempt at the source-only script from inside a container.
    assert not any(a[0] == "bash" for a in runner.calls)


async def test_update_in_container_sends_a_bearer_token() -> None:
    seen: list[httpx.Request] = []
    updater, _ = _in_container_updater(_answers(seen))

    await updater.update()

    assert seen[0].headers["authorization"] == f"Bearer {_TOKEN}"


async def test_update_in_container_reports_up_to_date_without_a_restart() -> None:
    """`changed` is false, so nothing was applied and nothing was recreated."""
    seen: list[httpx.Request] = []
    updater, _ = _in_container_updater(_answers(seen, changed=False))

    result = await updater.update()

    assert result.ok
    assert "up to date" in result.output.lower()
    assert "\n" not in result.output
    assert len(seen) == 1


async def test_update_in_container_carries_the_scripts_notes() -> None:
    """A success keeps its verdict as the headline and brings the notes with it.

    deploy/update-fast.sh names the parts of a release it could not deploy, and
    the diarization service is one: it is built from the checkout rather than
    pulled, so this path never touches it. The note used to be dropped twice
    over. The script printed it after the recreate, in a stage whose output
    nobody is left listening for, and this app then threw away the output it did
    get in favour of a canned sentence. An operator reading "Already up to date"
    had no way to learn that half a release had been skipped. This is the
    regression guard for the second half; the script's own stage placement is
    the first.
    """
    seen: list[httpx.Request] = []
    output = (
        "image_changed=0\n"
        "loreline-app-1  ghcr.io/luckytype/loreline  latest\n"
        "\n"
        "note: the diarization service is built from this checkout rather than pulled,\n"
        "      so this fast path never updates it. To rebuild that service:\n"
        "        docker compose --profile diarization up -d --build diarization\n"
        "Update complete.\n"
    )
    body: dict[str, object] = {"ok": True, "changed": False, "returncode": 0, "output": output}
    updater, _ = _in_container_updater(_answers(seen, body=body))

    result = await updater.update()

    assert result.ok
    first, _, rest = result.output.partition("\n")
    # The verdict still leads, so the page has one clear sentence to show.
    assert "up to date" in first.lower()
    # And the note travels with it, command and all.
    assert "diarization service is built from this checkout" in rest
    assert "--profile diarization up -d --build diarization" in rest
    # Compose's own chatter does not.
    assert "image_changed" not in result.output
    assert "loreline-app-1" not in result.output


async def test_update_in_container_falls_back_when_the_updater_is_absent() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    updater, _ = _in_container_updater(handle)
    result = await updater.update()

    assert not result.ok
    assert "deploy/update.sh" in result.output


async def test_update_in_container_without_a_token_never_calls_out() -> None:
    seen: list[httpx.Request] = []
    updater, _ = _in_container_updater(_answers(seen), token="")

    result = await updater.update()

    assert not result.ok
    assert "deploy/update.sh" in result.output
    assert not seen


async def test_update_in_container_reports_a_rejected_token() -> None:
    updater, _ = _in_container_updater(lambda request: httpx.Response(401, json={}))

    result = await updater.update()

    assert not result.ok
    # Names both halves of the pair, since a mismatch is the only way to get here.
    assert "LORELINE_UPDATER_TOKEN" in result.output
    assert "UPDATER_TOKEN" in result.output
    assert "\n" not in result.output


async def test_update_in_container_reports_a_busy_updater() -> None:
    """A second press while the first update is still running."""
    updater, _ = _in_container_updater(lambda request: httpx.Response(409, json={}))

    result = await updater.update()

    assert not result.ok
    assert "already running" in result.output.lower()
    assert "\n" not in result.output


async def test_update_in_container_returns_the_scripts_own_failure() -> None:
    """A failed update is worth far more verbatim than summarised."""
    transcript = (
        "previous_commit=abc\n"
        "Pulling ghcr.io/luckytype/loreline failed. Nothing on this box was changed.\n"
        "Either fix works: make the package public, or `docker login ghcr.io`."
    )
    seen: list[httpx.Request] = []
    updater, _ = _in_container_updater(
        _answers(
            seen,
            body={"ok": False, "changed": False, "returncode": 1, "output": transcript},
        )
    )

    result = await updater.update()

    assert not result.ok
    assert result.returncode == 1
    assert result.output == transcript
    # Multi-line, which is what makes Settings > Client render the output pane.
    assert "\n" in result.output


async def test_update_in_container_repeats_a_service_level_refusal() -> None:
    """503 means the service is up but cannot act, and it says which reason."""
    detail = "The updater service has no UPDATER_TOKEN configured."
    seen: list[httpx.Request] = []
    updater, _ = _in_container_updater(
        _answers(
            seen,
            status=503,
            body={"ok": False, "changed": False, "returncode": 0, "output": detail},
        )
    )

    result = await updater.update()

    assert not result.ok
    assert result.output == detail


async def test_update_in_container_reports_an_unreadable_answer() -> None:
    """Something answered on that port, but it was not the updater service."""
    updater, _ = _in_container_updater(lambda request: httpx.Response(200, text="<html>hi"))

    result = await updater.update()

    assert not result.ok
    assert "200" in result.output
    assert "deploy/update.sh" in result.output
    assert "\n" not in result.output


async def test_rollback_in_container_never_calls_the_updater() -> None:
    """The update endpoint only moves forward, and takes no image to move to."""
    seen: list[httpx.Request] = []
    updater, _ = _in_container_updater(_answers(seen))

    result = await updater.rollback("deadbeef")

    assert not result.ok
    assert "deploy/update.sh" in result.output
    assert not seen


async def test_update_failure_captured() -> None:
    def handle(argv: list[str]) -> CommandResult:
        if argv[0] == "bash":
            return CommandResult(1, "", "boom")
        return CommandResult(0, "sha\n", "")

    result = await Updater(
        app_dir=Path("/app"), runner=FakeRunner(handle), in_container=False
    ).update()
    assert not result.ok
    assert result.returncode == 1
    assert "boom" in result.output


async def test_rollback_runs_sequence() -> None:
    def handle(argv: list[str]) -> CommandResult:
        return CommandResult(0, "sha\n" if _is_rev_parse(argv) else "", "")

    runner = FakeRunner(handle)
    result = await Updater(
        app_dir=Path("/app"), unit="loreline", runner=runner, in_container=False
    ).rollback("dead")
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
    result = await Updater(app_dir=Path("/app"), runner=runner, in_container=False).rollback("x")
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
