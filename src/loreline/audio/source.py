"""What a capture source says when it cannot serve.

The capture seam (``CaptureSource`` / ``PreflightCapture`` in
``loreline.session.manager``) lets a session be fed by a PortAudio device or by
a browser, and those two fail for entirely different reasons. A missing ALSA
card and a tab that never got microphone permission are not the same sentence,
and neither is fixed where the other is.

``SessionManager._preflight_capture`` composes the device sentence itself,
because a raw ``PortAudioError`` is not one. A source that already knows what a
GM should be told raises this instead, and the manager passes the message
through untouched.
"""

from __future__ import annotations


class CaptureUnavailableError(RuntimeError):
    """A capture source cannot serve, stated in words the GM can act on.

    The message is shown verbatim: it names what is wrong and where the fix
    is, so it must read as a whole sentence rather than as a fragment for
    somebody else to wrap.
    """
