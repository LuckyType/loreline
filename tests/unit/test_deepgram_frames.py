"""Deepgram's turn protocol, pinned to frames the real service sent.

Every constant below is a verbatim copy of one line the live endpoint sent
during the verification run of ``nova-3`` on 2026-09-07: 60 seconds of LibriVox
speech, fed one 20 ms frame at a time at wall clock, ``interim_results=true``,
``endpointing=500``, ``utterance_end_ms=1000``, ``diarize=true``. No socket and
no network: the point is that the protocol behaviours that cost a turn once
cannot regress without a test going red.

Two of them are the reason this file exists, and neither is in the docs:

* one turn is *several* ``is_final`` segments, and only the last carries
  ``speech_final``, so a connector that published a segment would publish one
  speaker's sentence as three;
* an ``UtteranceEnd`` can arrive late, describing a turn that ``speech_final``
  already closed, *without* the ``last_word_end: -1`` the documentation defines
  for that case. The one this run produced read 8.8 while the open turn had
  begun at 9.6, and acting on it published a turn nothing ever finalized.
"""

# ruff: noqa: E501 - the frames below are verbatim wire lines; wrapping them
# would make them something other than what the service sent.

from __future__ import annotations

import json

from loreline.stt.backends.deepgram import (
    _TurnState,  # pyright: ignore[reportPrivateUsage]
)
from loreline.stt.streaming import TurnFinal, TurnPartial, TurnSignal, TurnStarted

_PARIS_INTERIM = '{"type":"Results","channel_index":[0,1],"duration":1.0,"start":0.0,"is_final":false,"speech_final":false,"channel":{"alternatives":[{"transcript":"Paris at a moment","confidence":0.97998047,"words":[{"word":"paris","start":0.0,"end":0.39999998,"confidence":0.97265625,"speaker":0,"punctuated_word":"Paris"},{"word":"at","start":0.39999998,"end":0.64,"confidence":0.89990234,"speaker":0,"punctuated_word":"at"},{"word":"a","start":0.64,"end":0.79999995,"confidence":0.9941406,"speaker":0,"punctuated_word":"a"},{"word":"moment","start":0.79999995,"end":0.96,"confidence":0.97998047,"speaker":0,"punctuated_word":"moment"}]}]},"metadata":{"request_id":"01a07aad-cb1e-78c3-80ef-181d08b56576","model_info":{"name":"general-nova-3","version":"2025-04-17.21547","arch":"nova-3"},"model_uuid":"40bd3654-e622-47c4-a111-63a61b23bfe8","diarize_info":{"model_uuid":"6ff6f59c-c349-443b-aba1-352de7d75943","arch":"v1"}},"from_finalize":false}'
_PARIS_SPEECH_FINAL = '{"type":"Results","channel_index":[0,1],"duration":2.3,"start":0.0,"is_final":true,"speech_final":true,"channel":{"alternatives":[{"transcript":"Paris at a moment\'s notice.","confidence":0.9902344,"words":[{"word":"paris","start":0.0,"end":0.39999998,"confidence":0.9223633,"speaker":0,"punctuated_word":"Paris"},{"word":"at","start":0.39999998,"end":0.71999997,"confidence":0.9902344,"speaker":0,"punctuated_word":"at"},{"word":"a","start":0.71999997,"end":0.79999995,"confidence":0.99902344,"speaker":0,"punctuated_word":"a"},{"word":"moment\'s","start":0.79999995,"end":1.1999999,"confidence":0.9536133,"speaker":0,"punctuated_word":"moment\'s"},{"word":"notice","start":1.1999999,"end":1.5999999,"confidence":0.9921875,"speaker":0,"punctuated_word":"notice."}]}]},"metadata":{"request_id":"01a07aad-cb1e-78c3-80ef-181d08b56576","model_info":{"name":"general-nova-3","version":"2025-04-17.21547","arch":"nova-3"},"model_uuid":"40bd3654-e622-47c4-a111-63a61b23bfe8","diarize_info":{"model_uuid":"6ff6f59c-c349-443b-aba1-352de7d75943","arch":"v1"}},"from_finalize":false}'
_THIRTY_INTERIM = '{"type":"Results","channel_index":[0,1],"duration":1.0,"start":2.3,"is_final":false,"speech_final":false,"channel":{"alternatives":[{"transcript":"I was thirty years of","confidence":1.0,"words":[{"word":"i","start":2.3,"end":2.46,"confidence":1.0,"speaker":0,"punctuated_word":"I"},{"word":"was","start":2.46,"end":2.6999998,"confidence":1.0,"speaker":0,"punctuated_word":"was"},{"word":"thirty","start":2.6999998,"end":2.94,"confidence":1.0,"speaker":0,"punctuated_word":"thirty"},{"word":"years","start":2.94,"end":3.1,"confidence":1.0,"speaker":0,"punctuated_word":"years"},{"word":"of","start":3.1,"end":3.26,"confidence":0.9604492,"speaker":0,"punctuated_word":"of"}]}]},"metadata":{"request_id":"01a07aad-cb1e-78c3-80ef-181d08b56576","model_info":{"name":"general-nova-3","version":"2025-04-17.21547","arch":"nova-3"},"model_uuid":"40bd3654-e622-47c4-a111-63a61b23bfe8","diarize_info":{"model_uuid":"6ff6f59c-c349-443b-aba1-352de7d75943","arch":"v1"}},"from_finalize":false}'
_THIRTY_INTERIM_SHRUNK = '{"type":"Results","channel_index":[0,1],"duration":2.0000002,"start":2.3,"is_final":false,"speech_final":false,"channel":{"alternatives":[{"transcript":"Was thirty years of age and had led up","confidence":1.0,"words":[{"word":"was","start":2.3799999,"end":2.6999998,"confidence":1.0,"speaker":0,"punctuated_word":"Was"},{"word":"thirty","start":2.6999998,"end":3.02,"confidence":1.0,"speaker":0,"punctuated_word":"thirty"},{"word":"years","start":3.02,"end":3.26,"confidence":1.0,"speaker":0,"punctuated_word":"years"},{"word":"of","start":3.26,"end":3.34,"confidence":1.0,"speaker":0,"punctuated_word":"of"},{"word":"age","start":3.34,"end":3.58,"confidence":1.0,"speaker":0,"punctuated_word":"age"},{"word":"and","start":3.58,"end":3.82,"confidence":0.9560547,"speaker":0,"punctuated_word":"and"},{"word":"had","start":3.82,"end":3.98,"confidence":0.9980469,"speaker":0,"punctuated_word":"had"},{"word":"led","start":3.98,"end":4.14,"confidence":0.83984375,"speaker":0,"punctuated_word":"led"},{"word":"up","start":4.14,"end":4.3,"confidence":0.5444336,"speaker":0,"punctuated_word":"up"}]}]},"metadata":{"request_id":"01a07aad-cb1e-78c3-80ef-181d08b56576","model_info":{"name":"general-nova-3","version":"2025-04-17.21547","arch":"nova-3"},"model_uuid":"40bd3654-e622-47c4-a111-63a61b23bfe8","diarize_info":{"model_uuid":"6ff6f59c-c349-443b-aba1-352de7d75943","arch":"v1"}},"from_finalize":false}'
_NAME_INTERIM = '{"type":"Results","channel_index":[0,1],"duration":1.0,"start":9.6,"is_final":false,"speech_final":false,"channel":{"alternatives":[{"transcript":"My name,","confidence":1.0,"words":[{"word":"my","start":9.76,"end":10.160001,"confidence":1.0,"speaker":0,"punctuated_word":"My"},{"word":"name","start":10.160001,"end":10.400001,"confidence":0.8376465,"speaker":0,"punctuated_word":"name,"}]}]},"metadata":{"request_id":"01a07aad-cb1e-78c3-80ef-181d08b56576","model_info":{"name":"general-nova-3","version":"2025-04-17.21547","arch":"nova-3"},"model_uuid":"40bd3654-e622-47c4-a111-63a61b23bfe8","diarize_info":{"model_uuid":"6ff6f59c-c349-443b-aba1-352de7d75943","arch":"v1"}},"from_finalize":false}'
_LATE_UTTERANCE_END = '{"type":"UtteranceEnd","channel":[0,1],"last_word_end":8.8}'
_NAME_INTERIM_GROWN = '{"type":"Results","channel_index":[0,1],"duration":2.3399992,"start":9.6,"is_final":false,"speech_final":false,"channel":{"alternatives":[{"transcript":"My name was Rupert Pennate.","confidence":0.99902344,"words":[{"word":"my","start":9.92,"end":10.240001,"confidence":1.0,"speaker":0,"punctuated_word":"My"},{"word":"name","start":10.240001,"end":10.72,"confidence":1.0,"speaker":0,"punctuated_word":"name"},{"word":"was","start":10.72,"end":11.04,"confidence":0.99902344,"speaker":0,"punctuated_word":"was"},{"word":"rupert","start":11.04,"end":11.440001,"confidence":0.99609375,"speaker":0,"punctuated_word":"Rupert"},{"word":"pennate","start":11.440001,"end":11.92,"confidence":0.79378253,"speaker":0,"punctuated_word":"Pennate."}]}]},"metadata":{"request_id":"01a07aad-cb1e-78c3-80ef-181d08b56576","model_info":{"name":"general-nova-3","version":"2025-04-17.21547","arch":"nova-3"},"model_uuid":"40bd3654-e622-47c4-a111-63a61b23bfe8","diarize_info":{"model_uuid":"6ff6f59c-c349-443b-aba1-352de7d75943","arch":"v1"}},"from_finalize":false}'
_NAME_SEGMENT_FINAL = '{"type":"Results","channel_index":[0,1],"duration":4.7599993,"start":9.6,"is_final":true,"speech_final":false,"channel":{"alternatives":[{"transcript":"My name was Rupert Pennace. I came of an old family","confidence":1.0,"words":[{"word":"my","start":9.92,"end":10.240001,"confidence":1.0,"speaker":0,"punctuated_word":"My"},{"word":"name","start":10.240001,"end":10.72,"confidence":1.0,"speaker":0,"punctuated_word":"name"},{"word":"was","start":10.72,"end":11.04,"confidence":0.99902344,"speaker":0,"punctuated_word":"was"},{"word":"rupert","start":11.04,"end":11.52,"confidence":0.99609375,"speaker":0,"punctuated_word":"Rupert"},{"word":"pennace","start":11.52,"end":12.4,"confidence":0.64127606,"speaker":0,"punctuated_word":"Pennace."},{"word":"i","start":12.56,"end":12.8,"confidence":1.0,"speaker":0,"punctuated_word":"I"},{"word":"came","start":12.8,"end":13.04,"confidence":1.0,"speaker":0,"punctuated_word":"came"},{"word":"of","start":13.04,"end":13.360001,"confidence":0.9980469,"speaker":0,"punctuated_word":"of"},{"word":"an","start":13.360001,"end":13.52,"confidence":1.0,"speaker":0,"punctuated_word":"an"},{"word":"old","start":13.52,"end":13.76,"confidence":1.0,"speaker":0,"punctuated_word":"old"},{"word":"family","start":13.76,"end":14.08,"confidence":1.0,"speaker":0,"punctuated_word":"family"}]}]},"metadata":{"request_id":"01a07aad-cb1e-78c3-80ef-181d08b56576","model_info":{"name":"general-nova-3","version":"2025-04-17.21547","arch":"nova-3"},"model_uuid":"40bd3654-e622-47c4-a111-63a61b23bfe8","diarize_info":{"model_uuid":"6ff6f59c-c349-443b-aba1-352de7d75943","arch":"v1"}},"from_finalize":false}'
_NEEDS_INTERIM = '{"type":"Results","channel_index":[0,1],"duration":1.0,"start":14.36,"is_final":false,"speech_final":false,"channel":{"alternatives":[{"transcript":"and had plenty of","confidence":1.0,"words":[{"word":"and","start":14.36,"end":14.599999,"confidence":0.94921875,"speaker":0,"punctuated_word":"and"},{"word":"had","start":14.599999,"end":14.839999,"confidence":1.0,"speaker":0,"punctuated_word":"had"},{"word":"plenty","start":14.839999,"end":15.16,"confidence":1.0,"speaker":0,"punctuated_word":"plenty"},{"word":"of","start":15.16,"end":15.24,"confidence":1.0,"speaker":0,"punctuated_word":"of"}]}]},"metadata":{"request_id":"01a07aad-cb1e-78c3-80ef-181d08b56576","model_info":{"name":"general-nova-3","version":"2025-04-17.21547","arch":"nova-3"},"model_uuid":"40bd3654-e622-47c4-a111-63a61b23bfe8","diarize_info":{"model_uuid":"6ff6f59c-c349-443b-aba1-352de7d75943","arch":"v1"}},"from_finalize":false}'
_NEEDS_SPEECH_FINAL = '{"type":"Results","channel_index":[0,1],"duration":2.6800013,"start":14.36,"is_final":true,"speech_final":true,"channel":{"alternatives":[{"transcript":"and had plenty of money for my needs.","confidence":1.0,"words":[{"word":"and","start":14.36,"end":14.599999,"confidence":0.9819336,"speaker":0,"punctuated_word":"and"},{"word":"had","start":14.599999,"end":14.839999,"confidence":1.0,"speaker":0,"punctuated_word":"had"},{"word":"plenty","start":14.839999,"end":15.24,"confidence":1.0,"speaker":0,"punctuated_word":"plenty"},{"word":"of","start":15.24,"end":15.4,"confidence":1.0,"speaker":0,"punctuated_word":"of"},{"word":"money","start":15.4,"end":15.639999,"confidence":1.0,"speaker":0,"punctuated_word":"money"},{"word":"for","start":15.639999,"end":15.879999,"confidence":0.99902344,"speaker":0,"punctuated_word":"for"},{"word":"my","start":15.879999,"end":16.039999,"confidence":1.0,"speaker":0,"punctuated_word":"my"},{"word":"needs","start":16.039999,"end":16.279999,"confidence":0.9904785,"speaker":0,"punctuated_word":"needs."}]}]},"metadata":{"request_id":"01a07aad-cb1e-78c3-80ef-181d08b56576","model_info":{"name":"general-nova-3","version":"2025-04-17.21547","arch":"nova-3"},"model_uuid":"40bd3654-e622-47c4-a111-63a61b23bfe8","diarize_info":{"model_uuid":"6ff6f59c-c349-443b-aba1-352de7d75943","arch":"v1"}},"from_finalize":false}'
_DYING_INTERIM = '{"type":"Results","channel_index":[0,1],"duration":1.0,"start":57.52,"is_final":false,"speech_final":false,"channel":{"alternatives":[{"transcript":"Dying,","confidence":0.8671875,"words":[{"word":"dying","start":57.920002,"end":58.4,"confidence":0.8671875,"speaker":0,"punctuated_word":"Dying,"}]}]},"metadata":{"request_id":"01a07aad-cb1e-78c3-80ef-181d08b56576","model_info":{"name":"general-nova-3","version":"2025-04-17.21547","arch":"nova-3"},"model_uuid":"40bd3654-e622-47c4-a111-63a61b23bfe8","diarize_info":{"model_uuid":"6ff6f59c-c349-443b-aba1-352de7d75943","arch":"v1"}},"from_finalize":false}'
_DYING_FROM_FINALIZE = '{"type":"Results","channel_index":[0,1],"duration":2.459999,"start":57.52,"is_final":true,"speech_final":true,"channel":{"alternatives":[{"transcript":"Dying, come at once, s","confidence":0.8691406,"words":[{"word":"dying","start":58.08,"end":58.72,"confidence":0.8466797,"speaker":0,"punctuated_word":"Dying,"},{"word":"come","start":58.72,"end":58.96,"confidence":0.99121094,"speaker":0,"punctuated_word":"come"},{"word":"at","start":58.96,"end":59.12,"confidence":0.99902344,"speaker":0,"punctuated_word":"at"},{"word":"once","start":59.12,"end":59.6,"confidence":0.8691406,"speaker":0,"punctuated_word":"once,"},{"word":"s","start":59.6,"end":59.84,"confidence":0.36938477,"speaker":0,"punctuated_word":"s"}]}]},"metadata":{"request_id":"01a07aad-cb1e-78c3-80ef-181d08b56576","model_info":{"name":"general-nova-3","version":"2025-04-17.21547","arch":"nova-3"},"model_uuid":"40bd3654-e622-47c4-a111-63a61b23bfe8","diarize_info":{"model_uuid":"6ff6f59c-c349-443b-aba1-352de7d75943","arch":"v1"}},"from_finalize":true}'


def _apply(frames: list[str]) -> list[TurnSignal]:
    state = _TurnState()
    return [signal for frame in frames for signal in state.apply(frame)]


def _near(value: float | None, expected: float) -> bool:
    """Float comparison for offsets the vendor states to the millisecond.

    A tenth of a millisecond, because Deepgram's offsets arrive with 32-bit
    precision and adding two of them drifts: 14.36 + 2.68 comes back as
    17.0400013. ``pytest.approx`` would say the same thing, but it types as
    partially unknown under this project's strict pyright, and one helper is
    cheaper than an ignore comment on every assertion.
    """
    return value is not None and abs(value - expected) < 1e-4


def _finals(signals: list[TurnSignal]) -> list[TurnFinal]:
    return [s for s in signals if isinstance(s, TurnFinal)]


def _partials(signals: list[TurnSignal]) -> list[TurnPartial]:
    return [s for s in signals if isinstance(s, TurnPartial)]


def _retimed(frame: str, last_word_end: float) -> str:
    """The recorded ``UtteranceEnd``, with only its timestamp changed.

    The run produced exactly one ``UtteranceEnd`` and it was a late one, so the
    two cases that matter and did not occur - the documented ``-1``, and one
    that genuinely closes the open turn - are that same frame with a different
    number in it, and nothing else.
    """
    return json.dumps(json.loads(frame) | {"last_word_end": last_word_end})


def test_a_simple_turn_is_one_interim_and_one_final() -> None:
    """The shape everything else is a complication of."""
    signals = _apply([_PARIS_INTERIM, _PARIS_SPEECH_FINAL])

    assert [type(s) for s in signals] == [TurnStarted, TurnPartial, TurnFinal]
    started, _, final = signals
    assert isinstance(started, TurnStarted)
    assert isinstance(final, TurnFinal)
    assert started.at == 0.0  # the segment's own offset, in seconds of stream
    assert final.text == "Paris at a moment's notice."
    assert _near(final.to, 2.3)  # start + duration of the settled segment
    assert [w.speaker for w in final.words] == ["Speaker 0"] * 5


def test_interims_replace_and_never_append() -> None:
    """Two consecutive real interims, the second of which is *shorter*.

    Deepgram restates the whole segment every time and revises what it already
    said: 'I was thirty years of' became 'Was thirty years of age and had led
    up'. Appending would have produced the two run together, and the leading
    'I' would have survived a revision that dropped it.
    """
    partials = _partials(_apply([_THIRTY_INTERIM, _THIRTY_INTERIM_SHRUNK]))

    assert all(p.append is False for p in partials)
    assert [p.text for p in partials] == [
        "I was thirty years of",
        "Was thirty years of age and had led up",
    ]


def test_several_settled_segments_are_one_turn() -> None:
    """One sentence, split by endpointing into two ``is_final`` segments.

    Only the second carries ``speech_final``. A connector that treated
    ``is_final`` as the end of a turn would report this as two.
    """
    signals = _apply(
        [
            _NAME_INTERIM,
            _NAME_INTERIM_GROWN,
            _NAME_SEGMENT_FINAL,
            _NEEDS_INTERIM,
            _NEEDS_SPEECH_FINAL,
        ]
    )
    finals = _finals(signals)

    assert len([s for s in signals if isinstance(s, TurnStarted)]) == 1
    assert len(finals) == 1
    assert finals[0].text == (
        "My name was Rupert Pennace. I came of an old family and had plenty of money for my needs."
    )
    # Every word of both segments, so inline diarization sees the whole turn.
    assert len(finals[0].words) == 19
    assert _near(finals[0].to, 17.04)


def test_a_late_utterance_end_does_not_split_a_turn() -> None:
    """The bug this file was written for, in the frames that produced it.

    The ``UtteranceEnd`` names 8.8, which is inside the turn ``speech_final``
    closed a moment earlier, while the open turn began at 9.6. Acting on it
    ended a turn that had no settled text yet, so its interims were published
    and then orphaned: a dimmed row on the session page that never settled.
    """
    signals = _apply(
        [
            _NAME_INTERIM,
            _LATE_UTTERANCE_END,
            _NAME_INTERIM_GROWN,
            _NAME_SEGMENT_FINAL,
            _NEEDS_INTERIM,
            _NEEDS_SPEECH_FINAL,
        ]
    )

    assert len([s for s in signals if isinstance(s, TurnStarted)]) == 1
    assert len(_finals(signals)) == 1
    assert _finals(signals)[0].text.startswith("My name was Rupert Pennace.")


def test_an_utterance_end_marked_already_final_is_ignored() -> None:
    """``last_word_end: -1`` is the documented "disregard this" marker."""
    signals = _apply([_NAME_INTERIM, _retimed(_LATE_UTTERANCE_END, -1)])

    assert _finals(signals) == []


def test_an_utterance_end_past_the_turn_start_closes_it() -> None:
    """The backstop doing its job: a word gap where endpointing heard none.

    The text is the unsettled segment's, because nothing had been finalized
    yet, which is Deepgram's own advice for this case ("process the last
    received transcript").
    """
    signals = _apply([_NAME_INTERIM, _retimed(_LATE_UTTERANCE_END, 10.4)])
    finals = _finals(signals)

    assert len(finals) == 1
    assert finals[0].text == "My name,"
    assert _near(finals[0].to, 10.4)


def test_from_finalize_closes_the_turn_the_microphone_stopped_on() -> None:
    """The answer to ``Finalize``: the last turn settles with its words.

    Without it the stream would wait out its final timeout and publish the
    turn's last interim instead, losing the word timings inline diarization
    reads.
    """
    finals = _finals(_apply([_DYING_INTERIM, _DYING_FROM_FINALIZE]))

    assert len(finals) == 1
    assert finals[0].text == "Dying, come at once, s"
    assert finals[0].words
    assert _near(finals[0].to, 59.98)


def test_an_error_frame_stops_the_connection() -> None:
    """Not recorded: the run produced none, so this is Deepgram's documented shape.

    What it pins is only that the state machine reports one, which is what lets
    the stream reconnect or fail over instead of streaming into silence.
    """
    state = _TurnState()
    state.apply(json.dumps({"type": "Error", "description": "DATA-0000", "variant": "DATA"}))

    assert state.error == "DATA-0000"


def test_an_empty_lead_in_settles_nothing_and_opens_no_turn() -> None:
    """A settled segment with no transcript is the near-silent lead-in.

    It carries an offset and no words, and it must not open a turn: one opened
    there would have to be closed by something, and nothing would.
    """
    lead_in = json.dumps(
        json.loads(_PARIS_INTERIM)
        | {"is_final": True, "channel": {"alternatives": [{"transcript": "", "words": []}]}}
    )

    assert _apply([lead_in]) == []
