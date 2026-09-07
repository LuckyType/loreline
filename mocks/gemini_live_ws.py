"""Mock Gemini Live API WebSocket server (``BidiGenerateContent``).

Replays the frame sequence a real session sends, recorded during the
verification run of ``gemini-3.5-transcribe-live``: ``setup`` answered with
``setupComplete``, then, per turn, cumulative ``interimInputTranscription``
partials (which connectors must not add to the transcript), one
``inputTranscription`` final, ``generationComplete`` to close the turn, and
the empty padding frames that follow it.

Two modes, chosen the way the real service chooses between them, by what the
``setup`` asks for:

**One session per utterance** (no ``realtimeInputConfig``), what the
``Connector`` shape opens. Two turns, placed where the real service puts them:
the first closes while audio is still being sent, the second only after
``audioStreamEnd``. That is the distinction that connector's receive loop rests
on, and a mock that answered only at the end could not express it.

**One session per capture** (``realtimeInputConfig.automaticActivityDetection``
present and not disabled), what the ``StreamingConnector`` shape opens. Here the
turns are the mock's own, cut from the audio by a loudness threshold rather than
a VAD model: enough to let a test drive turns by handing it loud and quiet
frames, and deterministic in a way a model would not be. The frames it sends per
turn are the recorded ones above, in the recorded order, which is the part worth
pinning: the final arrives *before* the turn end, so a connector that also
published on ``generationComplete`` would write every turn twice.

``go_away_after_turns`` sends the ``goAway`` the real service sends before it
drops a session at its duration cap, which on this vendor is normal operation
rather than a fault, so the reconnect path has something to be driven by. A
``setup`` asking for ``sessionResumption`` is answered with a handle, and every
handle a client presents is recorded, which is how a test tells a reconnect that
resumed from one that started over.

The real service never sends ``turnComplete`` and never closes the socket
itself, and neither does this. Deterministic, so tests can assert wiring; what
it cannot vouch for is server-side VAD, which is what let an earlier
docs-shaped mock look right while a real session returned nothing.
"""

from __future__ import annotations

import array
import base64
import json
from typing import cast

from websockets.asyncio.server import ServerConnection
from websockets.exceptions import ConnectionClosed

# Mean absolute sample value at or above which a block counts as speech. Well
# clear of the near-silent filler a test sends between turns, and well under a
# normal speaking level.
_SPEECH_LEVEL = 500
# Silence that closes a turn, matching what the connector asks for in
# automaticActivityDetection.silenceDurationMs.
_SILENCE_MS = 500
# One more word of interim per this much speech, so a turn produces several
# cumulative partials before its final.
_INTERIM_MS = 200
_RATE = 16_000

_WORDS = ("the", "goblin", "takes", "the", "chest", "and", "runs")


def _content(payload: dict[str, object]) -> str:
    return json.dumps({"serverContent": payload})


def turn_frames(text: str, *, trailing_empties: int) -> list[str]:
    """One recorded turn: partials, final, end of turn, padding.

    ``trailing_empties`` is two mid-session (one closes the turn, one heralds
    the next) and one for the turn the session ends on.
    """
    return [
        _content({"interimInputTranscription": {"text": text.split(maxsplit=1)[0]}}),
        _content({"interimInputTranscription": {"text": text}}),
        _content({"inputTranscription": {"text": text}}),
        _content({"generationComplete": True}),
        *(_content({}) for _ in range(trailing_empties)),
    ]


class _LiveVad:
    """The streaming mode's turn machinery: loudness in, recorded frames out.

    One turn open at a time, exactly as the protocol allows, since nothing on
    this wire names a turn.
    """

    def __init__(self) -> None:
        self.open = False
        self.turns = 0
        self._silence_ms = 0.0
        self._since_interim = 0.0
        self._words = 0

    def feed(self, pcm: bytes) -> list[str]:
        """One audio frame; the frames the real service would have sent back."""
        block_ms = len(pcm) / 2 / _RATE * 1000
        if _loud(pcm):
            self._silence_ms = 0.0
            if not self.open:
                self.open = True
                self._words = 1
                self._since_interim = 0.0
                return [self._interim()]
            self._since_interim += block_ms
            if self._since_interim >= _INTERIM_MS and self._words < len(_WORDS):
                self._since_interim = 0.0
                self._words += 1
                return [self._interim()]
            return []
        if self.open:
            self._silence_ms += block_ms
            if self._silence_ms >= _SILENCE_MS:
                return self.close(trailing_empties=2)
        return []

    def close(self, *, trailing_empties: int) -> list[str]:
        """End the open turn, if there is one, as the service ends one.

        The final comes before the turn end and the padding comes after it, in
        that order, because that is the order it was recorded in.
        """
        if not self.open:
            return []
        self.open = False
        self.turns += 1
        return [
            _content({"inputTranscription": {"text": self._text()}}),
            _content({"generationComplete": True}),
            *(_content({}) for _ in range(trailing_empties)),
        ]

    def _interim(self) -> str:
        """A partial: cumulative, so it replaces its predecessor."""
        return _content({"interimInputTranscription": {"text": self._text()}})

    def _text(self) -> str:
        return " ".join(_WORDS[: self._words])


def _loud(pcm: bytes) -> bool:
    """Whether a block is speech, by mean absolute sample value."""
    samples = array.array("h")
    samples.frombytes(pcm[: len(pcm) // 2 * 2])
    return bool(samples) and sum(abs(s) for s in samples) / len(samples) >= _SPEECH_LEVEL


async def _send(websocket: ServerConnection, frames: list[str]) -> None:
    """Send frames, ignoring a client that has already hung up.

    A connector is entitled to close as soon as the turn it was waiting for
    ends, which can be mid-way through this turn's trailing padding.
    """
    for frame in frames:
        try:
            await websocket.send(frame)
        except ConnectionClosed:
            return


def _streams(setup: dict[str, object]) -> bool:
    """Whether this setup asked for the service's own endpointing."""
    realtime = setup.get("realtimeInputConfig")
    if not isinstance(realtime, dict):
        return False
    detection = cast("dict[str, object]", realtime).get("automaticActivityDetection")
    return isinstance(detection, dict) and not cast("dict[str, object]", detection).get("disabled")


def _decoded(realtime: dict[str, object]) -> bytes | None:
    """The PCM in a ``realtimeInput`` frame, or None if it carries no audio."""
    audio = realtime.get("audio")
    if not isinstance(audio, dict):
        return None
    encoded = cast("dict[str, object]", audio).get("data")
    return base64.b64decode(encoded) if isinstance(encoded, str) else None


def _resumption(setup: dict[str, object], handles: list[str | None] | None) -> list[str]:
    """Answer a setup that asked to be resumable, and record what it presented.

    The handle offered back is derived from the one presented rather than fixed,
    so a test can tell the second connection's handle from the first's and see
    that a reconnect continued a session rather than starting one.
    """
    config = setup.get("sessionResumption")
    if not isinstance(config, dict):
        return []
    presented = cast("dict[str, object]", config).get("handle")
    handle = presented if isinstance(presented, str) else None
    if handles is not None:
        handles.append(handle)
    return [json.dumps({"sessionResumptionUpdate": {"newHandle": f"{handle or 'h'}+1"}})]


async def gemini_live_handler(
    websocket: ServerConnection,
    *,
    setups: list[dict[str, object]] | None = None,
    go_away_after_turns: int | None = None,
    handles: list[str | None] | None = None,
) -> None:
    """Handle one mock Live session, in whichever mode its setup asked for.

    ``setups`` collects each session's ``setup`` body, since that message is
    the connector's only chance to configure a session and both the custom
    vocabulary and the activity detection ride in it. ``handles`` collects the
    resumption handle each session presented, None for one that presented none.
    Bind any of them with ``functools.partial``.
    """
    samples = 0
    mid_turn_sent = False
    streaming = False
    vad = _LiveVad()
    async for message in websocket:
        if isinstance(message, bytes):
            continue
        data = cast("dict[str, object]", json.loads(message))
        if "setup" in data:
            setup = cast("dict[str, object]", data["setup"])
            if setups is not None:
                setups.append(setup)
            streaming = _streams(setup)
            await _send(websocket, [json.dumps({"setupComplete": {}}), _content({})])
            await _send(websocket, _resumption(setup, handles))
            continue
        realtime = data.get("realtimeInput")
        if not isinstance(realtime, dict):
            continue
        realtime_map = cast("dict[str, object]", realtime)
        pcm = _decoded(realtime_map)
        if pcm is not None:
            samples += len(pcm) // 2
            if streaming:
                await _send(websocket, vad.feed(pcm))
                if go_away_after_turns is not None and vad.turns >= go_away_after_turns:
                    go_away_after_turns = None
                    await _send(websocket, [json.dumps({"goAway": {"timeLeft": "5s"}})])
            elif not mid_turn_sent:
                mid_turn_sent = True
                await _send(websocket, turn_frames("gemini live mock", trailing_empties=2))
        if realtime_map.get("audioStreamEnd"):
            if streaming:
                await _send(websocket, vad.close(trailing_empties=1))
                continue
            await _send(websocket, turn_frames(f"{samples} samples", trailing_empties=1))
            # Deliberately no close and no further frame: the real service
            # goes quiet here and leaves the socket open, so returning (which
            # would close it) would hand the connector an end-of-session
            # signal that production never gives it.
