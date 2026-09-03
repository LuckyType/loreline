"""Command-line interface (typer)."""

from __future__ import annotations

import asyncio
import json
from typing import Annotated

import typer
import uvicorn

from loreline import __version__
from loreline.settings import get_settings
from loreline.staleness import (
    NEVER_WRITTEN_NOTE,
    NOT_CHECKED_NOTE,
    FailOn,
    SyncRefusedError,
    render,
    render_sync,
    run_check,
    run_sync,
    should_fail,
    summarize,
    write_sync,
)
from loreline.staleness.deprecation import FAIL_HORIZON_DAYS, WARN_HORIZON_DAYS

app = typer.Typer(
    name="loreline",
    help="Loreline - tabletop session transcriber.",
    no_args_is_help=True,
    add_completion=False,
)


@app.command()
def version() -> None:
    """Print the Loreline version."""
    typer.echo(__version__)


@app.command()
def run(
    host: str | None = typer.Option(None, help="Bind host (overrides settings)."),
    port: int | None = typer.Option(None, help="Bind port (overrides settings)."),
    reload: bool = typer.Option(False, help="Enable auto-reload (dev only)."),
) -> None:
    """Run the web server + orchestrator."""
    settings = get_settings()
    uvicorn.run(
        "loreline.web.app:create_app",
        factory=True,
        host=host or settings.host,
        port=port or settings.port,
        reload=reload,
        log_config=None,
    )


@app.command()
def check_capabilities(
    offline: Annotated[
        bool,
        typer.Option(help="Skip every vendor call and check the recorded sunset dates only."),
    ] = False,
    fail_on: Annotated[
        FailOn,
        typer.Option(help="Lowest severity that exits non-zero ('never' to always exit 0)."),
    ] = FailOn.ERROR,
    warn_days: Annotated[
        int,
        typer.Option(help="Warn when a recorded sunset date is closer than this many days."),
    ] = WARN_HORIZON_DAYS,
    fail_days: Annotated[
        int,
        typer.Option(help="Fail when a recorded sunset date is closer than this many days."),
    ] = FAIL_HORIZON_DAYS,
    request_timeout: Annotated[
        float,
        typer.Option(help="Per-catalogue HTTP timeout, in seconds."),
    ] = 20.0,
    as_json: Annotated[
        bool,
        typer.Option("--json", help="Emit the report as JSON instead of text."),
    ] = False,
) -> None:
    """Report where capabilities.yaml has drifted from the vendors (CI check).

    Runs without any credentials: OpenRouter's catalogue answers
    unauthenticated, and the sunset-date half needs no network at all. Vendors
    that do want a key are read from the environment (OPENAI_API_KEY,
    DEEPGRAM_API_KEY, GEMINI_API_KEY, OPENROUTER_API_KEY) and reported as "not
    checked" when there is none.

    An unreachable vendor never fails this command. Only findings at or above
    --fail-on do, and the default (error) is reached solely by things that need
    no vendor to confirm them: a sunset date that has passed on a model still
    being offered, or a model a reachable vendor has stopped listing.
    """
    report = asyncio.run(
        run_check(
            offline=offline,
            warn_days=warn_days,
            fail_days=fail_days,
            request_timeout=request_timeout,
        )
    )
    if as_json:
        typer.echo(json.dumps(report.as_dict(), indent=2))
    else:
        typer.echo(render(report))
        if not offline:
            # Only worth printing when catalogues were actually read: it is a
            # note about what the vendors were and were not asked.
            typer.echo("")
            typer.echo(NOT_CHECKED_NOTE)
        typer.echo("")
        typer.echo(summarize(report))
    if should_fail(report, fail_on):
        raise typer.Exit(code=1)


@app.command()
def sync_capabilities(
    write: Annotated[
        bool,
        typer.Option(
            "--write",
            help="Actually rewrite capabilities.yaml. Without it this is a dry run.",
        ),
    ] = False,
    request_timeout: Annotated[
        float,
        typer.Option(help="Per-catalogue HTTP timeout, in seconds."),
    ] = 20.0,
    as_json: Annotated[
        bool,
        typer.Option("--json", help="Emit the plan as JSON instead of text."),
    ] = False,
) -> None:
    """Regenerate the machine-derivable fields of capabilities.yaml.

    The write half of check-capabilities, and deliberately much narrower than
    it: this rewrites only the fields a vendor catalogue genuinely publishes -
    llm.context_length, llm.max_output_tokens, llm.temperature, the
    llm.reasoning block, and the video parameter lists - and only for models
    that are already curated. Every hand annotation is left alone, and adding
    or dropping a model stays a human decision that check-capabilities reports
    and this command will not make.

    A DRY RUN BY DEFAULT. It prints a diff of exactly what it would change and
    writes nothing until --write is passed, so a first run cannot rewrite the
    file under someone who was only looking.

    Edits are byte-span splices over the file as it stands, so the comments -
    which are the vendor doc citations and the reasons values are what they
    are - survive, and a run with no drift leaves the file untouched rather
    than re-rendered. Anything that cannot be edited that surgically is
    reported for a hand edit instead of guessed at.

    An unreachable vendor changes nothing: it contributes no comparison at all,
    so a fetch failure can never be read as "the vendor dropped this value" and
    blank a field. A run where no catalogue answered writes nothing even with
    --write.
    """
    plan = asyncio.run(run_sync(request_timeout=request_timeout))

    def say(message: str) -> None:
        """A human status line, suppressed under --json.

        --json is the whole of stdout when it is set, as it is for
        check-capabilities: a caller piping this into jq should not have to
        strip a status line off the end.
        """
        if not as_json:
            typer.echo(message)

    if as_json:
        typer.echo(json.dumps(plan.as_dict(), indent=2))
    else:
        typer.echo(render_sync(plan))
        typer.echo("")
        typer.echo(NEVER_WRITTEN_NOTE)
        typer.echo("")
    if not plan.answered:
        # Not a failure, and not a clean bill of health either. Writing here
        # would mean acting on nothing, so the command says so and stops.
        say("no vendor answered; nothing written.")
        return
    if not plan.dirty:
        say("nothing to write.")
        return
    if not write:
        say(f"dry run: {len(plan.changes)} value(s) would change. Pass --write to apply.")
        return
    try:
        write_sync(plan)
    except SyncRefusedError as exc:
        # The verification runs before the write, so the file on disk is still
        # the original one at this point.
        typer.echo(f"refused to write: {exc}")
        raise typer.Exit(code=1) from exc
    say(f"wrote {len(plan.changes)} value(s) to capabilities.yaml.")


@app.command()
def devices() -> None:
    """List available audio input devices (requires the 'audio' extra)."""
    try:
        from loreline.audio.devices import list_input_devices  # noqa: PLC0415
    except ImportError:  # pragma: no cover - optional dependency path
        typer.echo("Audio support not installed. Install with: uv sync --extra audio")
        raise typer.Exit(code=1) from None

    for dev in list_input_devices():
        typer.echo(f"[{dev.index}] {dev.name} ({dev.channels}ch @ {dev.default_samplerate:.0f}Hz)")


if __name__ == "__main__":  # pragma: no cover
    app()
