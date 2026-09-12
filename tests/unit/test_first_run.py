"""The three states, the setup code and the password rules, without a web app."""

from __future__ import annotations

from pathlib import Path

from loreline.secrets import SecretStore
from loreline.settings import Settings
from loreline.web.auth import AUTH_PASSWORD_NAME, ensure_auth_password
from loreline.web.first_run import is_blocked
from loreline.web.setup import (
    MIN_PASSWORD_LENGTH,
    SETUP_CODE_NAME,
    claim_instance,
    ensure_setup_code,
    instance_claimed,
    normalize_code,
    password_problem,
    setup_required,
    verify_setup_code,
)


def _settings(tmp_path: Path, **kwargs: object) -> Settings:
    """Settings for a fresh instance; the suite's env default is overridden per test."""
    return Settings(data_dir=tmp_path / "data", jwt_secret="test-secret", **kwargs)  # pyright: ignore[reportArgumentType]


# --- the three states -------------------------------------------------------


def test_a_password_from_the_environment_is_a_claimed_instance(tmp_path: Path) -> None:
    settings = _settings(tmp_path, auth_password="hunter2", first_run_setup=True)
    assert instance_claimed(settings)
    assert not setup_required(settings)


def test_no_password_with_the_gate_on_is_unclaimed(tmp_path: Path) -> None:
    settings = _settings(tmp_path, auth_password="", first_run_setup=True)
    assert not instance_claimed(settings)
    assert setup_required(settings)


def test_no_password_with_the_gate_off_is_the_open_box_the_tests_run_on(tmp_path: Path) -> None:
    """The state ~1200 tests rely on: no password, no gate, everything answers."""
    settings = _settings(tmp_path, auth_password="", first_run_setup=False)
    assert not instance_claimed(settings)
    assert not setup_required(settings)


def test_the_gate_defaults_to_on_so_a_bare_deployment_is_not_open(tmp_path: Path) -> None:
    """The default has to survive: it is the whole reason the claim exists."""
    assert Settings.model_fields["first_run_setup"].default is True


# --- the stored password ----------------------------------------------------


def test_a_stored_password_is_loaded_onto_the_live_settings(tmp_path: Path) -> None:
    store = SecretStore(tmp_path / "secrets.json")
    store.set(AUTH_PASSWORD_NAME, "from-the-store")
    settings = _settings(tmp_path, auth_password="", first_run_setup=True)
    ensure_auth_password(settings, store)
    assert settings.auth_password == "from-the-store"
    assert not setup_required(settings)


def test_the_environment_password_wins_over_the_stored_one(tmp_path: Path) -> None:
    """The documented way back into an instance whose password was mistyped."""
    store = SecretStore(tmp_path / "secrets.json")
    store.set(AUTH_PASSWORD_NAME, "the-typo")
    settings = _settings(tmp_path, auth_password="from-the-environment", first_run_setup=True)
    ensure_auth_password(settings, store)
    assert settings.auth_password == "from-the-environment"


def test_nothing_is_minted_when_there_is_no_password_anywhere(tmp_path: Path) -> None:
    store = SecretStore(tmp_path / "secrets.json")
    settings = _settings(tmp_path, auth_password="", first_run_setup=True)
    ensure_auth_password(settings, store)
    assert settings.auth_password == ""
    assert store.get(AUTH_PASSWORD_NAME) is None


# --- the setup code ---------------------------------------------------------


def test_the_code_is_minted_once_and_survives_a_restart(tmp_path: Path) -> None:
    store = SecretStore(tmp_path / "secrets.json")
    settings = _settings(tmp_path, auth_password="", first_run_setup=True)
    first = ensure_setup_code(settings, store)
    assert first
    # A second boot of a still-unclaimed instance must show the same code: the
    # one on the terminal that started the container is the one written down.
    second = _settings(tmp_path, auth_password="", first_run_setup=True)
    assert ensure_setup_code(second, store) == first


def test_a_claimed_instance_has_no_code_and_leaves_none_behind(tmp_path: Path) -> None:
    store = SecretStore(tmp_path / "secrets.json")
    unclaimed = _settings(tmp_path, auth_password="", first_run_setup=True)
    ensure_setup_code(unclaimed, store)
    assert SETUP_CODE_NAME in store.names()

    # Claiming by setting the environment variable never goes through the claim
    # route, so startup is what has to retire the code in that case.
    claimed = _settings(tmp_path, auth_password="hunter2", first_run_setup=True)
    assert ensure_setup_code(claimed, store) is None
    assert SETUP_CODE_NAME not in store.names()


def test_an_open_instance_never_mints_a_code(tmp_path: Path) -> None:
    store = SecretStore(tmp_path / "secrets.json")
    settings = _settings(tmp_path, auth_password="", first_run_setup=False)
    assert ensure_setup_code(settings, store) is None
    assert store.names() == []


def test_the_code_reads_the_way_it_was_printed(tmp_path: Path) -> None:
    store = SecretStore(tmp_path / "secrets.json")
    code = ensure_setup_code(_settings(tmp_path, auth_password="", first_run_setup=True), store)
    assert code is not None
    assert len(code) == 9  # four, a hyphen, four
    assert code[4] == "-"
    # No character anybody has to ask about over the phone.
    assert not set(code) & set("01OIL")


def test_a_code_is_accepted_however_it_was_typed(tmp_path: Path) -> None:
    store = SecretStore(tmp_path / "secrets.json")
    code = ensure_setup_code(_settings(tmp_path, auth_password="", first_run_setup=True), store)
    assert code is not None
    assert verify_setup_code(code, store)
    assert verify_setup_code(code.lower(), store)
    assert verify_setup_code(code.replace("-", ""), store)
    assert verify_setup_code(f"  {code.replace('-', ' ')}  ", store)


def test_a_wrong_or_missing_code_is_refused(tmp_path: Path) -> None:
    store = SecretStore(tmp_path / "secrets.json")
    assert not verify_setup_code("ABCD-EFGH", store)  # nothing stored at all
    ensure_setup_code(_settings(tmp_path, auth_password="", first_run_setup=True), store)
    assert not verify_setup_code("ABCD-EFGH", store)
    assert not verify_setup_code("", store)


def test_normalize_drops_only_presentation() -> None:
    assert normalize_code("ab-cd ef") == "ABCDEF"
    assert normalize_code("") == ""


def test_claiming_stores_the_password_and_destroys_the_code(tmp_path: Path) -> None:
    store = SecretStore(tmp_path / "secrets.json")
    settings = _settings(tmp_path, auth_password="", first_run_setup=True)
    ensure_setup_code(settings, store)
    claim_instance(settings, store, "a-good-password")
    assert settings.auth_password == "a-good-password"
    assert store.get(AUTH_PASSWORD_NAME) == "a-good-password"
    assert store.get(SETUP_CODE_NAME) is None
    assert instance_claimed(settings)
    assert not setup_required(settings)


def test_the_code_is_not_written_anywhere_but_the_secret_file(tmp_path: Path) -> None:
    """The file is 0600 and is the same one the provider keys already live in."""
    store = SecretStore(tmp_path / "secrets.json")
    code = ensure_setup_code(_settings(tmp_path, auth_password="", first_run_setup=True), store)
    assert code is not None
    written = [p for p in tmp_path.rglob("*") if p.is_file() and code in p.read_text()]
    assert written == [tmp_path / "secrets.json"]


# --- the password rules -----------------------------------------------------


def test_a_mismatch_is_refused_before_anything_is_stored() -> None:
    assert (
        password_problem("a-good-password", "a-good-passwrod") == "the two passwords do not match"
    )


def test_a_short_password_is_refused() -> None:
    problem = password_problem("short", "short")
    assert problem is not None
    assert str(MIN_PASSWORD_LENGTH) in problem


def test_a_long_matching_password_has_no_problem() -> None:
    assert password_problem("a-good-password", "a-good-password") is None


# --- what the gate lets through ---------------------------------------------


def test_the_gate_blocks_the_api_and_the_sockets() -> None:
    assert is_blocked("/api/providers")
    assert is_blocked("/api/system/healthz")
    assert is_blocked("/ws/transcript")
    assert is_blocked("/openapi.json")
    assert is_blocked("/docs")


def test_the_gate_lets_the_wizard_and_its_own_page_through() -> None:
    assert not is_blocked("/api/setup/state")
    assert not is_blocked("/api/setup/claim")
    assert not is_blocked("/api/system/livez")
    # The wizard is a page of the SPA, so an unclaimed instance has to serve it.
    assert not is_blocked("/setup")
    assert not is_blocked("/")
    assert not is_blocked("/_app/immutable/entry/app.js")
