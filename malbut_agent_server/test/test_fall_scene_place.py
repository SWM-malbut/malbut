"""A scene case keeps one place in one camera view; it never identifies a person."""

from malbut_agent_server.application.fall_scene_place import CameraMotionLog, same_place
from malbut_agent_server.domain.fall_monitoring import (
    CloudFallReply, SubjectFrame, VideoAssessment, VoiceAnswer,
)
from test_cloud_fall_monitor import enable, make
from test_fall_cloud_association import finding, reply
from test_fall_unidentified_verification import scan

A, B, C, D = ((.0, .5, .2, .9), (.25, .5, .45, .9), (.5, .5, .7, .9), (.75, .5, .95, .9))


def opened(events):
    return [e.incident_id for e in events if e.kind == 'incident_opened']


def asked(events):
    return [e.incident_id for e in events if e.kind == 'question_requested']


def pose_frame(monitor, clock, stamp, *, moving=False, stationary=False):
    clock.value = stamp
    monitor.ingest_subject_frame(SubjectFrame(
        stamp, (), .5, camera_stationary=stationary, camera_moving=moving))


def first_case(box=A):
    monitor, clock, provider = make()
    enable(monitor)
    events = scan(monitor, clock, provider, reply(finding(box)))
    case, = opened(events)
    assert asked(events) == [case]
    return monitor, clock, provider, case


def test_same_place_in_the_same_view_continues_without_asking_again():
    monitor, clock, provider, case = first_case()
    pose_frame(monitor, clock, 190, stationary=True)
    events = scan(monitor, clock, provider, reply(finding((.02, .52, .22, .9))), stamp=220)
    assert not opened(events) and not asked(events)
    assert monitor.incident(case).scene_boxes == ((.02, .52, .22, .9),)


def test_another_place_opens_its_own_case_and_question():
    monitor, clock, provider, case = first_case()
    events = scan(monitor, clock, provider, reply(finding(C)), stamp=220)
    other, = opened(events)
    assert other != case and asked(events) == [other]
    assert monitor.incident(other).subject_key is None
    assert monitor.incident(case).scene_boxes == (A,)


def test_reported_camera_movement_separates_even_the_same_image_position():
    # 2026-10-07: the robot drove to another room; a squat there joined a bag case.
    monitor, clock, provider, case = first_case()
    pose_frame(monitor, clock, 190, moving=True)
    events = scan(monitor, clock, provider, reply(finding(A)), stamp=220)
    other, = opened(events)
    assert other != case and asked(events) == [other]


def test_movement_before_the_case_was_seen_does_not_separate():
    monitor, clock, provider = make()
    enable(monitor)
    pose_frame(monitor, clock, 150, moving=True)
    case, = opened(scan(monitor, clock, provider, reply(finding(A))))
    assert not opened(scan(monitor, clock, provider, reply(finding(A)), stamp=220))
    assert monitor.incident(case).scene_seen_from == 219.5


def test_pose_input_reset_makes_positions_incomparable():
    monitor, clock, provider, case = first_case()
    monitor.invalidate_subject_input()
    other, = opened(scan(monitor, clock, provider, reply(finding(A)), stamp=220))
    assert other != case


def test_unknown_location_keeps_joining_the_latest_case():
    monitor, clock, provider, case = first_case()
    for value in (CloudFallReply(VideoAssessment.SUSPECTED_FALL, 'no location'),
                  CloudFallReply(VideoAssessment.SUSPECTED_FALL, 'bad location',
                                 localization_failed=True)):
        clock.value += 60
        assert not opened(scan(monitor, clock, provider, value, stamp=clock.value))
    assert monitor.incident(case).scene_boxes == (A,)


def test_findings_of_one_scan_share_one_room_question():
    monitor, clock, provider = make()
    enable(monitor)
    events = scan(monitor, clock, provider, reply(finding(A), finding(C)))
    case, = opened(events)
    assert asked(events) == [case]
    assert sum(e.kind == 'cloud_discovery' and e.discovery.incident_id == case
               for e in events) == 2


def test_open_scene_cases_are_bounded_and_then_join_the_latest():
    monitor, clock, provider, first = first_case(A)
    cases = [first]
    for stamp, box in ((220, B), (280, C)):
        case, = opened(scan(monitor, clock, provider, reply(finding(box)), stamp=stamp))
        cases.append(case)
    events = scan(monitor, clock, provider, reply(finding(D)), stamp=340)
    assert not opened(events) and not asked(events)
    assert monitor.incident(cases[-1]).scene_boxes == (D,)
    assert len({*cases}) == monitor.max_open_scene_cases == 3


def test_same_place_needs_overlap_or_near_centres_unless_a_location_is_unknown():
    assert same_place((A,), ((.05, .5, .25, .9),))
    assert not same_place((A,), (C,))
    assert same_place((), (C,)) and same_place((A,), ())
    assert same_place((A, C), (C,))
    # 2026-10-07 robot boxes: one mistaken spot moved between neighbouring objects.
    gap, bag = (.432, .368, .465, .478), (.341, .288, .462, .482)
    bag2, beside_box = (.112, .274, .261, .496), (.235, .38, .263, .488)
    assert same_place((gap,), (bag,)) and same_place((bag2,), (beside_box,))
    assert not same_place((bag,), (bag2,))  # 21% apart: another place


def test_motion_log_joins_marks_and_treats_resets_and_dropped_history_as_movement():
    log = CameraMotionLog(max_intervals=2)
    log.observe(1, moving=False)
    assert not log.moved_between(0, 10)
    for t in (5, 5.2, 5.4):
        log.observe(t, moving=True)
    assert log.moved_between(4, 6) and not log.moved_between(5.5, 9)
    log.unknown(20)
    assert log.moved_between(10, 21)
    log.observe(30, moving=False)
    assert log.moved_between(25, 26) and not log.moved_between(31, 40)
    log.observe(50, moving=True)
    log.observe(60, moving=True)  # Drops the oldest interval: before it is unknown.
    assert log.moved_between(1, 2) and not log.moved_between(31, 40)


class Places:
    """Fake AMCL + depth locator: a map point per Cloud box, None when unknown."""

    def __init__(self, points):
        self.points = points

    def locate(self, captured_at, box):
        return self.points.get(box)

    def clear(self):
        pass


def mapped(points):
    monitor, clock, provider = make()
    monitor._place = Places(points)
    enable(monitor)
    return monitor, clock, provider


def test_map_point_keeps_one_case_when_the_robot_moved_and_the_bag_moved_on_screen():
    # 2026-10-07 15:11 vs 16:05: same bag, robot elsewhere, 21% apart on screen.
    bag, bag2 = (.341, .288, .462, .482), (.112, .274, .261, .496)
    monitor, clock, provider = mapped({bag: (2.0, 1.0), bag2: (2.4, 1.3)})
    case, = opened(scan(monitor, clock, provider, reply(finding(bag))))
    pose_frame(monitor, clock, 190, moving=True)
    events = scan(monitor, clock, provider, reply(finding(bag2)), stamp=220)
    assert not opened(events) and not asked(events)
    assert monitor.incident(case).scene_points == ((2.4, 1.3),)


def test_more_than_one_metre_apart_on_the_map_is_another_case_even_at_one_screen_spot():
    monitor, clock, provider = mapped({A: (0.0, 0.0), (.0, .5, .2, .91): (1.2, 0.0)})
    case, = opened(scan(monitor, clock, provider, reply(finding(A))))
    moved = reply(finding((.0, .5, .2, .91)))
    other, = opened(scan(monitor, clock, provider, moved, stamp=220))
    assert other != case


def test_ten_quiet_minutes_end_the_situation_even_at_the_same_place():
    monitor, clock, provider = mapped({A: (0.0, 0.0)})
    case, = opened(scan(monitor, clock, provider, reply(finding(A))))
    assert not opened(scan(monitor, clock, provider, reply(finding(A)), stamp=760))  # 600 s gap
    other, = opened(scan(monitor, clock, provider, reply(finding(A)), stamp=1361))
    assert other != case
    assert asked(scan(monitor, clock, provider, reply(finding(A)), stamp=1421)) == []


def test_without_a_map_point_on_one_side_screen_positions_decide():
    monitor, clock, provider = mapped({A: (0.0, 0.0)})
    case, = opened(scan(monitor, clock, provider, reply(finding(A))))
    # C has no map point (no depth there): screen rule, C is elsewhere on screen.
    other, = opened(scan(monitor, clock, provider, reply(finding(C)), stamp=220))
    assert other != case
    assert not opened(scan(monitor, clock, provider, reply(finding((.01, .5, .21, .9))),
                           stamp=280))


def test_quiet_cases_do_not_fill_the_open_case_limit():
    monitor, clock, provider = mapped({})
    first, = opened(scan(monitor, clock, provider, reply(finding(A))))
    second, = opened(scan(monitor, clock, provider, reply(finding(B)), stamp=220))
    third, = opened(scan(monitor, clock, provider, reply(finding(C)), stamp=280))
    # All three are quiet after ten minutes; a new place gets its own case.
    fourth, = opened(scan(monitor, clock, provider, reply(finding(D)), stamp=1000))
    assert len({first, second, third, fourth}) == 4


def test_memory_releases_only_finished_quiet_cases_when_full():
    monitor, clock, provider = make(max_incidents=2)
    enable(monitor)
    events = scan(monitor, clock, provider, reply(finding(A)))
    first, = opened(events)
    second, = opened(scan(monitor, clock, provider, reply(finding(C)), stamp=220))
    # Unanswered questions keep both cases: the next place joins the latest case.
    assert not opened(scan(monitor, clock, provider, reply(finding(A)), stamp=1000))
    for iid in (first, second):
        monitor._incidents[iid].answer = VoiceAnswer.UNCLEAR  # Scene answers never clear.
    third, = opened(scan(monitor, clock, provider, reply(finding(A)), stamp=1700))
    assert first not in monitor._incidents and third in monitor._incidents


def test_ten_minutes_count_from_the_latest_finding_not_the_first():
    # 14:00, 14:08, 14:11 at one place: one case; quiet until 14:22 starts another.
    monitor, clock, provider = mapped({A: (0.0, 0.0)})
    base = 1000
    case, = opened(scan(monitor, clock, provider, reply(finding(A)), stamp=base))
    for minutes in (8, 11):
        assert not opened(scan(monitor, clock, provider, reply(finding(A)),
                               stamp=base + minutes * 60))
    assert monitor.incident(case).scene_seen_until == base + 11 * 60
    other, = opened(scan(monitor, clock, provider, reply(finding(A)), stamp=base + 22 * 60))
    assert other != case
