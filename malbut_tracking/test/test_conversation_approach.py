"""Conversation positioning stays bounded without changing continuous following."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from malbut_tracking.conversation_approach import ConversationApproach
from malbut_tracking.geometry import Point2D


def observe(policy, now=0., *, target=(2.5, 0.), robot=(0., 0.), track='a', metric=True):
    return policy.observe(now, Point2D(*target), Point2D(*robot), track, metric=metric)


def test_walk_to_standoff_then_align_once():
    policy = ConversationApproach(0., 1.5)
    assert observe(policy) is None
    for now, x in ((.2, .3), (.4, .7), (.6, 1.0)):
        assert observe(policy, now, robot=(x, 0.)) is None
        assert not policy.reached(now, 2.5 - x, .3)
    for now in (.8, 1.0, 1.2, 1.4):
        assert observe(policy, now, robot=(1., 0.)) is None
        reached = policy.reached(now, 1.5, .05)
    assert reached


@pytest.mark.parametrize('kwargs', [
    {'target': (2.9, 0.)}, {'target': (2.5, .4)}, {'track': 'b'},
    {'metric': False}, {'track': ''}, {'target': (float('nan'), 0.)},
    {'robot': (2.1, 0.)},
])
def test_no_chasing_track_substitution_or_uncertain_depth(kwargs):
    policy = ConversationApproach(0., 1.5)
    assert observe(policy) is None
    assert observe(policy, .2, **kwargs)


def test_acquisition_loss_and_total_timeout_are_separate():
    policy = ConversationApproach(0., 1.5)
    assert policy.expired(2.9) is None
    assert policy.expired(3.) == 'no metric person observation'
    policy = ConversationApproach(0., 1.5)
    assert observe(policy, 1.) is None
    assert policy.expired(1.61) == 'person observation lost'
    for step in range(6, 100):
        assert observe(policy, step / 5) is None
    assert observe(policy, 20.) == 'conversation positioning timed out'


def test_reached_requires_continuous_alignment_and_current_camera():
    policy = ConversationApproach(0., 1.5)
    assert observe(policy) is None
    assert not policy.reached(0., 1.5, 0.)
    assert not policy.reached(.2, 1.5, .2)
    assert not policy.reached(.4, 1.5, 0.)
    assert not policy.reached(.7, 1.5, 0.)  # stale camera cannot complete a turn
    assert observe(ConversationApproach(0., 1.5), target=(3.1, 0.))


def test_loss_in_approach_never_starts_recovery():
    pytest.importorskip('rclpy')
    from malbut_tracking.person_follower_node import PersonFollowerNode

    follower = SimpleNamespace(_approach=ConversationApproach(0., 1.5),
                               _finish_approach=Mock())
    PersonFollowerNode._begin_loss_recovery(follower, 1.)
    follower._finish_approach.assert_called_once_with(
        False, 'person lost; conversation approach will not search')


def test_delayed_camera_frame_does_not_extend_freshness_from_arrival():
    policy = ConversationApproach(0., 1.5)
    assert policy.observe(1., Point2D(2., 0.), Point2D(0., 0.), 'a',
                          metric=True, age=.5) is None
    assert policy.expired(1.11) == 'person observation lost'
    assert ConversationApproach(0., 1.5).observe(
        1., Point2D(2., 0.), Point2D(0., 0.), 'a', metric=True, age=.7)
