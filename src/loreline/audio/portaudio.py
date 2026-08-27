"""Recovery from stale PortAudio host state.

A long-lived process can end up with PortAudio unable to open a perfectly
good device after the OS audio subsystem changes underneath it - most
commonly a USB audio interface going through a sleep/wake or unplug/replug
cycle. ``Pa_OpenStream`` then fails with a generic host error
(``PaErrorCode -9986`` / "Internal PortAudio error") for the rest of that
process's lifetime, even though the device is fine and a brand-new process
opens it without issue.

PortAudio's own fix for this is cycling its init state (``Pa_Terminate`` +
``Pa_Initialize``), which forces a fresh scan of the host audio state. That's
what a full app restart does as a side effect; this lets the capture layer do
the same cycle in place instead of requiring one.
"""

# pyright: reportMissingImports=false, reportUnknownMemberType=false
# pyright: reportMissingModuleSource=false

from __future__ import annotations

import contextlib

from loreline.logging import get_logger

log = get_logger(__name__)


def reinitialize() -> None:
    """Cycle PortAudio's init state to clear stale host/device handles."""
    import sounddevice as sd  # noqa: PLC0415

    # Termination can itself fail if PortAudio's state is already wedged;
    # that's fine, initialize() is what actually matters for recovery.
    with contextlib.suppress(Exception):
        sd._terminate()  # pyright: ignore[reportPrivateUsage]
    sd._initialize()  # pyright: ignore[reportPrivateUsage]
    log.info("audio.portaudio.reinitialized")
