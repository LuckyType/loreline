"""The client microphone: a browser's frames as an ordinary capture source.

``ClientCaptureSource`` is a second ``CaptureSource``, so what these check is
the contract the capture loop reads it through - fixed-size frames at the
session's rate, a server timestamp on each, a clean stop - plus the two things
a socket has and a sound card does not: a pre-flight that means "this browser
is really streaming", and a disconnect that a reconnect can survive.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, cast

import pytest

from loreline.audio import client_source
from loreline.audio.client_source import (
    ClientCaptureSource,
    ClientHello,
    ClientMic,
    ClientMicBusyError,
)
from loreline.audio.source import CaptureUnavailableError
from loreline.models import CaptureSourceKind
from loreline.session.manager import capture_factory_for

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

_RATE = 16000
_FRAME_MS = 20
_FRAME_BYTES = int(_RATE * _FRAME_MS / 1000) * 2  # 320 samples, mono s16le


def _block(samples: int, value: int = 1) -> bytes:
    return value.to_bytes(2, "little", signed=True) * samples


def _frames(source: ClientCaptureSource) -> AsyncGenerator[tuple[bytes, float], None]:
    """``frames()`` as the generator it is, so a test can close it."""
    return cast("AsyncGenerator[tuple[bytes, float], None]", source.frames())


async def _collect(
    frames: AsyncGenerator[tuple[bytes, float], None], count: int
) -> list[tuple[bytes, float]]:
    """Take ``count`` frames, or fail rather than hang if they never come."""
    out: list[tuple[bytes, float]] = []

    async def take() -> None:
        async for frame in frames:
            out.append(frame)
            if len(out) == count:
                return

    await asyncio.wait_for(take(), timeout=5)
    return out


async def _collect_voiced(
    frames: AsyncGenerator[tuple[bytes, float], None], count: int
) -> list[tuple[bytes, float]]:
    """Take frames until ``count`` of them carry something other than silence."""
    out: list[tuple[bytes, float]] = []

    async def take() -> None:
        voiced = 0
        async for frame, ts in frames:
            out.append((frame, ts))
            if frame.strip(b"\x00"):
                voiced += 1
                if voiced == count:
                    return

    await asyncio.wait_for(take(), timeout=5)
    return out


def _source(**kwargs: object) -> ClientCaptureSource:
    defaults: dict[str, object] = {"sample_rate": _RATE, "client_rate": _RATE}
    defaults.update(kwargs)
    return ClientCaptureSource(**defaults)  # type: ignore[arg-type]


async def test_yields_what_the_socket_feeds() -> None:
    """Whatever arrives comes back out as fixed-size frames, in order."""
    source = _source()
    source.submit(_block(320, 1) + _block(320, 2))
    frames = _frames(source)
    got = await _collect(frames, 2)
    assert [len(frame) for frame, _ts in got] == [_FRAME_BYTES, _FRAME_BYTES]
    assert got[0][0] == _block(320, 1)
    assert got[1][0] == _block(320, 2)
    await frames.aclose()


async def test_reblocks_a_socket_that_sends_odd_sizes() -> None:
    """A browser's buffer size is its own business; the frame size is ours.

    An ``AudioWorklet`` delivers 128 samples at a time and whatever the client
    batches them into is not 20 ms of anything, so the source re-blocks - the
    same thing ``SoundDeviceSource`` does with what soxr hands back.
    """
    source = _source()
    for _ in range(5):
        source.submit(_block(128))  # 640 samples over five calls: two frames
    frames = _frames(source)
    got = await _collect(frames, 2)
    assert all(len(frame) == _FRAME_BYTES for frame, _ts in got)
    await frames.aclose()


async def test_stamps_frames_on_arrival_at_the_server() -> None:
    """The clock is the server's, and it advances across a block's frames."""
    before = time.monotonic()
    source = _source()
    source.submit(_block(320 * 3))
    frames = _frames(source)
    got = await _collect(frames, 3)
    after = time.monotonic()
    stamps = [ts for _frame, ts in got]
    assert stamps == sorted(stamps)
    assert before - 0.1 <= stamps[0] <= after
    assert abs((stamps[-1] - stamps[0]) - 2 * _FRAME_MS / 1000) < 1e-6
    await frames.aclose()


async def test_resamples_when_the_hello_rate_differs() -> None:
    """48 kHz in, 16 kHz out: a third of the samples, same seconds of audio."""
    pytest.importorskip("soxr")  # the streaming resampler, from the audio extra
    source = _source(client_rate=48000)
    # One second of 48 kHz audio; soxr holds a tail back, so this is "about".
    source.submit(_block(48000))
    frames = _frames(source)
    got = await _collect(frames, 45)
    assert all(len(frame) == _FRAME_BYTES for frame, _ts in got)
    await frames.aclose()


async def test_stop_ends_the_iteration() -> None:
    source = _source()
    source.submit(_block(320))
    frames = _frames(source)
    await _collect(frames, 1)
    source.stop()
    assert source.stopped
    # No frame left and the source stopped: the loop ends rather than waiting.
    rest = [frame async for frame in frames]
    assert rest == []


async def test_preflight_refuses_a_browser_that_sent_nothing() -> None:
    source = _source(last_frame_mono=None)
    with pytest.raises(CaptureUnavailableError, match="no audio has come through"):
        await source.preflight()


async def test_preflight_refuses_a_browser_that_is_not_connected() -> None:
    source = _source(connected=False, last_frame_mono=time.monotonic())
    with pytest.raises(CaptureUnavailableError, match="not streaming audio"):
        await source.preflight()


async def test_preflight_refuses_audio_that_stopped_a_while_ago() -> None:
    source = _source(last_frame_mono=time.monotonic() - 30, fresh_within_s=1.0)
    with pytest.raises(CaptureUnavailableError, match="no audio has come through"):
        await source.preflight()


async def test_preflight_passes_for_a_browser_that_is_streaming() -> None:
    source = _source(last_frame_mono=time.monotonic())
    await source.preflight()  # does not raise


async def test_a_reconnect_pads_the_audio_that_never_arrived() -> None:
    """The WAV's byte offset is the session clock, gap or no gap.

    Without this, every timestamp after a twenty-second wifi blip would point
    at the wrong second of the stored recording for the rest of the evening.
    """
    source = _source(reconnect_window_s=30.0)
    source.submit(_block(320))
    source.socket_lost()
    await asyncio.sleep(0.15)  # the browser is away
    source.rewire(ClientHello(sample_rate=_RATE))
    source.submit(_block(320))
    frames = _frames(source)
    got = await _collect_voiced(frames, 2)
    padded = got[1:-1]
    assert got[0][0] == _block(320, 1)
    assert got[-1][0] == _block(320, 1)
    assert padded, "the gap was not padded at all"
    assert all(frame == bytes(_FRAME_BYTES) for frame, _ts in padded)
    # 150 ms of gap is seven 20 ms frames, give or take the scheduler.
    assert 5 <= len(padded) <= 12
    await frames.aclose()


async def test_padding_is_capped_at_the_reconnect_window() -> None:
    source = _source(reconnect_window_s=0.06)
    source.socket_lost()
    await asyncio.sleep(0.25)  # far past the window; a clock jump is worse still
    source.rewire(ClientHello(sample_rate=_RATE))
    # Three frames of padding at most, not however long the browser was away.
    assert source.pending_frames <= 3
    assert source.pending_frames >= 1


async def test_a_browser_that_never_comes_back_ends_the_capture() -> None:
    """Past the window this dies like a dead device: the session ends in error."""
    source = _source(reconnect_window_s=0.01)
    source.socket_lost()
    frames = _frames(source)
    with pytest.raises(CaptureUnavailableError, match="did not come back"):
        await _collect(frames, 1)


async def test_a_browser_inside_the_window_keeps_the_session() -> None:
    source = _source(reconnect_window_s=30.0)
    source.socket_lost()
    frames = _frames(source)
    source.submit(_block(320))
    got = await _collect(frames, 1)
    assert got[0][0] == _block(320, 1)
    await frames.aclose()


def test_a_second_browser_is_refused_never_swapped() -> None:
    """One capture per process means one capture socket, and it is held."""
    mic = ClientMic()
    first = mic.connect(ClientHello(sample_rate=48000, label="Jabra"))
    with pytest.raises(ClientMicBusyError):
        mic.connect(ClientHello(sample_rate=44100))
    assert mic.connected
    assert mic.hello is not None
    assert mic.hello.sample_rate == 48000
    mic.disconnect(first)
    assert not mic.connected
    second = mic.connect(ClientHello(sample_rate=44100))
    assert mic.hello is not None
    assert mic.hello.sample_rate == 44100
    mic.disconnect(second)


def test_frames_before_a_session_keep_the_preflight_fresh() -> None:
    """Opening the microphone early costs nothing and proves everything."""
    mic = ClientMic()
    assert mic.last_frame_age is None
    token = mic.connect(ClientHello(sample_rate=_RATE))
    mic.feed(token, _block(320))
    age = mic.last_frame_age
    assert age is not None
    assert age < 1.0


def test_a_stale_token_cannot_write_into_the_session() -> None:
    """A socket that has been replaced is a socket that has stopped mattering."""
    mic = ClientMic()
    stale = mic.connect(ClientHello(sample_rate=_RATE))
    mic.disconnect(stale)
    current = mic.connect(ClientHello(sample_rate=_RATE))
    source = mic.open(_RATE)
    mic.feed(stale, _block(320))
    assert source.pending_frames == 0
    mic.feed(current, _block(320))
    assert source.pending_frames == 1


async def test_open_with_no_socket_refuses_at_preflight_not_at_build() -> None:
    """A start with no browser streaming is a refused request, not a crash.

    ``SessionManager.start`` builds the source and pre-flights it, in that
    order, and only the second of those can answer a start request with 400.
    """
    mic = ClientMic()
    source = mic.open(_RATE)
    with pytest.raises(CaptureUnavailableError, match="not streaming audio"):
        await source.preflight()


async def test_a_reconnect_at_another_rate_rebuilds_the_resampler() -> None:
    """A browser can come back on a different input, and so a different rate."""
    pytest.importorskip("soxr")
    mic = ClientMic()
    first = mic.connect(ClientHello(sample_rate=_RATE))
    source = mic.open(_RATE)
    assert source.client_rate == _RATE
    mic.disconnect(first)
    second = mic.connect(ClientHello(sample_rate=48000))
    assert source.client_rate == 48000
    mic.feed(second, _block(4800))
    frames = _frames(source)
    got = await _collect(frames, 1)
    assert len(got[0][0]) == _FRAME_BYTES
    await frames.aclose()


def test_a_stopped_source_is_forgotten() -> None:
    """The session ended; its frames must not keep piling up behind it."""
    mic = ClientMic()
    token = mic.connect(ClientHello(sample_rate=_RATE))
    source = mic.open(_RATE)
    source.stop()
    mic.feed(token, _block(320))
    assert source.pending_frames == 0


@pytest.mark.parametrize(
    ("rate", "channels"),
    [(0, 1), (4000, 1), (10**7, 1), (48000, 0), (48000, 64)],
)
def test_an_implausible_hello_is_refused(rate: int, channels: int) -> None:
    with pytest.raises(ValueError, match="plausible"):
        ClientHello(sample_rate=rate, channels=channels).validated()


def test_the_factory_picks_the_source_the_request_named() -> None:
    """The whole feature below the seam is one branch in one factory.

    A ``client`` start builds the browser's source; anything else goes to the
    device, so a stored default written before this existed is a device start
    and always will be.
    """
    mic = ClientMic()
    mic.connect(ClientHello(sample_rate=_RATE))
    seen: list[int] = []
    factory = capture_factory_for(mic, build_detector=lambda rate: (seen.append(rate), bool)[1])

    class _Req:
        source = CaptureSourceKind.CLIENT
        device = None

    source, detector = factory(_Req(), _RATE)  # type: ignore[arg-type]
    assert isinstance(source, ClientCaptureSource)
    assert source.sample_rate == _RATE
    assert source.client_rate == _RATE
    assert seen == [_RATE]
    assert detector(b"") is False


async def test_a_missing_resampler_refuses_the_start_rather_than_crashing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No native resampler and a browser at another rate is a refused start.

    A box with no sound hardware has no reason to have installed the ``audio``
    extra, and the failure has to reach the GM as a sentence rather than as a
    500 behind a session that has already been declared started.
    """

    def missing(_src: int, _dst: int) -> object:
        msg = "soxr is not installed (it ships with the `audio` extra)"
        raise RuntimeError(msg)

    monkeypatch.setattr(client_source, "Pcm16Stream", missing)
    source = _source(client_rate=48000, last_frame_mono=time.monotonic())
    with pytest.raises(CaptureUnavailableError, match="resampler is not installed"):
        await source.preflight()
    source.submit(_block(320))
    assert source.pending_frames == 0
