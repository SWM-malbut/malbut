"""Offline experiment contracts, not measured real-world fall accuracy."""
from dataclasses import replace
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from replay_leg_change import canonical, tracking_snapshot  # noqa: E402
from experimental_leg_change import (  # noqa: E402
    LegChangeConfig, LegChangeExperiment, merge_requests,
)
from homecam_detector.fall_candidate import FallCandidateDetector  # noqa: E402
from homecam_detector.pose import PersonPose, PoseKeypoint  # noqa: E402
from homecam_detector.pose_tracker import PoseTrackingResult, TrackedPose  # noqa: E402


def body(kind='standing', scale=1, dx=0, dy=0, score=.9):
    if kind == 'standing':
        joints = [(0.55, .15), (.55, .4), (.55, .65), (.55, .9)]
    elif kind == 'seated':
        joints = [(.55, .25), (.55, .5), (.35, .5), (.30, .9)]
    elif kind == 'lowered':
        joints = [(.55, .5), (.55, .75), (.35, .70), (.25, .80)]
    else:
        raise ValueError(kind)
    points = tuple(PoseKeypoint(f'{side}_{name}', (x+offset)*scale+dx, y*scale+dy, score)
                   for side, offset in [('left', 0), ('right', .02)]
                   for name, (x, y) in zip(('shoulder', 'hip', 'knee', 'ankle'), joints))
    return PersonPose(.2, (.1*scale+dx, .1*scale+dy, .9*scale+dx, .95*scale+dy),
                      points, sum(p.confidence >= .5 for p in points))


def step(base, exp, pose, stamp, tid='a', size=(640, 400)):
    track = TrackedPose(tid, 'tracked' if pose else 'missing', pose, 'weak', 3, 3, 0, ())
    tracks = PoseTrackingResult((track,), (), ())
    baseline = base.update(tracks, capture_time=stamp, image_size=size, robot_motion='unknown')
    return exp.update(tracks, baseline, capture_time=stamp, image_size=size)


@pytest.mark.parametrize('initial', ['standing', 'seated'])
def test_upright_torso_can_produce_verification_after_bilateral_change(initial):
    base, exp = FallCandidateDetector(), LegChangeExperiment()
    assert not step(base, exp, body(initial), 0)[0]
    assert not step(base, exp, body('lowered'), .2)[0]
    assert not step(base, exp, body('lowered'), .4)[0]
    events, _ = step(base, exp, body('lowered'), .6)
    assert len(events) == 1
    assert events[0]['candidateKind'] == 'fall_suspected'
    assert events[0]['requiresVerification']
    assert events[0]['evidenceStartSec'] == 0
    assert 'sitting_or_sinking_not_distinguished' in events[0]['uncertainties']
    assert not step(base, exp, body('lowered'), .8)[0]


@pytest.mark.parametrize('kind', ['standing', 'seated', 'lowered'])
def test_static_posture_or_id_does_not_create_transition(kind):
    base, exp = FallCandidateDetector(), LegChangeExperiment()
    for n in range(20):
        assert not step(base, exp, body(kind), n*.2)[0]


@pytest.mark.parametrize('mode', ['translation', 'zoom', 'both'])
def test_translation_or_uniform_zoom_is_not_descent(mode):
    base, exp = FallCandidateDetector(), LegChangeExperiment()
    for n in range(10):
        scale = 1-n*.02 if mode in ('zoom', 'both') else .8
        offset = n*.005 if mode in ('translation', 'both') else 0
        assert not step(base, exp, body('seated', scale=scale, dx=offset, dy=offset), n*.2)[0]


@pytest.mark.parametrize('change', ['missing', 'low_confidence', 'gap', 'new_id', 'size'])
def test_do_not_join_unobserved_or_different_person_intervals(change):
    base, exp = FallCandidateDetector(), LegChangeExperiment()
    step(base, exp, body(), 0)
    if change in ('missing', 'low_confidence'):
        step(base, exp, None if change == 'missing' else body(score=.1), .2)
    start = 1 if change == 'gap' else .4
    for n in range(5):
        events, _ = step(
            base, exp, body('lowered'), start+n*.2,
            tid='b' if change == 'new_id' else 'a',
            size=(1280, 800) if change == 'size' else (640, 400))
        assert not events


def test_one_leg_or_unreliable_ankle_cannot_satisfy_bilateral_condition():
    base, exp = FallCandidateDetector(), LegChangeExperiment()
    step(base, exp, body(), 0)
    lower = body('lowered')
    lower = replace(lower, keypoints=tuple(
        replace(p, confidence=.49) if p.name == 'right_ankle' else p for p in lower.keypoints))
    for n in range(5):
        assert not step(base, exp, lower, .2+n*.2)[0]


def test_one_sample_change_is_not_enough():
    base, exp = FallCandidateDetector(), LegChangeExperiment()
    for i, kind in enumerate(['standing', 'lowered', 'seated', 'lowered', 'seated']):
        assert not step(base, exp, body(kind), i*.2)[0]


def test_invalid_clock_interrupts_but_never_means_recovered():
    base, exp = FallCandidateDetector(), LegChangeExperiment()
    step(base, exp, body(), 0)
    step(base, exp, body('lowered'), .2)
    assert not step(base, exp, body('lowered'), .2)[0]
    for n in range(5):
        assert not step(base, exp, body('lowered'), .4+n*.2)[0]


@pytest.mark.parametrize('kwargs', [
    dict(minimum_samples=True), dict(minimum_samples=1),
    dict(window_sec=float('nan')), dict(keypoint_threshold=2),
    dict(max_gap_sec=3), dict(maximum_end_clearance=.6),
])
def test_invalid_config_rejected(kwargs):
    with pytest.raises(ValueError):
        LegChangeConfig(**kwargs)


def test_union_deduplicates_calls_but_preserves_later_evidence():
    first = dict(candidateId='exp-a', targetTrackId='a')
    later = dict(candidateId='base-a', targetTrackId='a')
    requests = {}
    assert merge_requests([], [first], requests) == ([first], [])
    out, updates = merge_requests([later], [], requests)
    assert out == [] and updates == [dict(requestCandidateId='exp-a', evidence=later)]
    other = dict(candidateId='base-b', targetTrackId='b')
    assert merge_requests([other], [], requests)[0] == [other]


def test_baseline_wins_same_frame_tie():
    baseline = dict(candidateId='base', targetTrackId='a')
    extra = dict(candidateId='extra', targetTrackId='a')
    requests, updates = merge_requests([baseline], [extra], {})
    assert requests == [baseline]
    assert updates[0]['evidence'] == extra


def test_parity_normalizes_only_random_ids_not_values():
    assert canonical(dict(candidateId='uuid1-candidate-1', value=.5)) == canonical(
        dict(candidateId='uuid2-candidate-1', value=.5))
    assert canonical(dict(candidateId='uuid1-candidate-1', value=.5)) != canonical(
        dict(candidateId='uuid2-candidate-1', value=.6))


def test_snapshot_preserves_pose_and_rejects_two_observations_for_one_track():
    row = dict(observations=[dict(pose=body().as_dict(), track_id='a')], fall_analysis=dict(
        tracks=[dict(targetTrackId='a', trackingState='tracked')],
        candidates=[], unassignedCount=0, expiredTrackIds=[]))
    assert tracking_snapshot(row).tracks[0].pose == body()
    row['observations'] *= 2
    with pytest.raises(ValueError, match='two observations'):
        tracking_snapshot(row)
