"""Frame handling for the AssemblyAI streaming connector, pinned to real frames.

Every frame below is a verbatim copy of one the real Universal-Streaming v3
endpoint sent during a verification run of ``universal-3-5-pro`` on 2026-09-07:
11 s of LibriVox speech cut into two short bursts with silence between them, so
AssemblyAI's own endpointing produced two short turns, fed at wall clock with
``speaker_labels`` and ``continuous_partials`` on. No socket and no network: the
connector's ``signals()`` is driven straight off the recorded messages.

Three of the four things that decide whether the transcript comes out right are
only knowable from frames like these, not from the docs:

* a partial's ``transcript`` is the *whole* turn so far, and it revises words it
  already sent ("My name was Rupert." became "My name was Rupert Pennaise. I"),
* a turn's start moves when it settles (turn 1's partials say 4480 ms, its final
  says 5488 ms), which is why the turn id is ``turn_order`` and not a timestamp,
* the words a turn settles with may be attributed to nobody ("PENDING"), and
  ``SpeakerRevision`` at the end of the session is where they stop being.
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator

from loreline.models import ProviderConfig, ProviderKind
from loreline.stt.backends.assemblyai import AssemblyAIBackend
from loreline.stt.streaming import (
    StreamAlive,
    StreamSignal,
    TurnFinal,
    TurnPartial,
    TurnSignal,
    TurnStarted,
)

# --- the recorded session -------------------------------------------------

_BEGIN = '{"type": "Begin", "id": "a8177f19-9fc7-48c6-9fe7-9efeb90625a0", "expires_at": 1788775231, "configuration": {"model": "universal-3-5-pro", "mode": "balanced", "api_version": "2025-05-12", "speaker_labels": true, "redact_pii": false, "filter_profanity": false, "domain": null, "voice_focus": null}}'  # noqa: E501

_SPEECH_STARTED_0 = '{"type": "SpeechStarted", "timestamp": 0, "confidence": 0.865829}'

_TURN_0_PARTIAL_1 = '{"turn_order": 0, "turn_is_formatted": true, "end_of_turn": false, "transcript": "Paris at a moment", "end_of_turn_confidence": 0.0, "words": [{"start": 0, "end": 273, "text": "Paris", "confidence": 0.995975, "word_is_final": false}, {"start": 283, "end": 392, "text": "at", "confidence": 0.961336, "word_is_final": false}, {"start": 402, "end": 456, "text": "a", "confidence": 0.984741, "word_is_final": false}, {"start": 466, "end": 794, "text": "moment", "confidence": 0.521262, "word_is_final": false}], "utterance": "", "type": "Turn"}'  # noqa: E501

_TURN_0_PARTIAL_2 = '{"turn_order": 0, "turn_is_formatted": true, "end_of_turn": false, "transcript": "Paris at a moment\'s notice.", "end_of_turn_confidence": 0.0, "words": [{"start": 0, "end": 504, "text": "Paris", "confidence": 0.997098, "word_is_final": false}, {"start": 524, "end": 725, "text": "at", "confidence": 0.996955, "word_is_final": false}, {"start": 745, "end": 845, "text": "a", "confidence": 0.999947, "word_is_final": false}, {"start": 865, "end": 1671, "text": "moment\'s", "confidence": 0.999961, "word_is_final": false}, {"start": 1691, "end": 2396, "text": "notice.", "confidence": 0.994184, "word_is_final": false}], "utterance": "", "type": "Turn"}'  # noqa: E501

_TURN_0_FINAL = '{"turn_order": 0, "turn_is_formatted": true, "end_of_turn": true, "transcript": "Paris at a moment\'s notice. I was 30", "end_of_turn_confidence": 1.0, "words": [{"start": 32, "end": 377, "text": "Paris", "confidence": 0.873701, "speaker": "A", "word_is_final": true}, {"start": 525, "end": 623, "text": "at", "confidence": 0.998243, "speaker": "A", "word_is_final": true}, {"start": 673, "end": 689, "text": "a", "confidence": 0.999972, "speaker": "A", "word_is_final": true}, {"start": 689, "end": 1231, "text": "moment\'s", "confidence": 0.999987, "speaker": "A", "word_is_final": true}, {"start": 1247, "end": 1543, "text": "notice.", "confidence": 0.999552, "speaker": "A", "word_is_final": true}, {"start": 2479, "end": 2495, "text": "I", "confidence": 0.999644, "speaker": "PENDING", "word_is_final": true}, {"start": 2561, "end": 2610, "text": "was", "confidence": 0.999898, "speaker": "PENDING", "word_is_final": true}, {"start": 2741, "end": 2938, "text": "30", "confidence": 0.999095, "speaker": "PENDING", "word_is_final": true}], "utterance": "Paris at a moment\'s notice. I was 30", "speaker_label": "A", "speaker_confidence": 0.0, "type": "Turn"}'  # noqa: E501

_SPEECH_STARTED_1 = '{"type": "SpeechStarted", "timestamp": 4480, "confidence": 0.950008}'

_TURN_1_PARTIAL_1 = '{"turn_order": 1, "turn_is_formatted": true, "end_of_turn": false, "transcript": "My", "end_of_turn_confidence": 0.0, "words": [{"start": 4480, "end": 5600, "text": "My", "confidence": 0.950008, "word_is_final": false}], "utterance": "", "type": "Turn"}'  # noqa: E501

_TURN_1_PARTIAL_2 = '{"turn_order": 1, "turn_is_formatted": true, "end_of_turn": false, "transcript": "My name was Rupert.", "end_of_turn_confidence": 0.0, "words": [{"start": 4480, "end": 4757, "text": "My", "confidence": 0.998221, "word_is_final": false}, {"start": 4784, "end": 5339, "text": "name", "confidence": 0.999261, "word_is_final": false}, {"start": 5366, "end": 5782, "text": "was", "confidence": 0.998352, "word_is_final": false}, {"start": 5809, "end": 6780, "text": "Rupert.", "confidence": 0.880057, "word_is_final": false}], "utterance": "", "type": "Turn"}'  # noqa: E501

_TURN_1_PARTIAL_3 = '{"turn_order": 1, "turn_is_formatted": true, "end_of_turn": false, "transcript": "My name was Rupert Pennaise. I", "end_of_turn_confidence": 0.0, "words": [{"start": 4480, "end": 4765, "text": "My", "confidence": 0.997312, "word_is_final": false}, {"start": 4793, "end": 5364, "text": "name", "confidence": 0.999951, "word_is_final": false}, {"start": 5392, "end": 5820, "text": "was", "confidence": 0.999895, "word_is_final": false}, {"start": 5848, "end": 6704, "text": "Rupert", "confidence": 0.999982, "word_is_final": false}, {"start": 6732, "end": 8016, "text": "Pennaise.", "confidence": 0.82141, "word_is_final": false}, {"start": 8044, "end": 8186, "text": "I", "confidence": 0.77243, "word_is_final": false}], "utterance": "", "type": "Turn"}'  # noqa: E501

_TURN_1_FINAL = '{"turn_order": 1, "turn_is_formatted": true, "end_of_turn": true, "transcript": "My name was Rupert Pennaise. I came", "end_of_turn_confidence": 1.0, "words": [{"start": 5488, "end": 5570, "text": "My", "confidence": 0.99321, "speaker": "A", "speaker_confidence": 0.832652, "word_is_final": true}, {"start": 5651, "end": 5928, "text": "name", "confidence": 0.999924, "speaker": "A", "speaker_confidence": 0.832652, "word_is_final": true}, {"start": 6383, "end": 6497, "text": "was", "confidence": 0.999894, "speaker": "A", "speaker_confidence": 0.832652, "word_is_final": true}, {"start": 6627, "end": 6969, "text": "Rupert", "confidence": 0.999994, "speaker": "A", "speaker_confidence": 0.832652, "word_is_final": true}, {"start": 7034, "end": 7473, "text": "Pennaise.", "confidence": 0.851091, "speaker": "A", "speaker_confidence": 0.832652, "word_is_final": true}, {"start": 8173, "end": 8189, "text": "I", "confidence": 0.996188, "speaker": "PENDING", "speaker_confidence": 0.832652, "word_is_final": true}, {"start": 8320, "end": 8596, "text": "came", "confidence": 0.998675, "speaker": "PENDING", "speaker_confidence": 0.832652, "word_is_final": true}], "utterance": "My name was Rupert Pennaise. I came", "speaker_label": "A", "speaker_confidence": 0.594751, "type": "Turn"}'  # noqa: E501

_SPEAKER_REVISION = '{"type": "SpeakerRevision", "revisions": [{"turn_order": 0, "speaker_label": "A", "words": [{"start": 32, "end": 377, "text": "Paris", "confidence": 0.833357, "speaker": "A", "word_is_final": true}, {"start": 525, "end": 623, "text": "at", "confidence": 0.745353, "speaker": "A", "word_is_final": true}, {"start": 673, "end": 689, "text": "a", "confidence": 0.110319, "speaker": "A", "word_is_final": true}, {"start": 689, "end": 1231, "text": "moment\'s", "confidence": 0.627261, "speaker": "A", "word_is_final": true}, {"start": 1247, "end": 1543, "text": "notice.", "confidence": 0.844031, "speaker": "A", "word_is_final": true}, {"start": 2479, "end": 2495, "text": "I", "confidence": 0.999767, "speaker": "A", "word_is_final": true}, {"start": 2561, "end": 2610, "text": "was", "confidence": 0.941001, "speaker": "A", "word_is_final": true}, {"start": 2741, "end": 2938, "text": "30", "confidence": 0.639451, "speaker": "A", "word_is_final": true}]}, {"turn_order": 1, "speaker_label": "A", "words": [{"start": 5488, "end": 5570, "text": "My", "confidence": 0.960637, "speaker": "A", "word_is_final": true}, {"start": 5651, "end": 5928, "text": "name", "confidence": 0.983574, "speaker": "A", "word_is_final": true}, {"start": 6383, "end": 6497, "text": "was", "confidence": 0.973881, "speaker": "A", "word_is_final": true}, {"start": 6627, "end": 6969, "text": "Rupert", "confidence": 0.688032, "speaker": "A", "word_is_final": true}, {"start": 7034, "end": 7473, "text": "Pennaise.", "confidence": 0.736599, "speaker": "A", "word_is_final": true}, {"start": 8173, "end": 8189, "text": "I", "confidence": 0.93811, "speaker": "B", "word_is_final": true}, {"start": 8320, "end": 8596, "text": "came", "confidence": 0.703651, "speaker": "B", "word_is_final": true}]}]}'  # noqa: E501

_TERMINATION = (
    '{"type": "Termination", "audio_duration_seconds": 11, "session_duration_seconds": 11}'
)

_SESSION = [
    _BEGIN,
    _SPEECH_STARTED_0,
    _TURN_0_PARTIAL_1,
    _TURN_0_PARTIAL_2,
    _TURN_0_FINAL,
    _SPEECH_STARTED_1,
    _TURN_1_PARTIAL_1,
    _TURN_1_PARTIAL_2,
    _TURN_1_PARTIAL_3,
    _TURN_1_FINAL,
    _SPEAKER_REVISION,
    _TERMINATION,
]


# --- driving the connector with them --------------------------------------


class _Recorded:
    """The socket ``signals()`` reads, replaying a recorded session."""

    def __init__(self, frames: list[str]) -> None:
        self._frames = frames

    def __aiter__(self) -> AsyncIterator[str]:
        return self._iter()

    async def _iter(self) -> AsyncIterator[str]:
        for frame in self._frames:
            yield frame


def _backend(frames: list[str]) -> AssemblyAIBackend:
    config = ProviderConfig(
        id="aai", name="AssemblyAI", kind=ProviderKind.ASSEMBLYAI, sample_rate=16000
    )
    backend = AssemblyAIBackend(config, api_key="secret")
    backend._stream_ws = _Recorded(frames)  # pyright: ignore[reportPrivateUsage, reportAttributeAccessIssue]
    return backend


async def _signals(frames: list[str]) -> list[StreamSignal]:
    return [signal async for signal in _backend(frames).signals()]


def _turns(signals: list[StreamSignal]) -> list[TurnSignal]:
    """Only the signals about turns; the rest say the socket is there."""
    return [s for s in signals if not isinstance(s, StreamAlive)]


async def test_the_whole_recorded_session_becomes_the_signals_it_should() -> None:
    signals = await _signals(_SESSION)

    # Two turns: a start plus a partial per interim, one final each, and one
    # more final per revised turn at the end. Begin, SpeechStarted and
    # Termination say nothing about a turn, so they become StreamAlive: the
    # liveness watchdog counts signals it was told about, and a session
    # between turns sends nothing else.
    assert [type(s).__name__ for s in signals] == [
        "StreamAlive",  # Begin
        "StreamAlive",  # SpeechStarted
        "TurnStarted",
        "TurnPartial",
        "TurnStarted",
        "TurnPartial",
        "TurnFinal",
        "StreamAlive",  # SpeechStarted
        "TurnStarted",
        "TurnPartial",
        "TurnStarted",
        "TurnPartial",
        "TurnStarted",
        "TurnPartial",
        "TurnFinal",
        "TurnFinal",
        "TurnFinal",
        "StreamAlive",  # Termination
    ]
    assert {s.ref for s in _turns(signals)} == {"0", "1"}  # turn_order is the whole ref


async def test_termination_ends_the_iteration_rather_than_waiting_for_a_close() -> None:
    """It is the server's own "that was everything", so nothing is left to read.

    Reading on past it spent the stream's final wait on a socket that had
    already finished, which is time added to Stop for no answer.
    """
    signals = await _signals([*_SESSION, _TURN_0_PARTIAL_1])

    assert signals == await _signals(_SESSION)  # the frame behind Termination is never read


async def test_a_partial_carries_the_whole_turn_so_far() -> None:
    """``append=False``, and the recorded frames are why.

    A connector that appended would have produced "Paris at a momentParis at a
    moment's notice.", and a turn that revises itself ("Rupert." to "Rupert
    Pennaise. I") could not be expressed by appending at all.
    """
    partials = [s for s in await _signals(_SESSION) if isinstance(s, TurnPartial)]

    assert all(not p.append for p in partials)
    assert [p.text for p in partials[:2]] == ["Paris at a moment", "Paris at a moment's notice."]
    assert partials[3].text == "My name was Rupert."
    assert partials[4].text == "My name was Rupert Pennaise. I"


async def test_a_turn_opens_on_its_own_first_word_offset() -> None:
    """There is no turn-start signal to use: ``SpeechStarted`` names no turn."""
    starts = [s for s in await _signals(_SESSION) if isinstance(s, TurnStarted)]

    assert starts[0] == TurnStarted(at=0.0, ref="0")
    assert starts[2] == TurnStarted(at=4.48, ref="1")


async def test_a_final_states_the_span_its_partials_only_guessed_at() -> None:
    """Turn 1's partials start at 4480 ms; settled, the turn starts at 5488."""
    finals = [s for s in await _signals(_SESSION) if isinstance(s, TurnFinal)]
    settled = finals[1]

    assert settled.ref == "1"
    assert settled.text == "My name was Rupert Pennaise. I came"
    assert settled.at == 5.488  # seconds into this connection's audio, not ms
    assert settled.to == 8.596


async def test_final_words_carry_speakers_and_pending_carries_none() -> None:
    finals = [s for s in await _signals(_SESSION) if isinstance(s, TurnFinal)]
    words = finals[0].words

    assert [w.text for w in words[:3]] == ["Paris", "at", "a"]
    assert words[0].start == 0.032  # milliseconds from the vendor, seconds out
    assert [w.speaker for w in words[:5]] == ["Speaker A"] * 5
    # "PENDING" is not a speaker; the last three words of this turn have none
    # until the revision below attributes them.
    assert [w.speaker for w in words[5:]] == [None, None, None]


async def test_the_speaker_revision_republishes_a_settled_turn() -> None:
    """The end-of-session pass is where PENDING words get a speaker at all.

    It re-states words and not text, so the republished final carries the text
    the turn settled with, under the same ref: the stream keys every revision
    of one turn by that, so this replaces the stored row rather than adding one.
    """
    finals = [s for s in await _signals(_SESSION) if isinstance(s, TurnFinal)]
    settled, revised = finals[0], finals[2]

    assert revised.ref == settled.ref == "0"
    assert revised.text == settled.text  # the revision names no transcript
    assert [w.speaker for w in revised.words] == ["Speaker A"] * 8
    assert [w.text for w in revised.words] == [w.text for w in settled.words]


async def test_a_revision_for_a_turn_that_never_settled_is_dropped() -> None:
    """Its final is still to come, and carries the revised labels already.

    Verified against the real service on a longer clip: the last turn's
    revision arrived before its own final, and the two agreed word for word.
    """
    signals = await _signals([_BEGIN, _SPEAKER_REVISION, _TERMINATION])

    assert _turns(signals) == []


async def test_an_error_frame_ends_the_session_instead_of_being_skipped() -> None:
    """A v3 session that rejected something answers nothing else afterwards."""
    error = json.dumps(
        {
            "type": "Error",
            "error_code": 3007,
            "error": "Input Duration Error: Input Duration Violation: 20.0 ms. "
            "Expected between 50 and 1000 ms",
        }
    )
    signals = await _signals([_BEGIN, _TURN_0_PARTIAL_1, error, _TURN_0_FINAL])

    assert [type(s).__name__ for s in signals] == ["StreamAlive", "TurnStarted", "TurnPartial"]


async def test_a_frame_that_is_not_json_is_skipped_rather_than_raised() -> None:
    """One malformed frame used to end a live session out of the read loop."""
    signals = await _signals([_BEGIN, "<html>502 Bad Gateway</html>", _TURN_0_PARTIAL_1])

    assert [type(s).__name__ for s in _turns(signals)] == ["TurnStarted", "TurnPartial"]


async def test_signals_without_an_open_stream_yield_nothing() -> None:
    config = ProviderConfig(id="aai", name="AssemblyAI", kind=ProviderKind.ASSEMBLYAI)
    backend = AssemblyAIBackend(config, api_key="secret")

    assert [s async for s in backend.signals()] == []


async def test_audio_is_held_only_until_it_clears_the_fifty_millisecond_floor() -> None:
    """20 ms messages close the socket with code 3007; three frames do not."""
    sent: list[bytes] = []

    class _Ws:
        async def send(self, data: bytes) -> None:
            sent.append(data)

    backend = _backend([])
    backend._stream_ws = _Ws()  # pyright: ignore[reportPrivateUsage, reportAttributeAccessIssue]
    frame = b"\x01\x00" * 320  # 20 ms at 16 kHz

    for _ in range(3):
        await backend.send_audio(frame)

    assert len(sent) == 1
    assert len(sent[0]) == 3 * len(frame)  # 60 ms: the first message over the floor
    assert 50 <= len(sent[0]) / 2 / 16000 * 1000 <= 1000


async def test_flush_pads_the_tail_and_terminates() -> None:
    """The last 20 ms would be refused on its own, and dropping it loses a word."""
    sent: list[bytes | str] = []
    backend = _backend([])

    class _Ws:
        """A socket that answers Terminate, which is what the real one does.

        Without the answer this test sat out the whole flush timeout for a
        session that had already said everything: a real second and a half of
        the suite spent waiting for a frame nobody was going to send.
        """

        async def send(self, data: bytes | str) -> None:
            sent.append(data)
            if isinstance(data, str) and json.loads(data).get("type") == "Terminate":
                backend._terminated.set()  # pyright: ignore[reportPrivateUsage]

    backend._stream_ws = _Ws()  # pyright: ignore[reportPrivateUsage, reportAttributeAccessIssue]
    tail = b"\x01\x00" * 320

    started = time.monotonic()
    await backend.send_audio(tail)
    await backend.flush_input()

    assert time.monotonic() - started < 1.0  # the answer ends the wait, not the timeout
    assert len(sent) == 2
    audio = sent[0]
    assert isinstance(audio, bytes)
    assert audio.startswith(tail)  # the tail kept, padded with silence
    assert len(audio) == 16000 * 2 * 50 // 1000  # exactly the 50 ms minimum
    assert json.loads(str(sent[1])) == {"type": "Terminate"}
