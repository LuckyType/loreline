"""Frame handling for the Gemini Live connector, pinned to recorded frames.

Every frame below is a verbatim copy of one the real service sent during the
verification run of ``gemini-3.5-transcribe-live`` (45 s of LibriVox speech,
78 frames realtime-paced, 52 frames blasted). No socket and no network: the
point is that the two service behaviours that cost a whole transcript once
cannot regress without a test going red.
"""

from __future__ import annotations

import json

from loreline.stt.backends.gemini_live import (
    _TurnState,  # pyright: ignore[reportPrivateUsage]
)

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
