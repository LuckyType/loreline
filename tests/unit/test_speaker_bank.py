"""Unit tests for the diarization service's per-session speaker memory.

The bank is what makes a speaker label mean the same person from one
``/diarize`` call to the next. It is plain arithmetic over embeddings, so these
tests drive it with synthetic unit vectors at known angles instead of audio:
``_at(60)`` is a voice at cosine 0.5 from ``_at(0)``, which is exactly the
service's default match threshold.
"""

from __future__ import annotations

import math

import pytest

from services.diarization.app import DEFAULT_MAX_SPEAKERS, SessionBanks, SpeakerBank

_THRESHOLD = 0.5


def _at(degrees: float) -> list[float]:
    """A unit vector whose cosine with ``_at(0)`` is ``cos(degrees)``."""
    radians = math.radians(degrees)
    return [math.cos(radians), math.sin(radians), 0.0]


_A = _at(0)  # one voice
_B = [0.0, 0.0, 1.0]  # another, orthogonal to every _at() vector


def _wide(index: int, width: int) -> list[float]:
    """A unit vector orthogonal to every other ``_wide`` of the same width."""
    return [1.0 if position == index else 0.0 for position in range(width)]


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
    assert set(bank.resolve([_at(10), _at(40)])) == {0, 1}


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


# ---------------------------------------------------------------------------
# What the bank refuses to do
# ---------------------------------------------------------------------------
# Three ways a bank could quietly acquire a speaker nobody was: from a fragment
# too short to identify anyone by, from a second cluster of the call that just
# opened one, and from a vector it cannot really compare at all.


def test_an_unreliable_cluster_borrows_a_known_voice_but_opens_none() -> None:
    """A fifth of a second of audio may recognise Alice; it may not invent Bob.

    The live path sends no ``max_speakers`` - one utterance cannot say how many
    people are at the table - so before this a vector pooled from a 0.2 s
    fragment that landed below the threshold opened a speaker that then lived
    for the whole session and turned up in the rename list as a person nobody
    remembered.
    """
    bank = _bank()
    bank.resolve([_A])
    assert bank.resolve([_at(20)], reliable=[False]) == [0]  # cosine 0.94: Alice
    assert bank.resolve([_B], reliable=[False]) == [None]  # nothing it matches: unlabelled
    assert bank.speakers == 1


def test_an_unreliable_observation_does_not_move_the_voice_it_borrowed() -> None:
    """The mirror of the drift test above, which the same angles pass.

    A trusted ``_at(53)`` pulls the centroid far enough that ``_at(80)`` is
    still the same speaker. An untrusted one must not, or a run of fragments
    too short to place would walk a voice away from the person it belongs to.
    """
    bank = _bank()
    bank.resolve([_A])
    assert bank.resolve([_at(53)], reliable=[False]) == [0]
    assert bank.resolve([_at(80)]) == [1]  # the centroid never followed the fragment


def test_two_new_voices_in_one_call_do_not_collapse_onto_each_other() -> None:
    """A cluster forced by the cap must not land on one opened moments earlier.

    Both are strangers to this bank and the cap leaves room for exactly one
    more. The first opens speaker 1; the second is forced onto the nearest
    voice still free, which has to be the one the call did not just create, or
    two clusters the call itself told apart come back under one label.
    """
    bank = _bank()
    bank.resolve([_A], max_speakers=2)
    assert set(bank.resolve([_at(85), _at(95)], max_speakers=2)) == {0, 1}


def test_a_session_stops_opening_voices_at_the_default_cap() -> None:
    """Nothing else bounds the live path, which sends no speaker counts at all."""
    width = DEFAULT_MAX_SPEAKERS + 2
    bank = SpeakerBank(threshold=_THRESHOLD)
    ids = [bank.resolve([_wide(index, width)])[0] for index in range(width)]
    assert bank.speakers == DEFAULT_MAX_SPEAKERS
    assert len(set(ids)) == DEFAULT_MAX_SPEAKERS  # the last two borrowed a label


def test_an_embedding_of_another_width_is_refused_rather_than_truncated() -> None:
    """Two widths mean two models, and a truncated dot product still scores.

    Nothing in the numbers would say the answer was nonsense, so the call fails
    instead of putting a plausible name on the words.
    """
    bank = _bank()
    bank.resolve([_A])
    with pytest.raises(ValueError, match="width"):
        bank.resolve([[1.0, 0.0]])


def test_the_cap_can_be_lifted_for_a_caller_that_polices_it_itself() -> None:
    """``DIAR_MAX_SPEAKERS=0`` is how a deployment opts out of the default."""
    bank = SpeakerBank(threshold=_THRESHOLD, max_voices=0)
    width = DEFAULT_MAX_SPEAKERS + 2
    ids = [bank.resolve([_wide(index, width)])[0] for index in range(width)]
    assert ids == list(range(width))
