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
    disk_alert_threshold_mb: int = Field(
        default=500,
        description="Free-space floor; below this /healthz reports 'degraded'.",
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


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the cached singleton settings instance."""
    return Settings()
