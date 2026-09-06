"""Logging configuration that survives being applied twice in one process.

One process builds one app in production, but a test session builds dozens, and
each one reconfigures logging. structlog caches a bound logger's processor chain
the first time that logger is used, so a reconfigure that hands it a *new* chain
leaves every logger already in use on the old one - two sets of loggers, only
one of which anything can still reach.
"""

from __future__ import annotations

from structlog.testing import capture_logs

from loreline.logging import configure_logging, get_logger


def test_a_reconfigure_keeps_cached_loggers_on_the_live_chain() -> None:
    """A logger used before the second app is built still logs through it.

    ``structlog.testing.capture_logs`` swaps processors by mutating the
    configured list in place, exactly because bound loggers hold that list by
    reference. A logger stranded on a previous list is therefore invisible to
    it: its lines keep getting printed instead of captured, and a test that
    asserts on them passes or fails on what happened to run before it.
    """
    configure_logging()
    log = get_logger("test.cached_before_reconfigure")
    log.info("caches the chain")

    configure_logging()  # a second app in the same process

    with capture_logs() as entries:
        log.info("after the reconfigure")

    assert [entry["event"] for entry in entries] == ["after the reconfigure"]
