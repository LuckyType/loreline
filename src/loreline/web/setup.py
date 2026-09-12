"""First run: the three states an instance can be in, and the claim between them.

An instance is in exactly one of three states, and the app has to be able to
tell them apart without guessing:

* **claimed** - a password is in force, from ``LORELINE_AUTH_PASSWORD`` or from
  the secret store. Everything behaves as it always has.
* **unclaimed** - ``first_run_setup`` is on and no password exists yet. Only
  the setup routes and ``/api/system/livez`` answer; everything else is 403.
* **open** - ``first_run_setup`` is off and no password exists. Today's
  auth-disabled behaviour, which a dev box and the test suite rely on.

The distinction that matters is the last one: an empty password has meant
"auth is off" since the first commit, so a fresh deployment cannot be
recognised from an empty password alone. ``first_run_setup`` is that fact, and
it defaults to on so that a ``docker run`` with no arguments is not a way to
leave a table's transcripts and API keys open on a LAN.

The **setup code** is the whole security argument for the unclaimed state.
Without it, whoever reaches the box first claims it and owns everything that
follows, which on a LAN or a tailnet is not a hypothetical. It is minted once
while unclaimed, persisted like any other app-managed secret so a restart does
not invent a new one, logged at every startup while unclaimed so a restart
shows it again, and deleted the moment a claim succeeds. It appears in exactly
one place, that log line, and never in an API response or an error body.
"""

from __future__ import annotations

import hmac
import secrets as token_gen
from typing import TYPE_CHECKING

from loreline.logging import get_logger
from loreline.web.auth import auth_enabled, store_auth_password

if TYPE_CHECKING:
    from loreline.secrets import SecretStore
    from loreline.settings import Settings

log = get_logger(__name__)

#: Where the minted setup code is kept, under the same ``0600`` file and the
#: same leading-underscore convention as the JWT secret and the password.
SETUP_CODE_NAME = "_setup_code"

#: Unambiguous when read aloud off a terminal and typed into a phone: no 0/O,
#: no 1/I/L. Eight picks from 31 symbols is just under 40 bits, which against
#: the login backoff (five tries, then a lockout window) is not guessable.
_CODE_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"
_CODE_LENGTH = 8
#: Printed and accepted in two groups, purely so a human can hold it in their
#: head between the terminal and the browser. The separator is not part of the
#: secret: it is stripped before anything is compared.
_CODE_GROUP = 4

#: Short enough not to be a hurdle on a LAN device, long enough that the thing
#: standing between a stranger and a table's transcripts is not four digits.
MIN_PASSWORD_LENGTH = 8


def instance_claimed(settings: Settings) -> bool:
    """Whether a password is in force, from either source."""
    return auth_enabled(settings)


def setup_required(settings: Settings) -> bool:
    """Whether this instance is waiting to be claimed.

    True only in the unclaimed state: the gate is closed, the setup code is
    live, and the browser is redirected to the wizard from anywhere.
    """
    return settings.first_run_setup and not instance_claimed(settings)


def _mint_code() -> str:
    """A fresh setup code, grouped for reading aloud."""
    raw = "".join(token_gen.choice(_CODE_ALPHABET) for _ in range(_CODE_LENGTH))
    return "-".join(raw[i : i + _CODE_GROUP] for i in range(0, _CODE_LENGTH, _CODE_GROUP))


def normalize_code(value: str) -> str:
    """The comparable form of a setup code, however it was typed.

    Case, spaces and the grouping hyphens are presentation: a GM reading the
    code off a terminal into a phone should not be told they got it wrong
    because they left the hyphen out or their keyboard capitalised the first
    letter. The alphabet has no character whose case is meaningful, so folding
    it costs no entropy at all.
    """
    return "".join(ch for ch in value.upper() if ch in _CODE_ALPHABET)


def ensure_setup_code(settings: Settings, store: SecretStore) -> str | None:
    """Mint, reuse or retire the setup code for the state the instance is in.

    Returns the live code while unclaimed, so the caller can log it, and None
    otherwise. Reused from the store rather than re-minted, because a restart
    that invented a new code would invalidate the one already written down on
    the terminal that started the container - and a stack that restarts on a
    crash loop would do it every few seconds.

    A claimed instance has no use for one, so any leftover is deleted here as
    well as at the claim: that is what cleans up an instance claimed by
    setting ``LORELINE_AUTH_PASSWORD`` and restarting, which never goes
    through the claim route at all.
    """
    if not setup_required(settings):
        # Only touch the file when there is something in it to remove; delete()
        # rewrites it either way, and a claimed instance boots with no reason
        # to write to its own secret store.
        if SETUP_CODE_NAME in store.names():
            store.delete(SETUP_CODE_NAME)
        return None
    existing = store.get(SETUP_CODE_NAME)
    if existing:
        return existing
    code = _mint_code()
    store.set(SETUP_CODE_NAME, code)
    return code


def announce_setup_code(settings: Settings, store: SecretStore) -> None:
    """Log the setup code at startup, while and only while it is needed.

    At warning level because it is the one line in the boot output a new
    operator has to find, and because the alternative to finding it is an
    instance nobody can claim. It stops appearing the moment the instance is
    claimed, which is also the moment the code stops existing.
    """
    code = ensure_setup_code(settings, store)
    if code is None:
        return
    log.warning(
        "setup.unclaimed",
        setup_code=code,
        hint="open Loreline in a browser and enter this setup code to claim this instance",
    )


def verify_setup_code(candidate: str, store: SecretStore) -> bool:
    """Constant-time check of a typed setup code against the stored one."""
    expected = store.get(SETUP_CODE_NAME)
    if not expected:
        return False
    return hmac.compare_digest(normalize_code(candidate), normalize_code(expected))


def password_problem(password: str, confirm: str) -> str | None:
    """What is wrong with a chosen password, as a sentence, or None.

    Both rules exist for the same reason: a password chosen here is stored and
    never shown again, so a typo locks the instance out and the only way back
    in is a shell on the host. Asking for it twice catches the typo; the floor
    keeps the LAN's one shared secret from being four characters.
    """
    if password != confirm:
        return "the two passwords do not match"
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"the password must be at least {MIN_PASSWORD_LENGTH} characters"
    return None


def claim_instance(settings: Settings, store: SecretStore, password: str) -> None:
    """Make ``password`` this instance's password and retire the setup code.

    The order is deliberate. The password is persisted and made live first, so
    that the instance is genuinely claimed before the code that let anyone
    claim it stops existing; a failure between the two leaves a claimed
    instance with a stale code, which the next startup deletes, rather than an
    unclaimable one.
    """
    store_auth_password(settings, store, password)
    store.delete(SETUP_CODE_NAME)
    log.info("setup.claimed")
