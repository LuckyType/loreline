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
from loreline.stt.streaming import TurnFinal, TurnPartial, TurnSignal, TurnStarted


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


async def _signals(frames: list[dict[str, object]]) -> list[TurnSignal]:
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
    # the connector forwards them untouched.
    assert final.at == 1.0
    assert final.to == 2.4
    assert [w.text for w in final.words] == ["the", "goblin", "flees"]
    assert [w.start for w in final.words] == [1.0, 1.3, 1.9]
    assert {w.speaker for w in final.words} == {"Speaker 0", "Speaker 1"}


async def test_a_turn_with_no_words_falls_back_to_the_events_own_span() -> None:
    """Nothing here invents a timestamp; the start/duration pair is the vendor's."""
    signals = await _signals([_partial("mhm", [], speech_final=True, start=4.0, duration=0.6)])

    final = signals[-1]
    assert isinstance(final, TurnFinal)
    assert (final.at, final.to) == (4.0, 4.6)


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


async def test_the_greeting_and_anything_unknown_translate_to_nothing() -> None:
    """Consumed, not translated: the watchdog counts messages, not signals."""
    signals = await _signals([{"type": "transcript.created"}, {"type": "something.new"}])

    assert signals == []


async def test_an_empty_interim_is_not_a_turn() -> None:
    signals = await _signals([_partial("", [])])

    assert signals == []


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
    # ...and the second turn still starts where its own first word does.
    assert finals[1].at == 3.0


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
