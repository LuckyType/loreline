"""Signal translation for the xAI streaming shape, from documented frames.

Every frame below is built from xAI's published event schema, NOT recorded from
api.x.ai: this environment has no xAI key and never has had one, so no run
against the real service exists to copy from. What these tests pin is therefore
the connector's reading of the documentation, which is the only thing that can
be pinned without a key, and they are the place to look first if a maintainer
with one finds the transcript coming out wrong.

The reading they exist for above all others: ``text`` is documented as
cumulative, and the documentation says cumulative over two different things
(the utterance, in the guide; the stream, in the API reference). The connector
is written to produce the same transcript either way, and the two tests at the
bottom are what say so.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import cast

from websockets.asyncio.client import ClientConnection

from loreline.models import ProviderConfig, ProviderKind
from loreline.stt.backends.xai import XaiBackend
from loreline.stt.streaming import (
    StreamAlive,
    StreamSignal,
    TurnFinal,
    TurnPartial,
    TurnStarted,
)


class _Frames:
    """A socket that only ever yields the events the docs describe."""

    def __init__(self, frames: list[dict[str, object]]) -> None:
        self._frames = [json.dumps(frame) for frame in frames]

    def __aiter__(self) -> AsyncIterator[str]:
        return self._replay()

    async def _replay(self) -> AsyncIterator[str]:
        for frame in self._frames:
            yield frame


def _word(text: str, start: float, end: float, speaker: int) -> dict[str, object]:
    """One entry of the documented ``words[]`` array, offsets from stream start."""
    return {"text": text, "start": start, "end": end, "confidence": 0.98, "speaker": speaker}


def _partial(
    text: str,
    words: list[dict[str, object]],
    *,
    is_final: bool = False,
    speech_final: bool = False,
    start: float = 0.0,
    duration: float = 0.5,
) -> dict[str, object]:
    """A ``transcript.partial`` with every documented field on it."""
    return {
        "type": "transcript.partial",
        "text": text,
        "words": words,
        "is_final": is_final or speech_final,
        "speech_final": speech_final,
        "start": start,
        "duration": duration,
    }


async def _signals(frames: list[dict[str, object]]) -> list[StreamSignal]:
    """What one connection's events become, with no socket and no network."""
    backend = XaiBackend(
        ProviderConfig(id="xai-1", name="xAI", kind=ProviderKind.XAI, language="en"),
        api_key="secret",
    )
    backend._stream_ws = cast(  # pyright: ignore[reportPrivateUsage]
        "ClientConnection", _Frames(frames)
    )
    return [signal async for signal in backend.signals()]


_HELLO = [_word("the", 1.0, 1.3, 0), _word("goblin", 1.3, 1.9, 0)]
_MORE = [*_HELLO, _word("flees", 1.9, 2.4, 1)]


async def test_an_interim_opens_a_turn_and_replaces_rather_than_appends() -> None:
    """The vendor resends the whole text every time, so nothing is concatenated."""
    signals = await _signals([_partial("the goblin", _HELLO)])

    assert signals == [
        TurnStarted(at=1.0),
        TurnPartial(text="the goblin", append=False),
    ]


async def test_a_turn_starts_where_its_first_word_does() -> None:
    """There is no speech-started event, so the words are the only honest answer.

    The alternative is the frame the partial happened to arrive on, which is
    the vendor's latency late, and the session timeline is drawn from it.
    """
    signals = await _signals([_partial("the goblin", _HELLO, start=1.0, duration=0.9)])

    started = signals[0]
    assert isinstance(started, TurnStarted)
    assert started.at == 1.0  # not 0.0, and not "now"


async def test_a_locked_chunk_is_still_an_interim_until_the_speaker_stops() -> None:
    """is_final locks three seconds of text; speech_final is the turn boundary."""
    signals = await _signals([_partial("the goblin", _HELLO, is_final=True)])

    assert [type(s) for s in signals] == [TurnStarted, TurnPartial]


async def test_speech_final_closes_the_turn_with_its_words_and_span() -> None:
    signals = await _signals(
        [
            _partial("the goblin", _HELLO),
            _partial("the goblin flees", _MORE, speech_final=True, start=1.0, duration=1.4),
        ]
    )

    final = signals[-1]
    assert isinstance(final, TurnFinal)
    assert final.text == "the goblin flees"
    # Offsets are documented as measured from the beginning of the audio
    # stream, which is what TranscriptStream maps onto the capture clock, so
    # the connector forwards them untouched...
    assert final.to == 2.4
    # ...but a final states no `at` at all, exactly as the Deepgram connector
    # deliberately does not. The turn already has a start, from the
    # TurnStarted its first partial carried, and restating a refined one here
    # would move a published row's timestamps for nothing.
    assert final.at is None
    assert [w.text for w in final.words] == ["the", "goblin", "flees"]
    assert [w.start for w in final.words] == [1.0, 1.3, 1.9]
    assert {w.speaker for w in final.words} == {"Speaker 0", "Speaker 1"}


async def test_a_turn_is_announced_once_and_its_start_never_moves() -> None:
    """The vendor revises where a turn began; the row it is written under may not.

    Three partials of one turn, each naming a later first word than the last,
    which is what a settling endpointer does (AssemblyAI was measured moving a
    turn's start by a second, and Deepgram's docs warn it may). Only the first
    of them announces the turn, so what the stream opens it at is where the
    vendor first said it began.
    """
    signals = await _signals(
        [
            _partial("the", [_word("the", 1.0, 1.3, 0)]),
            _partial("the goblin", [_word("the", 1.2, 1.5, 0), _word("goblin", 1.5, 1.9, 0)]),
            _partial(
                "the goblin flees",
                [
                    _word("the", 1.4, 1.7, 0),
                    _word("goblin", 1.7, 2.1, 0),
                    _word("flees", 2.1, 2.4, 1),
                ],
                speech_final=True,
            ),
        ]
    )

    starts = [s for s in signals if isinstance(s, TurnStarted)]
    assert [s.at for s in starts] == [1.0]  # once, at the first offset stated
    assert isinstance(signals[-1], TurnFinal)


async def test_the_next_turn_is_announced_again() -> None:
    """Announced once *per turn*, not once per connection."""
    second = [_word("it", 3.0, 3.2, 0)]
    signals = await _signals(
        [
            _partial("the goblin flees", _MORE, speech_final=True),
            _partial("it", second),
        ]
    )

    assert [s.at for s in signals if isinstance(s, TurnStarted)] == [1.0, 3.0]


async def test_a_turn_with_no_words_falls_back_to_the_events_own_span() -> None:
    """Nothing here invents a timestamp; the start/duration pair is the vendor's."""
    signals = await _signals([_partial("mhm", [], speech_final=True, start=4.0, duration=0.6)])

    started, final = signals[0], signals[-1]
    assert isinstance(started, TurnStarted)
    assert isinstance(final, TurnFinal)
    assert started.at == 4.0
    assert (final.at, final.to) == (None, 4.6)


async def test_transcript_done_ends_the_stream_without_publishing_the_session() -> None:
    """Its text is the whole connection's, so publishing it would repeat everything."""
    signals = await _signals(
        [
            _partial("the goblin flees", _MORE, speech_final=True),
            {
                "type": "transcript.done",
                "text": "the goblin flees",
                "words": _MORE,
                "duration": 2.4,
                "language": "en",
            },
            _partial("never read", _HELLO),
        ]
    )

    assert [type(s) for s in signals] == [TurnStarted, TurnFinal]


async def test_an_error_frame_ends_the_connection() -> None:
    """A socket that rejected something will not start working on the next frame."""
    signals = await _signals(
        [
            {"type": "error", "code": "invalid_request", "message": "keyterm too long"},
            _partial("never read", _HELLO),
        ]
    )

    assert signals == []


async def test_the_greeting_and_anything_unknown_say_the_socket_is_alive() -> None:
    """Not turns, and not nothing either.

    The stream's liveness watchdog counts signals it was told about, so a
    greeting swallowed here is a connection that has answered and looks quiet.
    """
    signals = await _signals([{"type": "transcript.created"}, {"type": "something.new"}])

    assert signals == [StreamAlive(), StreamAlive()]


async def test_an_empty_interim_is_not_a_turn() -> None:
    """...but it is still a frame, so it is still proof of a live socket."""
    signals = await _signals([_partial("", [])])

    assert signals == [StreamAlive()]


# -- the two readings of a cumulative `text` ------------------------------


async def test_a_second_turn_is_itself_when_text_restarts_per_utterance() -> None:
    """The guide's reading: speech_final carries the complete stitched utterance.

    Nothing is stripped here, because nothing in the second turn was published
    with the first.
    """
    second = [_word("it", 3.0, 3.2, 0), _word("escapes", 3.2, 3.8, 0)]
    signals = await _signals(
        [
            _partial("the goblin flees", _MORE, speech_final=True),
            _partial("it escapes", second, speech_final=True),
        ]
    )

    finals = [s for s in signals if isinstance(s, TurnFinal)]
    assert [f.text for f in finals] == ["the goblin flees", "it escapes"]
    assert [len(f.words) for f in finals] == [3, 2]


async def test_a_second_turn_is_itself_when_text_accumulates_over_the_stream() -> None:
    """The API reference's reading: every event repeats the whole stream so far.

    Left alone, this is the failure that would put the entire session in every
    row. What was already settled is stripped from the text and dropped from
    the words, which is the same transcript the test above produces.
    """
    second = [_word("it", 3.0, 3.2, 0), _word("escapes", 3.2, 3.8, 0)]
    signals = await _signals(
        [
            _partial("the goblin flees", _MORE, speech_final=True),
            _partial("the goblin flees it escapes", [*_MORE, *second], speech_final=True),
        ]
    )

    finals = [s for s in signals if isinstance(s, TurnFinal)]
    assert [f.text for f in finals] == ["the goblin flees", "it escapes"]
    assert [[w.text for w in f.words] for f in finals] == [
        ["the", "goblin", "flees"],
        ["it", "escapes"],
    ]
    # ...and the second turn still starts where its own first word does, which
    # is what its own TurnStarted says: the final states no start at all.
    starts = [s for s in signals if isinstance(s, TurnStarted)]
    assert [s.at for s in starts] == [1.0, 3.0]


async def test_a_turn_repeated_word_for_word_survives_as_itself() -> None:
    """The stripping only ever removes a strictly shorter prefix, so "Ja." twice
    is two rows rather than one row and a blank."""
    again = [_word("ja", 3.0, 3.3, 0)]
    signals = await _signals(
        [
            _partial("ja", [_word("ja", 1.0, 1.3, 0)], speech_final=True),
            _partial("ja", again, speech_final=True),
        ]
    )

    finals = [s for s in signals if isinstance(s, TurnFinal)]
    assert [f.text for f in finals] == ["ja", "ja"]
