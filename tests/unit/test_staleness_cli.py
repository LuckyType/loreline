"""The CI entry point: what `loreline check-capabilities` exits with.

The exit code is the whole contract with CI, so it is worth a test that does
not depend on today's capabilities.yaml. The report is faked here on purpose:
what is under test is the wiring between a finding's severity and a red build,
not the findings themselves (tests/unit/test_staleness.py owns those).
"""

from __future__ import annotations

from typing import Any

import pytest
from typer.testing import CliRunner

from loreline import cli
from loreline.models import ProviderKind
from loreline.staleness.report import Code, Finding, Severity, StalenessReport

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
