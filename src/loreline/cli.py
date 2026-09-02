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
    NOT_CHECKED_NOTE,
    FailOn,
    render,
    run_check,
    should_fail,
    summarize,
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
