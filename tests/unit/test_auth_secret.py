"""Tests for JWT-secret auto-generation and the atomic secret write."""

from __future__ import annotations

import stat
import time
from pathlib import Path

from loreline.secrets import SecretStore
from loreline.settings import DEFAULT_JWT_SECRET, Settings
from loreline.web.auth import LoginRateLimiter, ensure_jwt_secret, issue_token, verify_token


def test_ensure_jwt_secret_generates_and_persists(tmp_path: Path) -> None:
    secrets = SecretStore(tmp_path / "secrets.json")
    first = Settings(data_dir=tmp_path, jwt_secret=DEFAULT_JWT_SECRET)
    ensure_jwt_secret(first, secrets)
    assert first.jwt_secret != DEFAULT_JWT_SECRET
    assert len(first.jwt_secret) >= 32

    # A fresh process (new Settings) reuses the persisted secret so existing
    # session cookies stay valid across restarts.
    second = Settings(data_dir=tmp_path, jwt_secret=DEFAULT_JWT_SECRET)
    ensure_jwt_secret(second, secrets)
    assert second.jwt_secret == first.jwt_secret


def test_ensure_jwt_secret_respects_explicit_value(tmp_path: Path) -> None:
    secrets = SecretStore(tmp_path / "secrets.json")
    settings = Settings(data_dir=tmp_path, jwt_secret="explicit-secret")
    ensure_jwt_secret(settings, secrets)
    assert settings.jwt_secret == "explicit-secret"
    assert secrets.get("_jwt_secret") is None


def test_ensure_jwt_secret_empty_generates(tmp_path: Path) -> None:
    secrets = SecretStore(tmp_path / "secrets.json")
    settings = Settings(data_dir=tmp_path, jwt_secret="")
    ensure_jwt_secret(settings, secrets)
    assert settings.jwt_secret
    assert secrets.get("_jwt_secret") == settings.jwt_secret


def test_token_invalidated_by_password_rotation(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path, auth_password="old-pw", jwt_secret="shared-secret")
    token = issue_token(settings)
    assert verify_token(token, settings)

    # Rotating the password (the only way to do so: edit the env var and
    # restart) must invalidate every cookie issued under the old password,
    # even though the JWT signing secret itself is unchanged.
    settings.auth_password = "new-pw"
    assert not verify_token(token, settings)

    # A token issued after the rotation verifies fine.
    assert verify_token(issue_token(settings), settings)


def test_secret_file_atomic_and_private(tmp_path: Path) -> None:
    path = tmp_path / "secrets.json"
    store = SecretStore(path)
    store.set("k", "v")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    # No leftover temp file from the atomic replace.
    assert not (tmp_path / "secrets.json.tmp").exists()
    store.set("k2", "v2")
    assert store.get("k") == "v"
    assert store.get("k2") == "v2"


def test_login_rate_limiter_locks_out_after_max_attempts() -> None:
    limiter = LoginRateLimiter(max_attempts=3, lockout_seconds=0.05)
    for _ in range(2):
        assert limiter.allowed("1.2.3.4")
        limiter.record_failure("1.2.3.4")
    assert limiter.allowed("1.2.3.4")  # 2 failures, threshold is 3: still allowed
    limiter.record_failure("1.2.3.4")
    assert not limiter.allowed("1.2.3.4")  # 3rd failure trips the lockout

    # A different key (client IP) is unaffected by another key's lockout.
    assert limiter.allowed("5.6.7.8")

    time.sleep(0.06)
    assert limiter.allowed("1.2.3.4")  # lockout window elapsed


def test_login_rate_limiter_success_resets_count() -> None:
    limiter = LoginRateLimiter(max_attempts=3, lockout_seconds=30.0)
    limiter.record_failure("1.2.3.4")
    limiter.record_failure("1.2.3.4")
    limiter.record_success("1.2.3.4")
    limiter.record_failure("1.2.3.4")
    limiter.record_failure("1.2.3.4")
    assert limiter.allowed("1.2.3.4")  # only 2 failures since the reset, below threshold
