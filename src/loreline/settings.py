"""Application settings loaded from environment / .env.

Env vars always take precedence over UI-managed secrets (see ``secrets.py``).
All settings are prefixed with ``LORELINE_``.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Sentinel marking an unset JWT secret; replaced by a persisted random value at
# startup (see ``loreline.web.auth.ensure_jwt_secret``).
DEFAULT_JWT_SECRET = "change-me-in-prod"


class Settings(BaseSettings):
    """Global application configuration."""

    model_config = SettingsConfigDict(
        env_prefix="LORELINE_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- General ---
    environment: str = Field(default="dev", description="dev | prod")
    debug: bool = Field(default=False)

    # --- Storage ---
    data_dir: Path = Field(
        default=Path("./data"),
        description="Base dir for SQLite DB, audio files and the managed secret store.",
    )

    # --- Web server ---
    host: str = Field(default="127.0.0.1", description="Bind host (LAN-only by default).")
    port: int = Field(default=8000)

    # --- Ops / self-update ---
    app_dir: Path = Field(
        default=Path("."),
        description="Repo/app dir used for git self-update and rollback.",
    )
    systemd_unit: str = Field(default="loreline", description="systemd unit name for autostart.")
    docker_api: str = Field(
        default="",
        description=(
            "Base URL of a Docker API (the docker-socket-proxy in docker-compose.yml). "
            "Enables Settings > Services; blank disables it."
        ),
    )
    # --- Ops / self-update: Watchtower trigger (Docker deployments only) ---
    # Read only when the app is running in a container, where its own
    # git-and-systemd update path cannot work. Both are ignored otherwise.
    watchtower_url: str = Field(
        default="http://watchtower:8080/v1/update",
        description=(
            "Watchtower's HTTP API update endpoint, reachable on the compose network. "
            "Only consulted in a Docker deployment, and only when a token is set. "
            "Blank disables the in-app update trigger."
        ),
    )
    watchtower_token: str = Field(
        default="",
        description=(
            "Shared secret for the above, sent to Watchtower on every trigger and "
            "matching its WATCHTOWER_HTTP_API_TOKEN. Blank (the default) leaves the "
            "in-app Update button reporting that updates run from the host."
        ),
    )
    disk_alert_threshold_mb: int = Field(
        default=500,
        description=(
            "Free-space floor; below this /healthz reports 'degraded' and a recording "
            "session pushes a low-disk alert. 0 disables both."
        ),
    )
    # The only outbound call this app makes without a user asking for one, so
    # it gets a switch: an air-gapped deployment, or one that would rather not
    # talk to a vendor at boot, turns it off and keeps the offline half (a
    # recorded sunset date that has passed) which needs no network anyway.
    check_favorite_models: bool = Field(
        default=True,
        description=(
            "On startup, warn when a provider's favorite model is retired or gone "
            "from the vendor's catalogue. Best-effort; never blocks startup."
        ),
    )

    # --- Auth ---
    auth_password: str = Field(
        default="",
        description="Single shared web-UI password. Empty disables auth (dev only).",
    )
    jwt_secret: str = Field(
        default=DEFAULT_JWT_SECRET,
        description="HMAC secret for session JWTs; auto-generated + persisted if left default.",
    )
    jwt_ttl_seconds: int = Field(default=60 * 60 * 12)
    trusted_proxies: str = Field(
        default="",
        description=(
            "Comma-separated IPs/CIDRs of reverse proxies allowed to speak for the "
            "client, e.g. '172.16.0.0/12' for the bundled Caddy on the compose "
            "network. Only from these peers is X-Forwarded-Proto believed when "
            "deciding whether to mark the auth cookie Secure. Empty (the default) "
            "trusts nothing and reads the scheme off the connection itself."
        ),
    )

    # --- Logging ---
    log_level: str = Field(default="INFO")
    log_json: bool = Field(default=False, description="JSON logs (prod) vs console (dev).")

    @property
    def db_path(self) -> Path:
        return self.data_dir / "loreline.db"

    @property
    def audio_dir(self) -> Path:
        return self.data_dir / "audio"

    @property
    def video_dir(self) -> Path:
        return self.data_dir / "video"

    @property
    def logs_dir(self) -> Path:
        """Root of the per-version log files (one subdirectory per session)."""
        return self.data_dir / "logs"

    @property
    def secrets_path(self) -> Path:
        return self.data_dir / "secrets.json"

    @property
    def disk_alert_threshold_bytes(self) -> int:
        """The free-space floor in bytes (health badge and live capture check)."""
        return self.disk_alert_threshold_mb * 1024 * 1024


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the cached singleton settings instance."""
    return Settings()
