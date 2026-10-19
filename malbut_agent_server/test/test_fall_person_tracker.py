"""Who is who across pose frames (SWM25-267, design decisions 3-5). Made-up frames at
five a second; unit tests, not robot checks. Replay on recorded days comes later."""

import pytest

from malbut_agent_server.fall_rules.person_tracker import (
    FORGET_S, MAX_PEOPLE, SHORT_GONE_S, SOLO_S, Frame, Observation, PersonTracker,
)

DT = 0.2
BOX_A = (0.10, 0.40, 0.30, 0.80)
BOX_B = (0.60, 0.40, 0.80, 0.80)
NUDGED = (0.25, 0.40, 0.45, 0.80)  # overlaps BOX_A by 0.14 (< BOX_IOU)


class Clock:
    def __init__(self, tracker):
        self.tracker, self.t, self.view = tracker, 100.0, 0

    def frame(self, *people):
        self.t = round(self.t + DT, 3)
        return self.tracker.update(Frame(self.t, self.view, tuple(people)))

    def frames(self, seconds, *people):
        result = None
        for _ in range(round(seconds / DT)):
            result = self.frame(*people)
        return result

    def wait(self, seconds):
        self.t = round(self.t + seconds, 3)


def seen(track, point, box=BOX_A, state='unknown'):
    return Observation(track, box, point, state)


@pytest.fixture
def robot():
    return Clock(PersonTracker(run_id='test'))


def test_numbers_carry_a_run_prefix_and_one_person_keeps_one_number(robot):
    first = robot.frame(seen('pose:1:a', (3.0, 1.0)))
    assert first.assignments == {'pose:1:a': 'test:P1'} and first.changed == first.assignments
    again = robot.frames(2.0, seen('pose:1:a', (3.05, 1.0)))
    assert again.assignments == {'pose:1:a': 'test:P1'} and again.changed == {}
    assert PersonTracker().run_id != PersonTracker().run_id  # A restart never reuses numbers.


def test_a_new_yolo_track_at_the_same_place_is_the_same_person(robot):
    robot.frames(1.0, seen('pose:1:a', (3.0, 1.0)))
    result = robot.frame(seen('pose:1:b', (3.1, 1.0)))  # YOLO churn while lying (no alias needed)
    assert result.assignments == result.changed == {'pose:1:b': 'test:P1'}


def test_a_track_handed_to_someone_far_away_is_not_trusted(robot):
    """10-17: YOLO gave 95bd to 사람2 beside the robot; 사람1 stayed under the table."""
    robot.frames(2.0, seen('95bd', (3.0, 1.0)))
    result = robot.frame(seen('95bd', (0.6, 0.0), BOX_B))
    assert result.assignments == {'95bd': 'test:P2'}
    assert not robot.tracker.uncertain('test:P2')


def test_the_10_17_layout_is_two_people_with_or_without_depth(robot):
    robot.frames(3.0, seen('t1', (3.0, 1.0)))
    both = robot.frame(seen('t1', (3.0, 1.0)), seen('t2', (0.6, 0.0), BOX_B))
    assert both.assignments == {'t1': 'test:P1', 't2': 'test:P2'}
    no_depth = Clock(PersonTracker(run_id='n'))
    no_depth.frames(3.0, seen('t1', None, BOX_A))
    result = no_depth.frame(seen('t1', None, BOX_A), seen('t2', None, BOX_B))
    assert result.assignments == {'t1': 'n:P1', 't2': 'n:P2'}
    # 사람1 is still in view, so the newcomer could not be 사람1: nothing uncertain.
    assert not no_depth.tracker.uncertain('n:P2')


def test_moving_robot_still_follows_by_map_place(robot):
    robot.frames(1.0, seen('a', (3.0, 1.0)))
    robot.view = 1  # The robot drives: boxes do not compare across views, places do.
    result = robot.frame(seen('b', (3.1, 1.05), BOX_B))
    assert result.assignments == {'b': 'test:P1'}


def test_without_depth_the_same_view_box_continues_and_keeps_the_last_place(robot):
    robot.frames(1.0, seen('a', (3.0, 1.0)))
    result = robot.frame(seen('b', None, (0.11, 0.41, 0.31, 0.81)))
    assert result.assignments == {'b': 'test:P1'}
    assert robot.tracker.people['test:P1'].point == (3.0, 1.0)


def test_without_depth_someone_gone_elsewhere_in_view_is_not_near(robot):
    """Design decision 2 "근처": 사람2 lost for a moment on the other side of the screen
    is not who a new box under the table could be."""
    robot.frames(1.0, seen('a', None, BOX_A))
    robot.wait(1.0)  # 'a' unseen for a second; a new track with no box overlap and no place.
    result = robot.frame(seen('b', None, BOX_B))
    assert result.assignments == {'b': 'test:P2'}
    assert not robot.tracker.uncertain('test:P2') and not robot.tracker.uncertain('test:P1')


def test_without_depth_someone_just_gone_from_another_view_could_be_them(robot):
    robot.frames(1.0, seen('a', None, BOX_A))
    robot.view = 1  # The robot moved: boxes do not compare, so 'a' could be anyone now.
    result = robot.frames(0.6, seen('b', None, BOX_B))  # Seen in three frames: counts.
    assert result.assignments == {'b': 'test:P2'}
    assert robot.tracker.uncertain('test:P2') and robot.tracker.uncertain('test:P1')


def test_without_depth_a_box_overlapping_where_someone_was_is_uncertain(robot):
    robot.frames(1.0, seen('a', None, BOX_A))
    robot.wait(1.0)
    robot.frames(0.6, seen('b', None, NUDGED))  # Overlaps BOX_A, less than BOX_IOU.
    assert robot.tracker.uncertain('test:P2') and robot.tracker.uncertain('test:P1')


# ------------------------------------------------------------ depth back (decision 2)


def lost_depth(robot):
    """P1 measured at (3.0, 1.0) for a second, then a new track without depth beside it."""
    robot.frames(1.0, seen('a', (3.0, 1.0)))  # Too short for a stayed-put anchor.
    robot.wait(1.0)
    robot.frames(0.6, seen('b', None, NUDGED))
    assert robot.tracker.uncertain('test:P2') and robot.tracker.uncertain('test:P1')


def test_depth_back_at_the_last_place_turns_the_new_number_back_into_that_person(robot):
    robot.tracker.reid = False
    lost_depth(robot)
    result = robot.frame(seen('b', (3.2, 1.0), NUDGED))
    assert result.assignments == result.changed == {'b': 'test:P1'}
    assert result.merged == {'test:P2': 'test:P1'} and 'test:P2' not in robot.tracker.people
    assert not robot.tracker.uncertain('test:P1') and robot.tracker.groups == []
    assert robot.frame(seen('b', (3.2, 1.0), NUDGED)).assignments == {'b': 'test:P1'}


def test_depth_back_asks_reid_as_reappearing_does(robot):
    lost_depth(robot)
    robot.frame(seen('b', (3.2, 1.0), NUDGED))
    request, = robot.tracker.take_requests()
    assert request['a']['track'] == 'a' and request['b']['track'] == 'b'
    assert robot.tracker.answer(request['request_id'], 0.80) is None
    assert not robot.tracker.uncertain('test:P1')


def test_depth_back_further_than_1_m_stays_uncertain(robot):
    lost_depth(robot)
    result = robot.frame(seen('b', (4.5, 1.0), NUDGED))  # Moved: cannot tell.
    assert result.merged == {} and robot.tracker.uncertain('test:P2')


def test_depth_back_between_two_who_were_there_stays_uncertain(robot):
    robot.frames(1.0, seen('a', (3.0, 1.0)), seen('c', (3.0, 1.6), BOX_B))
    robot.wait(1.0)
    robot.frames(0.6, seen('b', None, (0.25, 0.40, 0.65, 0.80)))  # Overlaps both boxes.
    result = robot.frame(seen('b', (3.0, 1.3), (0.25, 0.40, 0.65, 0.80)))
    assert result.merged == {} and robot.tracker.uncertain('test:P3')


def test_two_candidates_equally_near_make_it_uncertain(robot):
    robot.frames(1.0, seen('a', (3.0, 1.0)), seen('b', (3.0, 1.6), BOX_B))
    robot.wait(1.0)
    result = robot.frame(seen('c', (3.0, 1.3)))  # Halfway: cannot choose.
    assert result.assignments['c'] in ('test:P1', 'test:P2')
    assert robot.tracker.uncertain('test:P1') and robot.tracker.uncertain('test:P2')


def test_seen_again_within_30_minutes_at_the_same_place_is_the_same_person(robot):
    robot.frames(1.0, seen('a', (3.0, 1.0)))
    robot.wait(600)  # Patrol in another room.
    assert robot.frame(seen('z', (3.4, 1.0))).assignments == {'z': 'test:P1'}
    robot.wait(FORGET_S)
    assert robot.frame(seen('y', (3.4, 1.0))).assignments == {'y': 'test:P2'}
    assert 'test:P1' not in robot.tracker.people


def test_seen_again_further_than_1_m_is_a_new_person(robot):
    robot.frames(1.0, seen('a', (3.0, 1.0)))
    robot.wait(SHORT_GONE_S + 5)
    assert robot.frame(seen('z', (4.5, 1.0))).assignments == {'z': 'test:P2'}


def test_two_unseen_people_near_where_someone_reappears_make_it_uncertain(robot):
    robot.frames(1.0, seen('a', (3.0, 1.0)), seen('b', (3.0, 1.8), BOX_B))
    robot.wait(60)
    robot.frame(seen('c', (3.0, 1.4)))
    assert robot.tracker.uncertain('test:P1') and robot.tracker.uncertain('test:P2')


def test_a_helper_who_came_and_left_clears_once_apart(robot):
    """The person on the floor stayed put; the helper came close, then walked off."""
    floor = (3.0, 1.0)
    robot.frames(12.0, seen('lie', floor, state='suspected'))
    for step in range(10):  # The helper walks in from 2 m away.
        robot.frame(seen('lie', floor, state='suspected'),
                    seen('help', (3.0, 3.0 - step * 0.18), BOX_B))
    assert robot.tracker.uncertain('test:P1') and robot.tracker.uncertain('test:P2')
    # YOLO swaps the tracks while they overlap; then the helper leaves.
    for step in range(10):
        robot.frame(seen('help', floor, state='suspected'),
                    seen('lie', (3.0, 1.4 + step * 0.2), BOX_B))
    assert not robot.tracker.uncertain('test:P1') and not robot.tracker.uncertain('test:P2')
    # The one at the floor spot is the person who lay there (P1), whatever YOLO calls it now.
    assert robot.tracker.person_of('help') == 'test:P1'


def test_a_box_for_a_frame_or_two_makes_no_one_uncertain(robot):
    """10-19 18:28: boxes on one person for a frame or two at the screen edge."""
    robot.frames(1.0, seen('a', None, BOX_A))
    robot.wait(1.0)
    robot.frames(0.4, seen('b', None, NUDGED))  # Two frames, then gone.
    robot.frames(1.0, seen('a', None, BOX_A))
    assert not robot.tracker.uncertain('test:P1') and robot.tracker.groups == []
    robot.frames(0.4, seen('a', (3.0, 1.0)), seen('dup', (3.1, 1.0)))  # A double, twice...
    assert not robot.tracker.uncertain('test:P1')
    robot.frame(seen('a', (3.0, 1.0)), seen('dup', (3.1, 1.0)))  # ...and a third frame:
    assert robot.tracker.uncertain('test:P1')  # two people this close, uncertain.


def test_one_of_them_alone_in_one_place_for_a_minute_clears_it(robot):
    """10-19 18:28: one person lay still for five minutes, uncertain 94% of the time."""
    robot.frames(1.0, seen('a', (3.0, 1.0)), seen('b', (3.2, 1.0), BOX_B))
    assert robot.tracker.uncertain('test:P1')
    robot.frames(SOLO_S - 1, seen('a', (3.0, 1.0)))  # 'b' never seen again.
    assert robot.tracker.uncertain('test:P1')
    robot.frames(2.0, seen('a', (3.1, 1.0)))
    assert not robot.tracker.uncertain('test:P1') and not robot.tracker.uncertain('test:P2')


def test_not_alone_long_enough_keeps_it_uncertain(robot):
    robot.frames(1.0, seen('a', (3.0, 1.0)), seen('b', (3.2, 1.0), BOX_B))
    robot.frames(30.0, seen('a', (3.0, 1.0)))
    robot.frames(1.0, seen('a', (3.0, 1.0)), seen('b', (3.7, 1.0), BOX_B))  # 'b' back.
    robot.frames(40.0, seen('a', (3.0, 1.0)))
    assert robot.tracker.uncertain('test:P1')  # Alone 40 s since, not 60.


def test_alone_but_walking_away_keeps_it_uncertain(robot):
    robot.frames(1.0, seen('a', (3.0, 1.0)), seen('b', (3.2, 1.0), BOX_B))
    for step in range(int(SOLO_S / DT) + 10):  # Alone, walking across the room.
        robot.frame(Observation('a', None, (3.0 + step * 0.05, 1.0)))
    assert robot.tracker.uncertain('test:P1')


def test_two_people_who_crossed_while_walking_stay_uncertain(robot):
    for step in range(15):
        robot.frame(seen('a', (1.0 + step * 0.15, 1.0)),
                    seen('b', (3.2 - step * 0.15, 1.1), BOX_B))
    assert robot.tracker.uncertain('test:P1') and robot.tracker.uncertain('test:P2')


def test_forgetting_the_other_one_clears_it(robot):
    robot.frames(1.0, seen('a', (3.0, 1.0)), seen('b', (3.2, 1.0), BOX_B))
    assert robot.tracker.uncertain('test:P1')
    robot.frames(FORGET_S, seen('a', (3.0, 1.0)))  # b never seen again.
    assert not robot.tracker.uncertain('test:P1') and 'test:P2' not in robot.tracker.people


def test_two_on_a_sofa_are_uncertain_but_nothing_else_happens(robot):
    result = robot.frames(5.0, seen('a', (2.0, 0.0), state='clear'),
                          seen('b', (2.4, 0.0), BOX_B, state='clear'))
    assert result.assignments == {'a': 'test:P1', 'b': 'test:P2'} and result.stood_up == []
    assert robot.tracker.uncertain('test:P1')


def test_standing_up_is_told_once_per_person(robot):
    robot.frames(1.0, seen('a', (3.0, 1.0), state='suspected'))
    assert robot.frame(seen('b', (3.0, 1.05), state='clear')).stood_up == ['test:P1']
    assert robot.frame(seen('b', (3.0, 1.05), state='clear')).stood_up == []


def test_a_suspicion_before_its_frame_is_matched_the_same_way(robot):
    robot.frames(1.0, seen('a', (3.0, 1.0)))
    assert robot.tracker.assign(seen('a', (3.0, 1.0)), robot.t + 0.1, 0) == ('test:P1', False)
    newcomer = robot.tracker.assign(seen('x', (0.5, 0.0), BOX_B), robot.t + 0.1, 0)
    assert newcomer == ('test:P2', False)
    assert robot.frame(seen('x', (0.5, 0.0), BOX_B)).assignments == {'x': 'test:P2'}


def test_no_more_than_64_people_are_remembered(robot):
    for n in range(MAX_PEOPLE + 5):
        robot.frame(seen(f't{n}', (n * 5.0, 0.0)))
        robot.wait(SHORT_GONE_S + 1)
    assert len(robot.tracker.people) == MAX_PEOPLE
    assert 'test:P1' not in robot.tracker.people  # The longest unseen went first.


# ------------------------------------------------------------ ReID splits (decision 10)


def reappeared(robot):
    robot.frames(1.0, seen('a', (3.0, 1.0)))
    robot.wait(600)
    robot.frame(seen('z', (3.3, 1.0)))
    request, = robot.tracker.take_requests()
    return request


def test_someone_reappearing_where_one_was_is_them_until_reid_says_otherwise(robot):
    request = reappeared(robot)
    assert request['a']['track'] == 'a' and request['b']['track'] == 'z'
    assert robot.tracker.uncertain('test:P1') and robot.tracker.person_of('z') == 'test:P1'
    assert robot.tracker.answer(request['request_id'], 0.80) is None  # Alike: the same.
    assert not robot.tracker.uncertain('test:P1')


def test_clearly_unlike_splits_off_someone_new_and_restores_who_was_unseen(robot):
    request = reappeared(robot)
    old, new, since = robot.tracker.answer(request['request_id'], 0.50)
    assert (old, new) == ('test:P1', 'test:P2') and since == robot.t
    assert robot.tracker.person_of('z') == 'test:P2'
    assert robot.tracker.people['test:P1'].point == (3.0, 1.0)  # Where P1 was last seen.
    assert robot.frame(seen('z', (3.3, 1.0))).assignments == {'z': 'test:P2'}


def test_no_answer_or_none_goes_by_place(robot):
    request = reappeared(robot)
    assert robot.tracker.answer(request['request_id'], None) is None
    assert not robot.tracker.uncertain('test:P1')
    robot.wait(600)
    robot.frame(seen('y', (3.3, 1.0)))
    robot.tracker.take_requests()
    robot.frames(6.0, seen('y', (3.3, 1.0)))  # Unanswered for 5 s.
    assert not robot.tracker.uncertain('test:P1') and robot.tracker.pending == {}


def test_reid_is_not_asked_when_two_could_be_them(robot):
    robot.frames(1.0, seen('a', (3.0, 1.0)), seen('b', (3.0, 1.8), BOX_B))
    robot.wait(60)
    robot.frame(seen('c', (3.0, 1.4)))
    assert robot.tracker.take_requests() == []  # Uncertain; ReID never chooses (only splits).
