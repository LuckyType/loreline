"""Tests for JWT-secret auto-generation and the atomic secret write."""

from __future__ import annotations

import hashlib
import stat
import time
from pathlib import Path

import jwt

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


def test_login_rate_limiter_forgets_keys_that_have_gone_quiet() -> None:
    """The table holds who is failing now, not every address that ever failed.

    Without this, a key that never reaches the threshold is never removed: the
    dict grows for the life of the process, one entry per address that mistyped
    the password once, and the address is whatever the caller can make it.
    """
    limiter = LoginRateLimiter(max_attempts=3, lockout_seconds=0.05)
    for octet in range(20):
        limiter.record_failure(f"1.2.3.{octet}")
    assert len(limiter) == 20

    time.sleep(0.06)
    limiter.record_failure("9.9.9.9")
    assert len(limiter) == 1
    # And their counts went with them: a window's silence is a clean slate,
    # which is what sitting out a lockout already bought before the sweep.
    assert limiter.allowed("1.2.3.0")


def test_a_token_gives_nothing_to_test_password_guesses_against(tmp_path: Path) -> None:
    """The password's mark in a token cannot be recomputed without the signing secret.

    A JWT's payload is only encoded, and the cookie crosses a LAN in the clear
    on the plain-HTTP path. The mark used to be a bare SHA-256 prefix of the
    password, which let anyone holding one cookie check guesses offline.
    """
    settings = Settings(data_dir=tmp_path, auth_password="hunter2", jwt_secret="s" * 32)
    payload = jwt.decode(issue_token(settings), options={"verify_signature": False})  # pyright: ignore[reportUnknownMemberType]

    assert payload["pwd"] != hashlib.sha256(b"hunter2").hexdigest()[:16]
    # Same password under another secret is another mark.
    other = Settings(data_dir=tmp_path, auth_password="hunter2", jwt_secret="t" * 32)
    other_payload = jwt.decode(issue_token(other), options={"verify_signature": False})  # pyright: ignore[reportUnknownMemberType]
    assert payload["pwd"] != other_payload["pwd"]
