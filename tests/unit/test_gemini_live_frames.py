"""Frame handling for the Gemini Live connector, pinned to recorded frames.

Every frame below is a verbatim copy of one the real service sent during the
verification run of ``gemini-3.5-transcribe-live`` (45 s of LibriVox speech,
78 frames realtime-paced, 52 frames blasted). No socket and no network: the
point is that the two service behaviours that cost a whole transcript once
cannot regress without a test going red.

Both shapes read the same frames, so both are pinned here against the same
recording: ``_TurnState`` folds them into one utterance's text, ``_StreamTurns``
turns them into the turn signals a persistent stream publishes (ADR 0006).
"""

from __future__ import annotations

import json

import pytest

from loreline.stt.backends.gemini_live import (
    _duration_s,  # pyright: ignore[reportPrivateUsage]
    _StreamTurns,  # pyright: ignore[reportPrivateUsage]
    _TurnState,  # pyright: ignore[reportPrivateUsage]
)
from loreline.stt.streaming import TurnFinal, TurnPartial, TurnSignal

# The four finals the paced run produced, one per turn. None of them carries
# leading or trailing spacing, which is the whole reason they cannot be
# concatenated.
_FINALS = [
    "Marseille: The Arrival",
    "signaled the Three Master, the Faraon from Smyrna, Trieste, and Naples.",
    (
        "As usual, a pilot put off immediately, and routing the Chateau d'If, "
        "got on board the vessel between Cape Morgion and Rion Island."
    ),
    (
        "Immediately and according to custom, the ramparts of Fort Saint-Jean "
        "were covered with spectators. It is always an event at Marseille for "
        "a ship to come into port, especially when this ship, like the "
        "Pharaon, has"
    ),
]


def _frame(payload: dict[str, object]) -> str:
    return json.dumps({"serverContent": payload})


def _turn(text: str, *, final: bool = True, trailing_empties: int = 2) -> list[str]:
    """One turn as the service sends it: interims, final, end, padding."""
    frames = [
        _frame({"interimInputTranscription": {"text": text[:9]}}),
        _frame({"interimInputTranscription": {"text": text}}),
    ]
    if final:
        frames.append(_frame({"inputTranscription": {"text": text}}))
        frames.append(_frame({"generationComplete": True}))
        frames.extend(_frame({}) for _ in range(trailing_empties))
    return frames


def _apply(state: _TurnState, frames: list[str]) -> None:
    for frame in frames:
        state.apply(frame)


def test_finals_join_with_a_space() -> None:
    """Word boundaries survive: concatenation produced "The Arrivalsignaled"."""
    state = _TurnState()
    _apply(state, [f for text in _FINALS for f in _turn(text)])

    assert state.transcript() == " ".join(_FINALS)
    assert "Arrivalsignaled" not in state.transcript()
    assert "Island.Immediately" not in state.transcript()


def test_generation_complete_ends_a_turn_and_turn_complete_never_arrives() -> None:
    """The recorded sessions end turns with generationComplete only."""
    state = _TurnState()
    _apply(state, _turn(_FINALS[0]))
    assert state.turn_ended is True

    # ... and the next turn re-opens the session, so a connector that stopped
    # at the first generationComplete would have kept 22 of 435 characters.
    _apply(state, _turn(_FINALS[1]))
    assert state.transcript() == f"{_FINALS[0]} {_FINALS[1]}"


def test_turn_complete_is_still_honoured_if_it_ever_appears() -> None:
    state = _TurnState()
    state.apply(_frame({"inputTranscription": {"text": "hello"}}))
    state.apply(_frame({"turnComplete": True}))

    assert state.turn_ended is True
    assert state.transcript() == "hello"


def test_empty_frames_do_not_end_a_turn() -> None:
    """{"serverContent": {}} is padding: one after setupComplete, one after
    every generationComplete, and a second one before the turn that follows."""
    state = _TurnState()
    state.apply(_frame({}))
    assert state.turn_ended is False

    state.apply(_frame({"interimInputTranscription": {"text": "Marseille"}}))
    state.apply(_frame({}))
    assert state.turn_ended is False


def test_interim_of_an_unfinalized_turn_is_kept() -> None:
    """The blast case: audio pushed faster than realtime leaves the last turn
    without a final, and its text only exists in the interims."""
    state = _TurnState()
    _apply(state, _turn(_FINALS[0]))
    _apply(state, _turn(_FINALS[3], final=False))

    assert state.transcript() == f"{_FINALS[0]} {_FINALS[3]}"


def test_interim_is_not_added_twice_when_its_turn_finalizes() -> None:
    state = _TurnState()
    _apply(state, _turn(_FINALS[0]))

    assert state.transcript() == _FINALS[0]


def test_snake_case_spellings_are_accepted() -> None:
    state = _TurnState()
    state.apply(json.dumps({"server_content": {"input_transcription": {"text": "hallo"}}}))
    state.apply(json.dumps({"server_content": {"generation_complete": True}}))

    assert state.turn_ended is True
    assert state.transcript() == "hallo"


def test_a_session_with_no_transcription_frames_yields_nothing() -> None:
    """What synthetic speech produced: setup acked, then empty frames only."""
    state = _TurnState()
    _apply(state, [_frame({}), _frame({})])

    assert state.transcript() == ""


# ---------------------------------------------------------------------------
# The streaming shape. Same frames, read as signals for a stream that outlives
# every turn in it rather than as one utterance's text.
# ---------------------------------------------------------------------------


def _signals(frames: list[str]) -> tuple[_StreamTurns, list[TurnSignal]]:
    turns = _StreamTurns()
    out: list[TurnSignal] = []
    for frame in frames:
        out.extend(turns.apply(frame))
    return turns, out


def test_streaming_interims_replace_rather_than_append() -> None:
    """Interims are cumulative within a turn, so append would repeat every word.

    ``TurnPartial.append`` is stated per signal precisely because getting it
    wrong shows up as duplicated interim text and nothing else.
    """
    _, signals = _signals(_turn(_FINALS[1], final=False))

    partials = [s for s in signals if isinstance(s, TurnPartial)]
    assert [s.text for s in partials] == [_FINALS[1][:9], _FINALS[1]]
    assert all(s.append is False for s in partials)


def test_streaming_publishes_one_final_per_turn_and_not_two() -> None:
    """inputTranscription settles the turn; the generationComplete behind it
    must publish nothing, or every turn would be written twice."""
    _, signals = _signals(_turn(_FINALS[0]))

    finals = [s for s in signals if isinstance(s, TurnFinal)]
    assert [s.text for s in finals] == [_FINALS[0]]


def test_streaming_settles_a_turn_the_service_never_finalized() -> None:
    """The blast case, and what a lost connection looks like mid-turn: the
    newest interim is that turn's text, so the turn end publishes it."""
    _, signals = _signals([*_turn(_FINALS[3], final=False), _frame({"generationComplete": True})])

    assert [s.text for s in signals if isinstance(s, TurnFinal)] == [_FINALS[3]]


def test_streaming_padding_frames_are_not_signals() -> None:
    """One follows every generationComplete and another heralds the next turn,
    so their count differs per turn and they cannot mean anything."""
    turns, signals = _signals([_frame({}), _frame({}), _frame({})])

    assert signals == []
    assert turns.go_away == ""


def test_streaming_states_no_offsets_and_numbers_its_own_turns() -> None:
    """The service reports no timing at all, which the stream reads as "now".

    Stating a zero instead would pin every turn to t0. The ``ref`` is this
    connector's own, since the wire names no turns and one of them can be
    settled twice; see the test below.
    """
    _, signals = _signals(_turn(_FINALS[0]))

    assert all(s.ref == "1" for s in signals)
    assert all(s.at is None for s in signals if isinstance(s, TurnFinal))
    assert all(s.to is None for s in signals if isinstance(s, TurnFinal))
    assert all(s.words == () for s in signals if isinstance(s, TurnFinal))


def test_streaming_numbers_each_turn_after_the_one_before() -> None:
    """One ref per turn, so a turn's interims and its final share a row."""
    _, signals = _signals([f for text in _FINALS for f in _turn(text)])

    assert [s.ref for s in signals if isinstance(s, TurnFinal)] == ["1", "2", "3", "4"]


def test_a_late_final_replaces_the_turn_that_was_settled_without_it() -> None:
    """One turn, settled twice, and it must not reach the transcript as two.

    generationComplete for a turn the service never finalized publishes the
    newest interim, because that is the only text that turn has. The service
    can still send the real ``inputTranscription`` for it afterwards - probed
    against the live model, and it is the better text, punctuated and cased.
    Without a ref the stream dated the second one from the last frame written,
    which is a different key from the first, so one turn became two final rows
    and both survived into the exports.
    """
    turns, signals = _signals(
        [
            *_turn(_FINALS[0], final=False),
            _frame({"generationComplete": True}),
            _frame({"inputTranscription": {"text": _FINALS[0]}}),
        ]
    )
    finals = [s for s in signals if isinstance(s, TurnFinal)]

    assert [s.ref for s in finals] == ["1", "1"]  # the second replaces the first
    assert turns.turn == 1  # ...rather than opening a turn of its own


def test_streaming_replays_a_whole_recorded_session_in_order() -> None:
    turns, signals = _signals([f for text in _FINALS for f in _turn(text)])

    assert [s.text for s in signals if isinstance(s, TurnFinal)] == _FINALS
    assert turns.interim == ""
    assert turns.owes_final is False


def test_a_final_after_a_turn_the_service_ended_properly_is_the_next_turn() -> None:
    """The other reading of a final behind a generationComplete.

    Where the turn was settled by its own ``inputTranscription`` there is
    nothing owed and nothing provisional, so a second final with no interim in
    front of it is the next turn arriving rather than a correction of the last.
    """
    _, signals = _signals([*_turn(_FINALS[0]), _frame({"inputTranscription": {"text": "again"}})])

    assert [s.ref for s in signals if isinstance(s, TurnFinal)] == ["1", "2"]


def test_streaming_accepts_the_snake_case_spellings_too() -> None:
    turns, signals = _signals(
        [
            json.dumps({"server_content": {"interim_input_transcription": {"text": "hal"}}}),
            json.dumps({"server_content": {"input_transcription": {"text": "hallo"}}}),
            json.dumps({"server_content": {"generation_complete": True}}),
        ]
    )

    assert [s.text for s in signals if isinstance(s, TurnPartial)] == ["hal"]
    assert [s.text for s in signals if isinstance(s, TurnFinal)] == ["hallo"]
    assert turns.owes_final is False


def test_go_away_is_recorded_so_the_reader_can_stop_on_it() -> None:
    """A session cap of minutes against a table that runs for hours: goAway is
    the announcement, and it is ordinary operation rather than a fault."""
    turns, signals = _signals([json.dumps({"goAway": {"timeLeft": "9.5s"}})])

    assert turns.go_away == "9.5s"
    assert signals == []


def test_go_away_with_no_time_left_still_stops_the_reader() -> None:
    turns, _ = _signals([json.dumps({"goAway": {}})])

    assert turns.go_away == "soon"


def test_the_newest_resumption_handle_is_kept() -> None:
    turns, signals = _signals(
        [
            json.dumps({"sessionResumptionUpdate": {"newHandle": "one", "resumable": True}}),
            json.dumps({"session_resumption_update": {"new_handle": "two", "resumable": True}}),
        ]
    )

    assert turns.handle == "two"
    assert signals == []


def test_a_session_the_server_calls_unresumable_drops_its_handle() -> None:
    """Presenting a handle the server has disowned costs a whole connection,
    which is exactly the thing resumption is here to avoid."""
    turns = _StreamTurns(handle="stale")
    _ = turns.apply(json.dumps({"sessionResumptionUpdate": {"resumable": False}}))

    assert turns.handle == ""


def test_a_turn_is_open_between_its_first_interim_and_its_final() -> None:
    """What the reader waits for before it acts on a goAway: leaving with a
    turn open costs that turn's settled text, and there is no need to."""
    turns = _StreamTurns()
    assert turns.owes_final is False

    _ = turns.apply(_frame({"interimInputTranscription": {"text": "Marseille"}}))
    assert turns.owes_final is True

    _ = turns.apply(_frame({"inputTranscription": {"text": _FINALS[0]}}))
    assert turns.owes_final is False


@pytest.mark.parametrize(
    ("value", "seconds"),
    [("50s", 50.0), ("49.500s", 49.5), ("0s", 0.0), ("soon", 0.0), ("", 0.0)],
)
def test_go_away_time_left_is_read_as_seconds(value: str, seconds: float) -> None:
    """goAway.timeLeft is a proto Duration, which JSON spells as "50s".

    An unreadable one is zero rather than a guess: the connection is ending
    either way, and leaving at once costs a reconnect where guessing high would
    cost the server hanging up in the middle of one.
    """
    assert _duration_s(value) == seconds
