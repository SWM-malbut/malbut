"""ReID descriptors for the person tracking's splits (SWM25-267 decision 10). A fake
encoder and decoder; unit tests, not a model check (that ran on the Jetson, 10-19)."""

from malbut_agent_server.application.fall_appearance import (
    EVERY_S, KEEP_S, PAIR_GAP_S, FallAppearance,
)

BOX = (0.25, 0.5, 0.5, 1.0)


class Image:
    shape = (480, 640, 3)


def looks(*vectors):
    """An encoder answering the given vectors in turn; it records the pixel boxes."""
    calls = []

    def encode(image, boxes):
        calls.append(boxes)
        return [vectors[(len(calls) - 1) % len(vectors)]] * len(boxes)
    return encode, calls


def store(*vectors):
    encode, calls = looks(*vectors)
    return FallAppearance(encode=encode, decode=lambda jpeg: Image()), calls


def test_a_pair_of_looks_a_second_apart_every_five_seconds_in_pixels():
    seen, calls = store([1.0, 0.0])
    for at in (100.0, 100.4, 100.0 + PAIR_GAP_S, 103.0, 100.0 + EVERY_S, 104.6,
               100.0 + EVERY_S + PAIR_GAP_S):
        seen.observe(at, b'jpeg', [('a', BOX)])
    assert calls[0] == [(160.0, 240.0, 320.0, 480.0)]
    assert [at for at, _, _ in seen._tracks['a']] == [100.0, 101.0, 105.0, 106.0]
    assert [first for _, _, first in seen._tracks['a']] == [True, False, True, False]


def test_two_against_two_the_last_pair_before_and_the_first_pair_after():
    seen, _ = store([1.0, 0.0], [0.8, 0.6], [0.0, 1.0], [0.6, 0.8])
    for at in (100.0, 101.0):  # The one who left: [1, 0] and [0.8, 0.6].
        seen.observe(at, b'jpeg', [('old', BOX)])
    seen.observe(700.0, b'jpeg', [('new', BOX)])
    assert not seen.ready(('new', 700.0))
    assert seen.compare(('old', 101.0), ('new', 700.0)) is None  # One look after: wait.
    seen.observe(701.0, b'jpeg', [('new', BOX)])  # [0, 1] and [0.6, 0.8].
    assert seen.ready(('new', 700.0))
    old, new = (0.9, 0.3), (0.3, 0.9)  # The two means, before normalizing.
    expected = (old[0] * new[0] + old[1] * new[1]) / (sum(x * x for x in old) ** 0.5) ** 2
    assert abs(seen.compare(('old', 101.0), ('new', 700.0)) - expected) < 1e-9


def test_without_the_encoder_or_model_the_answer_is_none():
    def missing(path):
        raise ImportError('malbut_reid')
    seen = FallAppearance(loader=missing)
    seen.observe(100.0, b'jpeg', [('a', BOX)])
    assert not seen.available and seen.compare(('a', 100.0), ('a', 100.0)) is None


def test_a_broken_frame_loses_a_look_never_the_fall_check():
    def broken(jpeg):
        raise ValueError('not a jpeg')
    encode, calls = looks([1.0, 0.0])
    seen = FallAppearance(encode=encode, decode=broken)
    seen.observe(100.0, b'jpeg', [('a', BOX)])
    assert seen.failures == 1 and calls == []


def test_people_unseen_for_30_minutes_are_dropped():
    seen, _ = store([1.0, 0.0])
    for at in (100.0, 101.0):
        seen.observe(at, b'jpeg', [('a', BOX)])
    seen.observe(102.0 + KEEP_S, b'jpeg', [('b', BOX)])
    assert 'a' not in seen._tracks
