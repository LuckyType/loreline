"""Command-line interface (typer)."""

from __future__ import annotations

import typer
import uvicorn

from loreline import __version__
from loreline.settings import get_settings

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
