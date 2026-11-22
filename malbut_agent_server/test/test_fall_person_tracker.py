"""Who is who across pose frames (SWM25-267, design decisions 3-5). Made-up frames at
five a second; unit tests, not robot checks. Replay on recorded days comes later."""

import pytest

from malbut_agent_server.fall_rules.person_tracker import (
    FORGET_S, LINK_S, MAX_PEOPLE, REID_WAIT_S, SHORT_GONE_S, SOLO_S, Frame, Observation,
    PersonTracker,
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
    robot.tracker.reid = False  # The rule by place; ReID's check is tested below.
    robot.frames(1.0, seen('a', (3.0, 1.0)), seen('b', (3.2, 1.0), BOX_B))
    assert robot.tracker.uncertain('test:P1')
    robot.frames(SOLO_S - 1, seen('a', (3.0, 1.0)))  # 'b' never seen again.
    assert robot.tracker.uncertain('test:P1')
    robot.frames(2.0, seen('a', (3.1, 1.0)))
    assert not robot.tracker.uncertain('test:P1') and not robot.tracker.uncertain('test:P2')


def test_not_alone_long_enough_keeps_it_uncertain(robot):
    robot.tracker.reid = False  # The rule by place; ReID's check is tested below.
    robot.frames(1.0, seen('a', (3.0, 1.0)), seen('b', (3.2, 1.0), BOX_B))
    robot.frames(40.0, seen('a', (3.0, 1.0)))
    assert robot.tracker.uncertain('test:P1')  # Alone and apart 40 s, not 60.
    robot.frames(21.0, seen('a', (3.0, 1.0)))
    assert not robot.tracker.uncertain('test:P1')


def test_alone_but_walking_away_keeps_it_uncertain(robot):
    robot.frames(1.0, seen('a', (3.0, 1.0)), seen('b', (3.2, 1.0), BOX_B))
    for step in range(int(55 / DT)):  # Alone, walking across the room.
        robot.frame(Observation('a', None, (3.0 + step * 0.05, 1.0)))
    assert robot.tracker.uncertain('test:P1')


def test_meeting_again_renews_and_who_stayed_put_is_told_when_they_part(robot):
    robot.tracker.reid = False  # By place alone here (ReID would wait for its answer).
    robot.frames(1.0, seen('a', (3.0, 1.0)), seen('b', (3.2, 1.0), BOX_B))
    robot.frames(30.0, seen('a', (3.0, 1.0)))
    robot.frames(1.0, seen('a', (3.0, 1.0)), seen('b', (3.3, 1.0), BOX_B))  # Met again.
    assert robot.tracker.uncertain('test:P1')
    robot.frame(seen('a', (3.0, 1.0)))  # 'a' had stayed put: told apart as 'b' leaves.
    assert not robot.tracker.uncertain('test:P1') and not robot.tracker.uncertain('test:P2')


def test_two_who_do_not_meet_again_for_a_minute_are_apart(robot):
    """As MHT commits after its horizon: both seen apart, never close again (10-17)."""
    robot.tracker.reid = False  # The rule by place; ReID's check is tested below.
    robot.frames(1.0, seen('a', (3.0, 1.0)), seen('b', (3.2, 1.0), BOX_B))
    robot.frames(50.0, seen('a', (3.0, 1.0)), seen('b', (6.0, 1.0), BOX_B))
    assert robot.tracker.uncertain('test:P1') and robot.tracker.uncertain('test:P2')
    robot.frames(11.0, seen('a', (3.0, 1.0)), seen('b', (6.0, 1.0), BOX_B))
    assert not robot.tracker.uncertain('test:P1') and not robot.tracker.uncertain('test:P2')


def test_a_group_splits_where_its_links_were_not_renewed(robot):
    robot.tracker.reid = False  # The rule by place; ReID's check is tested below.
    robot.frames(1.0, seen('a', (3.0, 1.0)), seen('b', (3.2, 1.0), BOX_B))  # a-b now.
    for step in range(15):  # 'b' walks off.
        robot.frame(seen('a', (3.0, 1.0)), seen('b', (3.4 + step * 0.2, 1.0), BOX_B))
    robot.frames(37.0, seen('a', (3.0, 1.0)), seen('b', (6.2, 1.0), BOX_B))
    robot.frames(1.0, seen('a', (3.0, 1.0)), seen('b', (6.2, 1.0), BOX_B),
                 seen('c', (6.4, 1.0), BOX_A))  # b-c 40 s later.
    group, = robot.tracker.groups
    assert group.members == {'test:P1', 'test:P2', 'test:P3'}
    robot.frames(21.0, seen('a', (3.0, 1.0)), seen('b', (6.2, 1.0), BOX_B),
                 seen('c', (6.4, 1.0), BOX_A))  # b and c stay together.
    assert not robot.tracker.uncertain('test:P1')  # a-b not renewed for a minute.
    assert robot.tracker.uncertain('test:P2') and robot.tracker.uncertain('test:P3')


def apart_a_minute(robot):
    """Two close, then both seen apart for a minute: their link is due."""
    robot.frames(1.0, seen('a', (3.0, 1.0)), seen('b', (3.2, 1.0), BOX_B))
    for step in range(15):  # 'b' walks off.
        robot.frame(seen('a', (3.0, 1.0)), seen('b', (3.4 + step * 0.2, 1.0), BOX_B))
    robot.frames(LINK_S - 4, seen('a', (3.0, 1.0)), seen('b', (6.2, 1.0), BOX_B))
    assert robot.tracker.take_requests() == []
    robot.frames(3.0, seen('a', (3.0, 1.0)), seen('b', (6.2, 1.0), BOX_B))  # A minute on.
    requests = robot.tracker.take_requests()
    assert {r['a']['track'] for r in requests} == {'a', 'b'}  # Each: then against now.
    assert robot.tracker.uncertain('test:P1')  # Not committed while ReID is asked.
    return requests


def test_before_committing_reid_checks_each_still_looks_as_when_linked(robot):
    for request in apart_a_minute(robot):
        robot.tracker.answer(request['request_id'], 0.80)
    robot.frame(seen('a', (3.0, 1.0)), seen('b', (6.2, 1.0), BOX_B))
    assert not robot.tracker.uncertain('test:P1') and not robot.tracker.uncertain('test:P2')


def test_clearly_unlike_under_a_number_keeps_them_uncertain_a_minute_more(robot):
    first, second = apart_a_minute(robot)
    robot.tracker.answer(first['request_id'], 0.80)
    robot.tracker.answer(second['request_id'], 0.40)  # Maybe swapped while close.
    robot.frames(30.0, seen('a', (3.0, 1.0)), seen('b', (6.2, 1.0), BOX_B))
    assert robot.tracker.uncertain('test:P1') and robot.tracker.uncertain('test:P2')
    robot.frames(31.0, seen('a', (3.0, 1.0)), seen('b', (6.2, 1.0), BOX_B))
    assert len(robot.tracker.take_requests()) == 2  # Asked again a minute later.


def test_no_reid_answer_commits_by_place_after_the_wait(robot):
    apart_a_minute(robot)
    robot.frames(REID_WAIT_S + 1, seen('a', (3.0, 1.0)), seen('b', (6.2, 1.0), BOX_B))
    assert not robot.tracker.uncertain('test:P1') and not robot.tracker.uncertain('test:P2')


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


LYING = (0.08, 0.08, 0.99, 0.63)      # 10-20 15:15, lying 1 m from the robot
STANDING = (0.08, 0.0, 0.23, 0.48)    # 10-20 15:16, standing there: head cut off, unknown


def stood_up_while(robot, seconds, *people):
    """Every person number told as up over these frames."""
    up = []
    for _ in range(round(seconds / DT)):
        up += robot.frame(*people).stood_up
    return up


def test_standing_up_cut_off_at_the_top_is_up_after_2_seconds(robot):
    """10-20 15:16 (SWM25-269): YOLO-Pose never said clear, so nobody came to look."""
    robot.frames(1.0, seen('a', (0.1, 0.5), LYING, state='suspected'))
    assert stood_up_while(robot, 1.6, seen('a', None, STANDING)) == []
    assert stood_up_while(robot, 1.0, seen('a', None, STANDING)) == ['test:P1']  # Once.


def test_walking_a_metre_from_where_they_lay_is_up(robot):
    robot.frames(1.0, seen('a', (3.0, 1.0), LYING, state='suspected'))
    up = []
    for step in range(1, 11):  # Walks 1.1 m away in two seconds (pose unknown).
        up += robot.frame(seen('a', (3.0, 1.0 + 0.11 * step), LYING)).stood_up
    assert up == []
    assert stood_up_while(robot, 1.6, seen('a', (3.0, 2.1), LYING)) == []
    assert stood_up_while(robot, 1.0, seen('a', (3.0, 2.1), LYING)) == ['test:P1']


def test_lying_on_with_unknown_frames_and_a_wandering_place_is_not_up(robot):
    for _ in range(20):
        robot.frames(0.4, seen('a', (3.0, 1.0), LYING, state='suspected'))
        assert stood_up_while(robot, 1.0, seen('a', (3.0, 1.7), LYING)) == []


def test_while_the_robot_moves_nobody_is_taken_as_up(robot):
    """10-19 22:33: turning, the robot put the lying person's place 1.1 m off."""
    robot.frames(1.0, seen('a', (3.0, 1.0), LYING, state='suspected'))
    up = []
    for step in range(1, 26):  # A standing box drifting 1.1 m off over five seconds.
        robot.t = round(robot.t + DT, 3)
        moved = seen('a', (3.0, 1.0 + 0.044 * step), STANDING)
        up += robot.tracker.update(Frame(robot.t, 0, (moved,), stationary=False)).stood_up
    assert up == []
    assert stood_up_while(robot, 2.2, seen('a', (3.0, 2.1), STANDING)) == ['test:P1']


def test_a_corner_scrap_or_someone_never_down_is_not_up(robot):
    robot.frames(1.0, seen('a', (3.0, 1.0), LYING, state='suspected'))
    assert stood_up_while(robot, 5.0, seen('a', None, (0.0, 0.0, 0.07, 0.17))) == []  # 05:31
    assert stood_up_while(robot, 5.0, seen('b', None, STANDING, state='unknown'),
                          seen('a', None, (0.0, 0.0, 0.07, 0.17))) == []


def test_a_suspicion_before_its_frame_is_matched_the_same_way(robot):
    robot.frames(1.0, seen('a', (3.0, 1.0)))
    assert robot.tracker.assign(seen('a', (3.0, 1.0)), robot.t + 0.1, 0) == ('test:P1', False)
    newcomer = robot.tracker.assign(seen('x', (0.5, 0.0), BOX_B), robot.t + 0.1, 0)
    assert newcomer == ('test:P2', False)
    assert robot.frame(seen('x', (0.5, 0.0), BOX_B)).assignments == {'x': 'test:P2'}


def test_no_more_than_64_people_are_remembered(robot):
    for n in range(MAX_PEOPLE + 5):
        robot.frames(3 * DT, seen(f't{n}', (n * 5.0, 0.0)))
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


# One person, one number, when they fill the screen (2026-11-17 09:14).
WHOLE = (0.00, 0.00, 0.34, 0.65)     # the body's first box, at the screen edge
PART = (0.10, 0.21, 0.95, 0.63)      # 0.44 of WHOLE lies in it (IoU 0.21)
APART = (0.30, 0.21, 0.95, 0.63)     # 0.08 of WHOLE


def test_a_placeless_new_track_inside_someones_box_is_that_body(robot):
    robot.frames(1.0, seen('a', (3.0, 1.0), WHOLE))
    result = robot.frames(1.0, seen('a', None, WHOLE), seen('b', None, PART, 'suspected'))
    assert result.assignments == {'a': 'test:P1', 'b': 'test:P1'}
    assert list(robot.tracker.people) == ['test:P1']
    # Its suspicion is that person's, so it joins their incident.
    assert robot.tracker.assign(Observation('b', PART, None), robot.t, 0) == ('test:P1', False)


def test_a_new_track_barely_touching_a_box_or_with_a_place_is_someone_else(robot):
    robot.frames(1.0, seen('a', (3.0, 1.0), WHOLE))
    result = robot.frame(seen('a', None, WHOLE), seen('b', None, APART),
                         seen('c', (5.0, 1.0), PART))
    assert result.assignments == {'a': 'test:P1', 'b': 'test:P2', 'c': 'test:P3'}


def test_the_other_box_carries_the_person_on_when_the_first_is_gone(robot):
    robot.frames(1.0, seen('a', (3.0, 1.0), WHOLE))
    robot.frames(1.0, seen('a', None, WHOLE), seen('b', None, PART))
    result = robot.frames(3.0, seen('b', None, PART))
    assert result.assignments == {'b': 'test:P1'}
    assert robot.tracker.people['test:P1'].box == PART
    back = robot.frame(seen('b', None, PART), seen('a', None, WHOLE))
    assert back.assignments == {'b': 'test:P1', 'a': 'test:P1'}


def test_another_box_measured_further_than_the_person_could_be_is_someone_else(robot):
    robot.frames(1.0, seen('a', (3.0, 1.0), WHOLE))
    robot.frames(1.0, seen('a', (3.0, 1.0), WHOLE), seen('b', None, PART))
    near = robot.frame(seen('a', (3.0, 1.0), WHOLE), seen('b', (3.6, 1.0), PART))
    assert near.assignments == {'a': 'test:P1', 'b': 'test:P1'}  # Depth wobbles (0.85 m).
    far = robot.frame(seen('a', (3.0, 1.0), WHOLE), seen('b', (6.0, 1.0), PART))
    assert far.assignments == {'a': 'test:P1', 'b': 'test:P2'}


def test_a_suspicion_without_a_track_is_whoever_the_frames_showed_under_its_box(robot):
    """Cloud's box covered the lying person; its own place was 1.3 m off and near where
    someone else had been, and it arrived 2.5 s after it was taken."""
    robot.frames(1.0, seen('old', (1.7, 1.0), BOX_B))
    robot.wait(60.0)
    robot.frames(2.0, seen('a', (3.0, 1.0), BOX_A))
    taken = robot.t
    robot.frames(2.6, seen('a', (3.0, 1.0), BOX_A))
    assert robot.tracker.assign(Observation(None, BOX_A, (1.7, 1.0)), taken, 0) == (
        'test:P2', False)
    assert robot.tracker.people['test:P2'].point == (3.0, 1.0)  # Cloud's place is not theirs.
    # No one under its box, or another view: by its place, as before.
    assert robot.tracker.assign(Observation(None, BOX_B, (1.7, 1.0)), taken, 0)[0] == 'test:P1'
    assert robot.tracker.assign(Observation(None, BOX_A, (1.7, 1.0)), robot.t, 1)[0] == 'test:P1'


# ------------------------------------------- a moment's box, and who was there (11-18)

BODY = (0.47, 0.25, 1.00, 0.98)   # someone lying near the robot
BAG = (0.61, 0.29, 0.74, 0.49)    # a bag under the bed taken for a person (overlap 0.07)


def test_a_box_for_a_moment_is_forgotten_after_five_seconds(robot):
    """11-18: a hanging sleeve seen for a frame or two became 79 numbers in 63 minutes."""
    robot.frame(seen('sleeve', None, box=(0.94, 0.0, 1.0, 0.6)))
    robot.frames(1.0, seen('a', (3.0, 1.0)))
    robot.wait(SHORT_GONE_S + 1)
    robot.frame(seen('b', (9.0, 9.0), box=BOX_B))
    assert set(robot.tracker.people) == {'test:P2', 'test:P3'}  # The sleeve is no one.
    robot.wait(600)
    assert robot.frame(seen('a2', (3.1, 1.0))).assignments == {'a2': 'test:P2'}


def test_a_suspicion_counts_as_someone_at_once(robot):
    person, _ = robot.tracker.assign(seen('fell', (3.0, 1.0)), robot.t, robot.view)
    robot.wait(600)
    robot.frame(seen('b', (9.0, 9.0), box=BOX_B))
    assert person in robot.tracker.people
    assert robot.frame(seen('fell2', (3.2, 1.0))).assignments == {'fell2': person}


def test_something_where_someone_unseen_was_is_them_not_the_one_who_just_left(robot):
    """11-18 09:16: the bag 0.86 m from the person who had just got up took their number,
    though its own was 0.08 m away."""
    robot.frames(1.0, seen('bag', (1.5, -0.45), box=BAG))             # P1
    robot.wait(90)
    robot.frames(2.0, seen('lying', (0.6, 0.3), box=BODY))            # P2, in front of it
    robot.wait(0.5)
    result = robot.frame(seen('bag2', (1.52, -0.46), box=BAG))        # 1.2 m from P2
    assert result.assignments == {'bag2': 'test:P1'}
    assert not robot.tracker.uncertain('test:P2')


def test_something_new_beside_someone_just_gone_is_a_number_of_its_own(robot):
    """11-18 10:19: the bag, never seen before, 0.97 m from the person who left 0.7 s
    ago, its box barely on theirs."""
    robot.frames(2.0, seen('lying', (0.8, 0.15), box=BODY))           # P1
    robot.wait(0.5)
    first = robot.frame(seen('bag', (1.59, -0.40), box=BAG))
    assert first.assignments == {'bag': 'test:P2'} and not robot.tracker.uncertain('test:P1')
    robot.frames(2 * DT, seen('bag', (1.59, -0.40), box=BAG))
    assert robot.tracker.uncertain('test:P1') and robot.tracker.uncertain('test:P2')
    # A minute of only the bag in one place: no longer uncertain, each their own number.
    robot.frames(SOLO_S + REID_WAIT_S + 2, seen('bag', (1.59, -0.40), box=BAG))
    assert not robot.tracker.uncertain('test:P2')
    robot.wait(600)
    back = robot.frame(seen('back', (0.85, 0.2), box=BODY))
    assert back.assignments == {'back': 'test:P1'}


def test_a_new_track_near_or_over_the_one_just_seen_is_still_them(robot):
    robot.frames(2.0, seen('lying', (0.8, 0.15), box=BODY))
    robot.wait(0.5)
    # 0.97 m away but the box still on theirs: they rolled over.
    rolled = robot.frame(seen('rolled', (1.59, -0.40), box=(0.5, 0.3, 1.0, 0.9)))
    assert rolled.assignments == {'rolled': 'test:P1'}
    robot.wait(0.5)
    # A box elsewhere on screen but within the place's error (0.4 m).
    near = robot.frame(seen('near', (1.3, -0.15), box=BAG))
    assert near.assignments == {'near': 'test:P1'}


def test_verdict_compare_requests_use_check_mode(robot):
    requests = apart_a_minute(robot)
    assert len(requests) == 2
    assert all(req.get('mode') == 'check' for req in requests)


def test_edge_entry_without_depth_after_short_gone_reassociates_on_first_depth(robot):
    """2026-11-22: after turning away > SHORT_GONE_S, the person enters at the edge with
    point=None and gets depth 0.6 s later; must merge into the unseen person, not duplicate."""
    robot.frames(1.0, seen('old', (1.90, -0.62), state='suspected'))
    robot.wait(SHORT_GONE_S + 10.0)
    robot.view = 1
    edge = robot.frame(seen('new', None, box=(0.0, 0.0, 0.07, 0.59), state='unknown'))
    assert edge.assignments == {'new': 'test:P2'}
    recovered = robot.frame(seen('new', (1.94, -0.52), box=(0.20, 0.12, 0.35, 0.56),
                                 state='unknown'))
    assert recovered.assignments == {'new': 'test:P1'}
    assert recovered.merged == {'test:P2': 'test:P1'}
    assert 'test:P2' not in robot.tracker.people
    # Edge 'unknown' frame must not erase the unseen person's suspected posture.
    assert robot.tracker.people['test:P1'].posture == 'suspected'
    req, = robot.tracker.take_requests()
    assert req['a']['track'] == 'old' and req['b']['track'] == 'new'


def test_unplaced_waiting_track_recovers_if_depth_arrives_before_settle_hits(robot):
    """Depth returning on frame 2 (< SETTLE_HITS) must merge instead of clearing unplaced."""
    robot.tracker.reid = False
    robot.frames(1.0, seen('a', (3.0, 1.0), state='suspected'))
    robot.wait(1.0)
    first = robot.frame(seen('b', None, NUDGED, state='unknown'))
    assert first.assignments == {'b': 'test:P2'}
    assert robot.tracker.people['test:P2'].unplaced is True
    second = robot.frame(seen('b', (3.1, 1.0), NUDGED, state='unknown'))
    assert second.assignments == {'b': 'test:P1'}
    assert second.merged == {'test:P2': 'test:P1'}
    assert 'test:P2' not in robot.tracker.people
    assert robot.tracker.people['test:P1'].posture == 'suspected'
    assert not robot.tracker.uncertain('test:P1')
