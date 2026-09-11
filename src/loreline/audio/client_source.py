"""Capture from the client device's microphone, over a WebSocket.

The browser on the table already has a microphone; the machine running Loreline
may have none at all. A **client microphone** is that browser feeding the
session: ``getUserMedia`` in the tab, an ``AudioWorklet`` turning Float32 into
PCM16, and a socket carrying the frames to ``WS /ws/audio/capture``.

Nothing below the capture seam knows about any of it. ``ClientCaptureSource``
is a second :class:`~loreline.session.manager.CaptureSource`, "a stoppable
source of timestamped PCM frames", and the capture loop, ``_CaptureStats``, the
level publisher, the disk watch, the WAV writer, ``StreamPath`` and
``SttRouter`` consume it exactly as they consume a PortAudio device. Frames are
resampled from the browser's rate to the session's with the same
``Pcm16Stream`` ``SoundDeviceSource`` uses for a device that serves another
rate, and re-blocked to the fixed 20 ms frame the chunker is built around.

Two things are this module's own, because a socket is not a sound card:

* **The clock is the server's.** Every frame is stamped with ``time.monotonic``
  at the moment it arrives here, never from anything the browser said. A client
  clock is a clock nobody can check, and the session clock is what the
  transcript, the WAV index and the player are all measured against.
* **A socket can come back.** A laptop that slept for twenty seconds, or a
  wifi blip, must not end the evening's recording, so the source survives a
  disconnect for ``reconnect_window_s`` and the browser may re-open the socket
  and resume feeding the same session. The audio that did not arrive is padded
  with silence on resume, so the recording's byte offset stays the session
  clock: without that, every timestamp after a reconnect would point at the
  wrong second of the stored WAV for the rest of the evening. Past the window
  the source raises, and the session ends the way a dead device ends it.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass

from loreline.audio.resample import Pcm16Stream, downmix_pcm16
from loreline.audio.source import CaptureUnavailableError
from loreline.logging import get_logger

log = get_logger(__name__)

_BYTES_PER_SAMPLE = 2  # mono s16le, the one format capture writes
_FRAME_MS = 20  # what VadChunker is built around, and what the device source yields

# How long a browser may be away before its session ends. Long enough for a
# lid closed while someone moves rooms and for a wifi roam, short enough that a
# table which has genuinely packed up is not left "recording" for minutes.
_RECONNECT_WINDOW_S = 45.0
# How recently the socket must have delivered audio for Start to be allowed.
# Frames arrive every few tens of milliseconds, so this is generous; what it
# refuses is a tab that opened the socket and never got permission, which
# would otherwise start a session that records silence.
_PREFLIGHT_FRESH_S = 3.0
# Frames the source will hold for a consumer that is behind. A reconnect pads
# up to _RECONNECT_WINDOW_S of silence in one go, which is 2250 frames, so this
# has to clear that or a resumed session would drop the gap it just measured.
_QUEUE_MAX = 4096
# How long frames() waits on an empty queue before re-checking whether the
# browser has been gone too long. Also the resolution of that check.
_POLL_INTERVAL_S = 0.25

# Rates a browser could plausibly be running an AudioContext at. Outside this a
# hello is a bug or a hostile client, and building a resampler for it would
# either explode or silently produce garbage.
_MIN_RATE = 8000
_MAX_RATE = 192000
_MAX_CHANNELS = 8


class ClientMicBusyError(RuntimeError):
    """A second browser tried to stream while one is already streaming.

    There is at most one capture per process, so there is at most one client
    capture socket. A second one is refused with a close code and a reason,
    never silently swapped: swapping would hand the running session's audio to
    whichever tab connected last, which is both invisible and unrecoverable.
    """


@dataclass(frozen=True, slots=True)
class ClientHello:
    """What a browser says about itself before it sends a single frame.

    ``label`` is for the log only. It is whatever the browser called the
    chosen input ("MacBook Pro Microphone"), which is the only way an operator
    reading a session's log can tell which device at the table was recording.
    """

    sample_rate: int
    channels: int = 1
    label: str = ""

    def validated(self) -> ClientHello:
        """Refuse a hello no resampler could be built for."""
        if not _MIN_RATE <= self.sample_rate <= _MAX_RATE:
            msg = f"sample_rate {self.sample_rate} is not a plausible capture rate"
            raise ValueError(msg)
        if not 1 <= self.channels <= _MAX_CHANNELS:
            msg = f"channels {self.channels} is not a plausible channel count"
            raise ValueError(msg)
        return self


class ClientCaptureSource:
    """Frame source fed by a browser's microphone over a WebSocket.

    Built by the capture factory when a start request names the client source,
    handed the rate the browser's last hello declared and the rate the session
    wants. From there it is an ordinary ``CaptureSource``: ``frames()`` yields
    fixed-size ``sample_rate`` frames with a server timestamp, and ``stop()``
    ends the iteration.

    ``submit`` is called from the socket handler on the event loop. Resampling
    a 20 ms block costs microseconds, so it runs there rather than behind a
    thread hop that would add latency to every frame for no measurable gain.
    """

    def __init__(
        self,
        *,
        sample_rate: int,
        client_rate: int,
        channels: int = 1,
        connected: bool = True,
        last_frame_mono: float | None = None,
        frame_ms: int = _FRAME_MS,
        fresh_within_s: float = _PREFLIGHT_FRESH_S,
        reconnect_window_s: float = _RECONNECT_WINDOW_S,
    ) -> None:
        self._sample_rate = sample_rate
        self._frame_bytes = int(sample_rate * frame_ms / 1000) * _BYTES_PER_SAMPLE
        self._frame_s = frame_ms / 1000.0
        self._fresh_within_s = fresh_within_s
        self._reconnect_window_s = reconnect_window_s
        self._queue: asyncio.Queue[tuple[bytes, float]] = asyncio.Queue(maxsize=_QUEUE_MAX)
        self._pending = bytearray()
        self._stop = asyncio.Event()
        self._last_frame_mono = last_frame_mono
        # None while a socket is attached; the moment it went away otherwise.
        self._lost_at: float | None = None if connected else time.monotonic()
        self._channels = channels
        self._client_rate = client_rate
        self._resampler: Pcm16Stream | None = None
        self._resampler_error: str | None = None
        self._build_resampler(client_rate)

    @property
    def sample_rate(self) -> int:
        """The rate frames come out at, which is the session's, not the browser's."""
        return self._sample_rate

    @property
    def client_rate(self) -> int:
        """The rate the browser is sending at, which is what gets resampled."""
        return self._client_rate

    @property
    def stopped(self) -> bool:
        """True once the session has stopped this source (see :meth:`stop`)."""
        return self._stop.is_set()

    @property
    def pending_frames(self) -> int:
        """Frames waiting for the capture loop to take them.

        The one window into a source that is being fed but not yet read, which
        is most of a source's life: the socket runs from the moment the GM
        turns the microphone on, and the capture loop only starts consuming
        when a session does.
        """
        return self._queue.qsize()

    def stop(self) -> None:
        self._stop.set()

    # --- the socket's side -------------------------------------------------

    def rewire(self, hello: ClientHello) -> None:
        """Point the source at a freshly connected socket.

        A reconnect gets a new resampler rather than the old one continued:
        its filter state describes audio that stopped arriving, the browser may
        have come back at a different rate entirely (a different input, a
        different AudioContext), and the part-frame left over from the last
        block is audio from before the gap that must not be spliced onto the
        first block after it.
        """
        gap = 0.0 if self._lost_at is None else time.monotonic() - self._lost_at
        self._lost_at = None
        self._channels = hello.channels
        if hello.sample_rate != self._client_rate or self._pending:
            self._client_rate = hello.sample_rate
            self._pending.clear()
            self._build_resampler(hello.sample_rate)
        if gap > 0:
            self._pad_silence(gap)
            log.info(
                "audio.client.audio_resumed",
                gap_seconds=round(gap, 2),
                client_rate=hello.sample_rate,
            )

    def socket_lost(self) -> None:
        """Note that the browser has gone, starting the reconnect window."""
        if self._lost_at is not None:
            return
        self._lost_at = time.monotonic()
        log.warning("audio.client.audio_stopped", window_seconds=self._reconnect_window_s)

    def submit(self, pcm: bytes) -> None:
        """Take one block of PCM16 from the socket and frame it for the session."""
        resampler = self._resampler
        if self._stop.is_set() or resampler is None or not pcm:
            return
        now = time.monotonic()
        self._last_frame_mono = now
        self._pending += resampler.process(downmix_pcm16(pcm, self._channels))
        self._drain(now)

    # --- the session's side ------------------------------------------------

    def _build_resampler(self, client_rate: int) -> None:
        """Build the rate converter for this socket, remembering a refusal.

        A browser at the session's own rate needs no native library at all, so
        the common homelab case (a box with no sound hardware, and no reason to
        have installed the ``audio`` extra's resampler) is a pass-through.
        Where one is needed and missing, the start is refused at the pre-flight
        with a sentence saying so, rather than crashing inside the socket
        handler once the session is already declared started.
        """
        try:
            self._resampler = Pcm16Stream(client_rate, self._sample_rate)
            self._resampler_error = None
        except RuntimeError as exc:
            self._resampler = None
            self._resampler_error = str(exc)
            log.error("audio.client.resampler_missing", error=str(exc))

    async def preflight(self) -> None:
        """Refuse the start unless this browser is actually streaming audio.

        The browser equivalent of opening the device: a GM who has not granted
        microphone permission, or whose tab has gone, fails Start and is told
        so, rather than starting a session that reports "capturing" over a WAV
        that never grows. Frames that arrived before the session existed are
        what makes this answerable - they are discarded, but they keep the
        pre-flight fresh, so opening the microphone early costs nothing and
        proves everything.
        """
        if self._lost_at is not None:
            msg = (
                "This browser is not streaming audio to Loreline. Turn on this device's "
                "microphone in the capture card and wait for the level meter to move, "
                "then start the session."
            )
            raise CaptureUnavailableError(msg)
        if self._resampler_error is not None:
            msg = (
                f"This browser records at {self._client_rate} Hz and the session needs "
                f"{self._sample_rate} Hz, but the resampler is not installed on the server "
                f"({self._resampler_error})."
            )
            raise CaptureUnavailableError(msg)
        age = None if self._last_frame_mono is None else time.monotonic() - self._last_frame_mono
        if age is None or age > self._fresh_within_s:
            msg = (
                "The connection from this browser is open but no audio has come through it. "
                "Check that the microphone is allowed for this site and that the right input "
                "is selected, then start the session."
            )
            raise CaptureUnavailableError(msg)

    async def frames(self) -> AsyncIterator[tuple[bytes, float]]:
        log.info(
            "audio.client.capture.start",
            rate=self._sample_rate,
            client_rate=self._client_rate,
        )
        try:
            while not self._stop.is_set():
                try:
                    frame = await asyncio.wait_for(self._queue.get(), timeout=_POLL_INTERVAL_S)
                except TimeoutError:
                    self._check_still_there()
                    continue
                yield frame
        finally:
            log.info("audio.client.capture.stop")

    # --- internals ---------------------------------------------------------

    def _check_still_there(self) -> None:
        """End the capture once the browser has been gone past the window."""
        if self._lost_at is None:
            return
        away = time.monotonic() - self._lost_at
        if away < self._reconnect_window_s:
            return
        log.error("audio.client.gave_up", away_seconds=round(away, 1))
        msg = (
            f"The browser that was recording has not sent audio for {round(away)} seconds "
            "and did not come back. The recording was closed; everything captured before "
            "that is saved."
        )
        raise CaptureUnavailableError(msg)

    def _pad_silence(self, seconds: float) -> None:
        """Stand in for the audio a disconnected browser could not send.

        Capped at the reconnect window, which is the longest gap this source
        ever survives, so a clock jump cannot turn into an hour of silence
        written to disk.
        """
        span = min(seconds, self._reconnect_window_s)
        frames = int(span / self._frame_s)
        if frames <= 0:
            return
        self._pending += bytes(frames * self._frame_bytes)
        self._drain(time.monotonic())

    def _drain(self, now: float) -> None:
        """Cut whatever whole frames have accumulated and queue them.

        soxr hands back whatever whole samples its filter has ready, so the
        resampled blocks do not line up with the frames downstream expects;
        re-blocking here keeps the fixed frame size the only thing a caller has
        to know about this source, exactly as ``SoundDeviceSource`` does.

        The last frame of a block is stamped with the moment the block arrived
        and the ones before it are walked back a frame at a time, so a burst
        (a resumed socket's silence padding, a delayed packet carrying several
        frames) carries timestamps that advance rather than a pile sharing one.
        """
        ready = len(self._pending) // self._frame_bytes
        for index in range(ready):
            frame = bytes(self._pending[: self._frame_bytes])
            del self._pending[: self._frame_bytes]
            self._offer((frame, now - (ready - 1 - index) * self._frame_s))

    def _offer(self, frame: tuple[bytes, float]) -> None:
        """Queue a frame, dropping the oldest rather than blocking the socket."""
        if self._queue.full():
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:  # pragma: no cover - single-threaded loop
                pass
            else:
                log.warning("audio.client.frame_dropped")  # the capture loop is behind
        self._queue.put_nowait(frame)


class ClientMic:
    """The one browser socket that may feed a capture, and what it last sent.

    ``SessionManager`` holds one ``_Runtime`` behind a lock, so there is at
    most one capture per process and therefore at most one client capture
    socket. That is the invariant this holds: :meth:`connect` refuses a second
    socket with :class:`ClientMicBusyError` rather than swapping it in, and the
    route turns that into a close code and a reason the tab can show.

    Frames arriving with no session running are discarded, and only their
    arrival time is kept. That is what lets a GM open the microphone, watch the
    meter and *then* press Start: the pre-flight asks this object how recently
    audio came through, so opening early costs nothing and proves the one thing
    Start needs to know.
    """

    def __init__(
        self,
        *,
        reconnect_window_s: float = _RECONNECT_WINDOW_S,
        preflight_fresh_s: float = _PREFLIGHT_FRESH_S,
    ) -> None:
        self._reconnect_window_s = reconnect_window_s
        self._preflight_fresh_s = preflight_fresh_s
        self._token: object | None = None
        self._hello: ClientHello | None = None
        self._last_frame_mono: float | None = None
        self._source: ClientCaptureSource | None = None

    @property
    def connected(self) -> bool:
        """True while a browser holds the capture socket."""
        return self._token is not None

    @property
    def hello(self) -> ClientHello | None:
        """What the browser holding the socket said about itself, or None."""
        return self._hello

    @property
    def last_frame_age(self) -> float | None:
        """Seconds since this browser last delivered audio, or None if never."""
        if self._last_frame_mono is None:
            return None
        return max(0.0, time.monotonic() - self._last_frame_mono)

    def connect(self, hello: ClientHello) -> object:
        """Take the capture socket for this browser; refuse if one holds it.

        Returns an opaque token the route hands back to :meth:`feed` and
        :meth:`disconnect`, so a socket that has already been replaced cannot
        keep writing into a session that has moved on.
        """
        if self._token is not None:
            raise ClientMicBusyError
        token = object()
        self._token = token
        self._hello = hello
        log.info(
            "audio.client.connected",
            client_rate=hello.sample_rate,
            channels=hello.channels,
            device=hello.label or None,
            capturing=self._source is not None and not self._source.stopped,
        )
        source = self._live_source()
        if source is not None:
            source.rewire(hello)
        return token

    def disconnect(self, token: object) -> None:
        """Release the socket, if this token still holds it."""
        if self._token is not token:
            return
        self._token = None
        self._hello = None
        log.info("audio.client.disconnected")
        source = self._live_source()
        if source is not None:
            source.socket_lost()

    def feed(self, token: object, pcm: bytes) -> None:
        """Hand one block of PCM16 to whoever is recording, or to nobody."""
        if self._token is not token:
            return
        self._last_frame_mono = time.monotonic()
        source = self._live_source()
        if source is not None:
            source.submit(pcm)

    def open(self, sample_rate: int) -> ClientCaptureSource:
        """Build the capture source for a session starting on this browser.

        Never raises: a start with no socket has to fail as a refused request
        with a sentence about the browser, and that is the pre-flight's job
        (``SessionManager.start`` builds the source first and pre-flights it
        second). So a source is built either way, and one built with no socket
        behind it is already in its reconnect window and refuses.
        """
        hello = self._hello
        source = ClientCaptureSource(
            sample_rate=sample_rate,
            client_rate=hello.sample_rate if hello is not None else sample_rate,
            channels=hello.channels if hello is not None else 1,
            connected=self.connected,
            last_frame_mono=self._last_frame_mono,
            fresh_within_s=self._preflight_fresh_s,
            reconnect_window_s=self._reconnect_window_s,
        )
        self._source = source
        log.info(
            "audio.client.capture.open",
            rate=sample_rate,
            client_rate=source.client_rate,
            connected=self.connected,
        )
        return source

    def _live_source(self) -> ClientCaptureSource | None:
        """The source still recording, forgetting one the session has stopped."""
        source = self._source
        if source is None:
            return None
        if source.stopped:
            self._source = None
            return None
        return source
