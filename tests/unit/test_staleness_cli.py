"""The two capability commands' CLI contracts: what they exit with, and what
`sync-capabilities` refuses to do without being asked.

The exit code is the whole contract with CI, so it is worth a test that does
not depend on today's capabilities.yaml. The report is faked here on purpose:
what is under test is the wiring between a finding's severity and a red build,
not the findings themselves (tests/unit/test_staleness.py owns those).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from typer.testing import CliRunner

from loreline import cli
from loreline.models import Interaction, ProviderKind
from loreline.staleness.catalog import CatalogProbe, CatalogStatus
from loreline.staleness.report import Code, Finding, Severity, StalenessReport
from loreline.staleness.sync import Change, SyncPlan, SyncRefusedError

runner = CliRunner()


def _report(*severities: Severity) -> StalenessReport:
    return StalenessReport(
        tuple(
            Finding(
                severity=severity,
                code=Code.DEPRECATION_NEAR,
                kind=ProviderKind.OPENAI,
                model="old-model",
                message="retires in 3 days (2026-09-05), and is still offered",
            )
            for severity in severities
        )
    )


@pytest.fixture
def fake_check(monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    def install(report: StalenessReport) -> None:
        async def _run(**_kwargs: Any) -> StalenessReport:
            return report

        monkeypatch.setattr(cli, "run_check", _run)

    return install


def test_a_clean_report_exits_zero(fake_check) -> None:  # type: ignore[no-untyped-def]
    fake_check(_report())
    result = runner.invoke(cli.app, ["check-capabilities", "--offline"])
    assert result.exit_code == 0
    assert "no recorded sunset date is due or imminent" in result.stdout


def test_an_error_finding_fails_the_build(fake_check) -> None:  # type: ignore[no-untyped-def]
    fake_check(_report(Severity.ERROR))
    result = runner.invoke(cli.app, ["check-capabilities", "--offline"])
    assert result.exit_code == 1
    assert "retires in 3 days" in result.stdout


def test_a_warning_alone_keeps_the_build_green(fake_check) -> None:  # type: ignore[no-untyped-def]
    """The 30 day band is a nudge, not a gate: the model still works for
    another month."""
    fake_check(_report(Severity.WARNING))
    result = runner.invoke(cli.app, ["check-capabilities", "--offline"])
    assert result.exit_code == 0


def test_the_threshold_can_be_tightened(fake_check) -> None:  # type: ignore[no-untyped-def]
    fake_check(_report(Severity.WARNING))
    assert runner.invoke(cli.app, ["check-capabilities", "--fail-on", "warning"]).exit_code == 1


def test_never_is_the_escape_hatch(fake_check) -> None:  # type: ignore[no-untyped-def]
    fake_check(_report(Severity.ERROR))
    assert runner.invoke(cli.app, ["check-capabilities", "--fail-on", "never"]).exit_code == 0


def test_json_output_is_machine_readable(fake_check) -> None:  # type: ignore[no-untyped-def]
    import json  # noqa: PLC0415

    fake_check(_report(Severity.ERROR))
    result = runner.invoke(cli.app, ["check-capabilities", "--offline", "--json"])
    payload = json.loads(result.stdout)
    assert payload["findings"][0]["severity"] == "error"
    assert payload["findings"][0]["model"] == "old-model"


# --------------------------------------------------------------------------
# `loreline sync-capabilities`: the write half, whose contract is that it does
# not write. The planning is covered by tests/unit/test_capability_sync.py;
# what matters here is that a plain run cannot rewrite anyone's file.
# --------------------------------------------------------------------------


def _plan(*, dirty: bool = True, answered: bool = True) -> SyncPlan:
    original = "a:\n  b: 1\n"
    updated = "a:\n  b: 2\n" if dirty else original
    probe = CatalogProbe(
        ProviderKind.OPENROUTER,
        Interaction.SUMMARIZE,
        "https://example.invalid/models",
        CatalogStatus.OK if answered else CatalogStatus.UNREACHABLE,
        "1 models" if answered else "could not check: ConnectError",
    )
    change = Change(
        kind=ProviderKind.OPENROUTER,
        interaction=Interaction.SUMMARIZE,
        model="nvidia/nemotron-3-ultra-550b-a55b",
        fact="llm.max_output_tokens",
        path=("providers", "openrouter"),
        before="32768",
        after="182520",
    )
    return SyncPlan(
        original=original,
        updated=updated,
        changes=(change,) if dirty else (),
        probes=(probe,),
    )


# Installs a plan, and hands back the list the command's writes land in, so a
# test can assert on the one thing that matters: whether it wrote at all.
InstallSync = Callable[[SyncPlan], list[SyncPlan]]


@pytest.fixture
def fake_sync(monkeypatch: pytest.MonkeyPatch) -> InstallSync:
    written: list[SyncPlan] = []

    def install(plan: SyncPlan) -> list[SyncPlan]:
        async def _run(**_kwargs: Any) -> SyncPlan:
            return plan

        def _write(target: SyncPlan, *_args: Any, **_kwargs: Any) -> None:
            written.append(target)

        monkeypatch.setattr(cli, "run_sync", _run)
        monkeypatch.setattr(cli, "write_sync", _write)
        return written

    return install


def test_a_plain_run_prints_the_diff_and_writes_nothing(fake_sync: InstallSync) -> None:
    written = fake_sync(_plan())
    result = runner.invoke(cli.app, ["sync-capabilities"])
    assert result.exit_code == 0
    assert written == []
    assert "dry run" in result.stdout
    assert "--write to apply" in result.stdout
    # The diff itself, not just a count: the point of the dry run is that a
    # reader can see the exact line before agreeing to it.
    assert "-  b: 1" in result.stdout
    assert "+  b: 2" in result.stdout
    assert "llm.max_output_tokens: 32768 -> 182520" in result.stdout


def test_the_write_flag_is_what_writes(fake_sync: InstallSync) -> None:
    written = fake_sync(_plan())
    result = runner.invoke(cli.app, ["sync-capabilities", "--write"])
    assert result.exit_code == 0
    assert len(written) == 1
    assert "wrote 1 value(s)" in result.stdout


def test_a_vendor_that_never_answered_is_not_a_clean_sync(fake_sync: InstallSync) -> None:
    """ "Nothing to write" and "nobody told us anything" have to read
    differently, or an outage looks like a green run."""
    written = fake_sync(_plan(dirty=False, answered=False))
    result = runner.invoke(cli.app, ["sync-capabilities", "--write"])
    assert result.exit_code == 0
    assert written == []
    assert "no vendor answered" in result.stdout


def test_an_agreeing_run_writes_nothing_either(fake_sync: InstallSync) -> None:
    written = fake_sync(_plan(dirty=False))
    result = runner.invoke(cli.app, ["sync-capabilities", "--write"])
    assert result.exit_code == 0
    assert written == []
    assert "nothing to write" in result.stdout


def test_a_refused_write_fails_the_command(
    monkeypatch: pytest.MonkeyPatch, fake_sync: InstallSync
) -> None:
    fake_sync(_plan())

    def refuse(_plan: SyncPlan, *_args: Any, **_kwargs: Any) -> None:
        raise SyncRefusedError("the rewritten file no longer validates: boom")

    monkeypatch.setattr(cli, "write_sync", refuse)
    result = runner.invoke(cli.app, ["sync-capabilities", "--write"])
    assert result.exit_code == 1
    assert "refused to write" in result.stdout


def test_the_command_says_what_it_will_never_touch(fake_sync: InstallSync) -> None:
    """The scope note is printed on every run, because "no changes" is only
    reassuring if the reader knows which fields were in scope at all."""
    fake_sync(_plan())
    result = runner.invoke(cli.app, ["sync-capabilities"])
    assert "Never written by this command" in result.stdout
    assert "glossary" in result.stdout


def test_the_sync_plan_is_machine_readable(fake_sync: InstallSync) -> None:
    import json  # noqa: PLC0415

    fake_sync(_plan())
    result = runner.invoke(cli.app, ["sync-capabilities", "--json"])
    # The whole of stdout, so the output pipes straight into jq.
    payload = json.loads(result.stdout)
    assert payload["changes"][0]["fact"] == "llm.max_output_tokens"
    assert payload["changes"][0]["after"] == "182520"
    assert "-  b: 1" in payload["diff"]
