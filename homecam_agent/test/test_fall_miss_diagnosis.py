"""Diagnostic explanations agree with the unchanged Pose quality rules."""
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from diagnose_fall_misses import geometry_checks  # noqa: E402
from homecam_detector.fall_candidate import FallCandidateConfig  # noqa: E402


def value():
    joints = [('left_shoulder', .2, .3), ('right_shoulder', .2, .35),
              ('left_hip', .7, .3), ('right_hip', .7, .35),
              ('left_knee', .8, .3), ('right_knee', .8, .35)]
    return dict(boxConfidence=.2, box=dict(left=.1, top=.2, right=.9, bottom=.5),
                visibleKeypoints=6,
                keypoints=[dict(name=n, x=x, y=y, confidence=.9) for n, x, y in joints])


def check(v):
    return geometry_checks(v, (640, 400), FallCandidateConfig())


def test_low_detection_score_is_not_a_pose_quality_rejection():
    r = check(value())
    assert not r['quality_failures']
    assert not r['horizontal_failures'] and not r['compact_failures']


def test_four_body_joints_do_not_replace_missing_hips():
    v = value()
    for p in v['keypoints']:
        if p['name'].endswith('_hip'):
            p['confidence'] = .2
    r = check(v)
    assert r['features']['body_points'] == 4
    assert not r['features']['usable']
    assert 'hip_present' in r['quality_failures']
    assert 'enough_body_joints' not in r['quality_failures']


def test_reliable_looking_joint_count_can_still_have_degenerate_torso():
    v = value()
    for p in v['keypoints']:
        p['x'], p['y'] = .5, .3
    r = check(v)
    assert r['features']['body_points'] == 6
    assert r['quality_failures'] == ['torso_length_sufficient']


def test_five_joints_and_one_hip_can_be_usable_but_cannot_pass_compact_rule():
    v = value()
    v['keypoints'][3]['confidence'] = .2
    r = check(v)
    assert r['features']['usable']
    assert 'enough_body_joints' in r['compact_failures']
    assert 'both_shoulders_and_hips' in r['compact_failures']


@pytest.mark.parametrize('invalid', ['duplicate', 'nonfinite', 'outside', 'face'])
def test_invalid_or_facial_points_cannot_inflate_body_count(invalid):
    v = value()
    p = dict(v['keypoints'][0])
    if invalid == 'nonfinite':
        p.update(name='left_ankle', x=float('nan'))
    elif invalid == 'outside':
        p.update(name='left_ankle', y=1.1)
    elif invalid == 'face':
        p['name'] = 'nose'
    v['keypoints'].append(p)
    assert check(v)['features']['body_points'] == 6


def test_weak_hips_are_reported_as_evidence_not_silently_promoted():
    v = value()
    v['keypoints'][2]['confidence'] = .236
    v['keypoints'][3]['confidence'] = .275
    r = check(v)
    assert r['shoulder_hip_confidences']['left_hip'] == .236
    assert 'left_hip' not in r['reliable_body_joint_names']
    assert 'hip_present' in r['quality_failures']
