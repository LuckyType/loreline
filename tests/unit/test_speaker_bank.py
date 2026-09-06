"""Unit tests for the diarization service's per-session speaker memory.

The bank is what makes a speaker label mean the same person from one
``/diarize`` call to the next. It is plain arithmetic over embeddings, so these
tests drive it with synthetic unit vectors at known angles instead of audio:
``_at(60)`` is a voice at cosine 0.5 from ``_at(0)``, which is exactly the
service's default match threshold.
"""

from __future__ import annotations

import math

from services.diarization.app import SessionBanks, SpeakerBank

_THRESHOLD = 0.5


def _at(degrees: float) -> list[float]:
    """A unit vector whose cosine with ``_at(0)`` is ``cos(degrees)``."""
    radians = math.radians(degrees)
    return [math.cos(radians), math.sin(radians), 0.0]


_A = _at(0)  # one voice
_B = [0.0, 0.0, 1.0]  # another, orthogonal to every _at() vector


def _bank() -> SpeakerBank:
    return SpeakerBank(threshold=_THRESHOLD)


def test_the_same_voice_keeps_its_speaker_across_calls() -> None:
    bank = _bank()
    assert bank.resolve([_A]) == [0]
    assert bank.resolve([_A]) == [0]
    assert bank.speakers == 1


def test_a_new_voice_opens_a_new_speaker() -> None:
    bank = _bank()
    assert bank.resolve([_A]) == [0]
    assert bank.resolve([_B]) == [1]
    assert bank.resolve([_A]) == [0]
    assert bank.speakers == 2


def test_the_threshold_decides_same_or_new() -> None:
    """Either side of the 0.5 line, from a bank in the same state."""
    near = _bank()
    near.resolve([_A])
    assert near.resolve([_at(50)]) == [0]  # cosine 0.64, the same voice

    far = _bank()
    far.resolve([_A])
    assert far.resolve([_at(70)]) == [1]  # cosine 0.34, somebody else


def test_two_clusters_of_one_call_never_share_a_speaker() -> None:
    """The call already decided these were two people; the bank must agree.

    Both are close enough to the one remembered voice to match it, and a bank
    that matched each independently would answer with one label twice.
    """
    bank = _bank()
    bank.resolve([_A])
    assert sorted(bank.resolve([_at(10), _at(40)])) == [0, 1]


def test_max_speakers_caps_the_bank_and_forces_the_nearest() -> None:
    bank = _bank()
    bank.resolve([_A], max_speakers=2)
    bank.resolve([_B], max_speakers=2)
    assert bank.resolve([_at(90)], max_speakers=2) == [0]  # nearest of the two it may use
    assert bank.speakers == 2


def test_a_forced_match_does_not_move_the_voice_it_borrowed() -> None:
    """A bank at its cap must not learn the speaker it had no room for.

    ``_at(90)`` below is forced onto speaker 0, being the nearest of the two
    voices the cap allows. Had that observation joined speaker 0's centroid, it
    would have swung the centroid to 45 degrees, and ``_at(-50)`` - cosine 0.64
    from the voice this session actually started with, but -0.09 from the swung
    one - would have come back as somebody else.
    """
    bank = _bank()
    bank.resolve([_A], max_speakers=2)
    bank.resolve([_B], max_speakers=2)
    assert bank.resolve([_at(90)], max_speakers=2) == [0]
    assert bank.resolve([_at(-50)], max_speakers=2) == [0]


def test_min_speakers_makes_the_bank_reluctant_to_merge() -> None:
    """A floor is a statement that the table has more than one voice in it."""
    lenient = _bank()
    lenient.resolve([_A])
    assert lenient.resolve([_at(55)]) == [0]  # cosine 0.57, above the plain threshold

    strict = _bank()
    strict.resolve([_A], min_speakers=2)
    assert strict.resolve([_at(55)], min_speakers=2) == [1]  # below the floor's stricter bar
    assert strict.resolve([_at(55)], min_speakers=2) == [1]  # and the floor is met from here on


def test_a_matched_voice_drifts_towards_what_it_heard() -> None:
    """Centroids follow a voice as the session moves, rather than freezing.

    ``_at(80)`` is cosine 0.17 from where this speaker started, far below the
    threshold, and matches only because the earlier ``_at(53)`` observation
    moved the centroid to meet it.
    """
    bank = _bank()
    bank.resolve([_A])
    assert bank.resolve([_at(53)]) == [0]
    assert bank.resolve([_at(80)]) == [0]
    assert bank.speakers == 1


def test_an_unknown_voice_is_a_new_speaker_not_the_nearest_one() -> None:
    bank = _bank()
    bank.resolve([_A])
    assert bank.resolve([_at(80)]) == [1]


def test_sessions_do_not_share_voices() -> None:
    banks = SessionBanks(threshold=_THRESHOLD)
    assert banks.resolve("s1", [_A]) == [0]
    assert banks.resolve("s2", [_B]) == [0]  # a different session starts over
    assert banks.resolve("s1", [_B]) == [1]
    assert len(banks) == 2


def test_deleting_a_session_forgets_its_voices() -> None:
    banks = SessionBanks(threshold=_THRESHOLD)
    banks.resolve("s1", [_A])
    banks.resolve("s1", [_B])
    assert banks.delete("s1") is True
    assert banks.delete("s1") is False  # nothing left to delete, and no error
    assert len(banks) == 0
    assert banks.resolve("s1", [_B]) == [0]  # a fresh bank, numbering from zero


def test_an_idle_session_is_evicted() -> None:
    """The TTL is the backstop for a session end that never arrives."""
    now = [1000.0]
    banks = SessionBanks(threshold=_THRESHOLD, ttl_s=60.0, clock=lambda: now[0])
    banks.resolve("s1", [_A])
    now[0] += 30.0
    assert banks.resolve("s1", [_A]) == [0]  # still inside the TTL, still remembered
    now[0] += 61.0
    assert banks.resolve("s2", [_B]) == [0]  # any call sweeps
    assert len(banks) == 1
    assert banks.resolve("s1", [_B]) == [0]  # s1 was evicted and starts over


def test_the_session_cap_evicts_the_least_recently_used() -> None:
    banks = SessionBanks(threshold=_THRESHOLD, max_sessions=2)
    banks.resolve("s1", [_A])
    banks.resolve("s2", [_A])
    banks.resolve("s1", [_B])  # s1 used again, so s2 is now the oldest
    banks.resolve("s3", [_A])
    assert len(banks) == 2
    assert banks.delete("s2") is False
    assert banks.delete("s1") is True
